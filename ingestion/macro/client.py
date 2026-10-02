"""FRED + ALFRED HTTP client.

Fetches vintage-aware macro observations from the St. Louis Fed.

Design
------
- **One request per series.** We use FRED's ``realtime_start=1776-07-04``
  sentinel, which returns every vintage of every observation in one
  response. We then group by ``realtime_start`` to reconstruct the
  individual vintages. This is O(series) requests, not O(series x vintages).
- **Retry with exponential backoff + jitter.** FRED is generally
  reliable, but transient failures happen; we do not want a single
  blip to abort a 149-series run.
- **Rate limiting.** FRED allows 120 req/min on the free tier. We
  default to 0.6s between calls (100 req/min) with configurable jitter.
- **Failures are typed.** ``MacroFetchError`` is raised on 4xx/5xx and
  on malformed responses; the caller decides whether to continue.

Endpoints used:
  - /fred/series           — metadata (title, frequency, units)
  - /fred/series/observations — observations, vintage-aware

See ADR 0009 for the PIT design.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ingestion.logging import get_logger
from ingestion.macro.config import MacroSettings, get_macro_settings
from ingestion.macro.schemas import MacroObservation, MacroSnapshot, utcnow

__all__ = [
    "FRED_BASE_URL",
    "FredClient",
    "MacroFetchError",
]

log = get_logger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org"

# FRED's sentinel for "since the beginning of time" for the realtime
# parameters. It is the value FRED itself uses in its own documentation.
_ALL_TIME = "1776-07-04"


class MacroFetchError(RuntimeError):
    """Raised when a FRED fetch fails after all retries are exhausted."""


@dataclass
class FredClient:
    """FRED + ALFRED HTTP client.

    Args:
        rate_limit_seconds: Minimum gap between requests. Set to 0 to
            disable (useful in tests).
        max_retries: Number of attempts before giving up.
        retry_min_seconds / retry_max_seconds: Backoff bounds.
        sleep: Injectable sleep for tests. Defaults to ``time.sleep``.
        settings: Optional MacroSettings override.
    """

    rate_limit_seconds: float | None = None
    max_retries: int | None = None
    retry_min_seconds: float | None = None
    retry_max_seconds: float | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    settings: MacroSettings = field(default_factory=get_macro_settings)

    _last_call_ts: float = field(default=0.0, init=False, repr=False)

    # ── Public API ──────────────────────────────────────────
    def fetch_metadata(self, series_id: str) -> dict[str, Any]:
        """Fetch series metadata (title, frequency, units).

        Returns an empty dict if FRED does not recognise the series.
        """
        data = self._get_json(
            "/fred/series",
            {"series_id": series_id, "file_type": "json"},
        )
        series_list = data.get("seriess") or []
        if not series_list:
            return {}
        return dict(series_list[0])

    def fetch_vintages(
        self,
        series_id: str,
        *,
        observation_start: date,
        observation_end: date | None = None,
        mode: str = "full",
    ) -> list[MacroSnapshot]:
        """Fetch vintages of ``series_id`` in the given window.

        Args:
            series_id: FRED series id.
            observation_start: Earliest observation_date to include.
            observation_end: Latest observation_date to include; None
                means "up to today".

        Returns:
            One :class:`MacroSnapshot` per distinct vintage_date, sorted
            ascending by vintage_date. Empty list if FRED returned no
            observations.

        Raises:
            MacroFetchError: on HTTP errors or malformed responses.
        """
        if mode == "latest":
            return self._fetch_latest(
                series_id,
                observation_start=observation_start,
                observation_end=observation_end,
            )
        if mode != "full":
            raise ValueError(f"unknown vintage mode: {mode!r}")

        params: dict[str, Any] = {
            "series_id": series_id,
            "file_type": "json",
            "observation_start": observation_start.isoformat(),
            "realtime_start": _ALL_TIME,
        }
        if observation_end is not None:
            params["observation_end"] = observation_end.isoformat()

        data = self._get_json("/fred/series/observations", params)
        raw_obs = data.get("observations") or []
        if not raw_obs:
            return []

        # FRED returns one row per (observation_date, vintage interval)
        # where the vintage interval is [realtime_start, realtime_end].
        # It does NOT repeat unchanged observations across vintages.
        # To reconstruct the *full* vintage (what a user would have seen
        # on date V), we:
        #   1. Collect every distinct vintage date in the response.
        #   2. For each vintage V, keep rows where
        #      realtime_start <= V <= realtime_end.
        #   3. Group by observation_date; pick the value with the
        #      highest realtime_start (most recent revision known at V).
        # See ADR 0009 (vintage-aware PIT).
        rows: list[tuple[date, date, date, Any]] = []
        for row in raw_obs:
            obs_date = _parse_date(row.get("date"))
            rt_start = _parse_date(row.get("realtime_start"))
            rt_end = _parse_date(row.get("realtime_end"))
            if obs_date is None or rt_start is None or rt_end is None:
                continue
            rows.append((obs_date, rt_start, rt_end, row.get("value")))

        if not rows:
            return []

        vintages = sorted({r[1] for r in rows})
        snapshots: list[MacroSnapshot] = []
        for vintage in vintages:
            # Map observation_date -> (best_rt_start, raw_value) for this vintage.
            best: dict[date, tuple[date, Any]] = {}
            for obs_date, rt_start, rt_end, raw_value in rows:
                if not (rt_start <= vintage <= rt_end):
                    continue
                prev = best.get(obs_date)
                if prev is None or rt_start > prev[0]:
                    best[obs_date] = (rt_start, raw_value)

            observations: list[MacroObservation] = []
            for obs_date in sorted(best):
                _, raw_value = best[obs_date]
                try:
                    obs = MacroObservation(
                        series_id=series_id,
                        observation_date=obs_date,
                        value=raw_value,
                        vintage_date=vintage,
                    )
                except Exception:
                    log.warning(
                        "macro_obs_skipped",
                        series_id=series_id,
                        observation_date=str(obs_date),
                        vintage_date=str(vintage),
                    )
                    continue
                observations.append(obs)

            snapshots.append(
                MacroSnapshot(
                    series_id=series_id,
                    vintage_date=vintage,
                    fetched_at=utcnow(),
                    observations=observations,
                )
            )
        return snapshots

    def _fetch_latest(
        self,
        series_id: str,
        *,
        observation_start: date,
        observation_end: date | None = None,
    ) -> list[MacroSnapshot]:
        """Fetch a single snapshot: observations as of the latest update.

        Used for series that either (a) have too many vintages for FRED
        to return in one request, or (b) exist only in FRED, not ALFRED.
        The resulting snapshot has ``vintage_date = max(observation_date)``
        and carries no revision history.

        See ``config/macro_series_latest_only.yml`` and ADR 0009.
        """
        params: dict[str, Any] = {
            "series_id": series_id,
            "file_type": "json",
            "observation_start": observation_start.isoformat(),
        }
        if observation_end is not None:
            params["observation_end"] = observation_end.isoformat()

        data = self._get_json("/fred/series/observations", params)
        raw_obs = data.get("observations") or []
        if not raw_obs:
            return []

        rows: list[tuple[date, Any]] = []
        for row in raw_obs:
            obs_date = _parse_date(row.get("date"))
            if obs_date is None:
                continue
            rows.append((obs_date, row.get("value")))

        if not rows:
            return []

        vintage_date = max(d for d, _ in rows)
        observations: list[MacroObservation] = []
        for obs_date, value in rows:
            try:
                obs = MacroObservation(
                    series_id=series_id,
                    observation_date=obs_date,
                    value=value,
                    vintage_date=vintage_date,
                )
            except Exception:
                continue
            observations.append(obs)

        return [
            MacroSnapshot(
                series_id=series_id,
                vintage_date=vintage_date,
                observations=sorted(observations, key=lambda o: o.observation_date),
            )
        ]

    # ── Internals ───────────────────────────────────────────
    def _rate_limit(self) -> None:
        limit = (
            self.rate_limit_seconds
            if self.rate_limit_seconds is not None
            else self.settings.macro_rate_limit_seconds
        )
        if limit <= 0:
            self._last_call_ts = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_call_ts
        wait = limit - elapsed
        if self._last_call_ts > 0 and wait > 0:
            self.sleep(wait)
        self._last_call_ts = time.monotonic()

    def _get_json(
        self,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """GET with retry, backoff, rate limit. Returns parsed JSON."""
        self._rate_limit()
        return self._get_with_retry(path, params)

    def _get_with_retry(
        self,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        max_retries = (
            self.max_retries if self.max_retries is not None else self.settings.macro_max_retries
        )
        retry_min = (
            self.retry_min_seconds
            if self.retry_min_seconds is not None
            else self.settings.macro_retry_min_seconds
        )
        retry_max = (
            self.retry_max_seconds
            if self.retry_max_seconds is not None
            else self.settings.macro_retry_max_seconds
        )

        @retry(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential_jitter(initial=retry_min, max=retry_max),
            retry=retry_if_exception_type((MacroFetchError, ConnectionError, TimeoutError)),
            before_sleep=self._log_retry(path),
            reraise=True,
        )
        def _do() -> dict[str, Any]:
            return self._get_once(path, params)

        return _do()

    def _log_retry(self, path: str) -> Callable[[RetryCallState], None]:
        def _hook(state: RetryCallState) -> None:
            outcome = state.outcome
            exc = outcome.exception() if outcome is not None else None
            log.warning(
                "macro_fetch_retry",
                path=path,
                attempt=state.attempt_number,
                error=repr(exc) if exc is not None else None,
            )

        return _hook

    def _get_once(
        self,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Single attempt against FRED. Raises MacroFetchError on failure."""
        api_key = self.settings.fred_api_key
        if not api_key:
            raise MacroFetchError("FRED_API_KEY is not set. Add it to .env or Colab secrets.")

        url = f"{FRED_BASE_URL}{path}"
        full_params = {**params, "api_key": api_key}

        log.info("macro_fetch_start", path=path, series_id=params.get("series_id"))
        try:
            with httpx.Client(
                timeout=self.settings.macro_request_timeout_seconds,
                headers={
                    # FRED requests that we identify ourselves. See
                    # https://fred.stlouisfed.org/docs/api/faq.html
                    "User-Agent": "data-backtest-pipeline/0.1",
                },
            ) as client:
                resp = client.get(url, params=full_params)
        except httpx.HTTPError as exc:
            raise MacroFetchError(f"HTTP error for {path}: {exc!r}") from exc

        if resp.status_code >= 400:
            raise MacroFetchError(f"FRED returned {resp.status_code} for {path}: {resp.text[:200]}")

        try:
            data = resp.json()
        except Exception as exc:
            raise MacroFetchError(f"FRED returned non-JSON for {path}: {exc!r}") from exc

        if not isinstance(data, dict):
            raise MacroFetchError(
                f"FRED returned unexpected payload for {path}: {type(data).__name__}"
            )
        return data


def _parse_date(value: Any) -> date | None:
    """Parse a YYYY-MM-DD string; return None if not parseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
