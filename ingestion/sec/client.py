"""SEC EDGAR companyfacts client.

Fetches XBRL fundamental facts for a ticker via SEC's public API.

Design
------
- **User-Agent required.** SEC blocks anonymous requests. We send
  the value from `SEC_USER_AGENT` (format: "AppName ContactEmail").
- **Two hosts.** SEC serves static files from `www.sec.gov` (ticker
  map) and the XBRL REST API from `data.sec.gov` (companyfacts).
  Using the wrong host returns 404 with no further explanation.
- **Rate limiting.** SEC fair-use limit is 10 requests/second per IP.
  We default to 0.15s between calls (~6.7 req/s), configurable.
- **Ticker -> CIK mapping** is fetched once via
  `https://www.sec.gov/files/company_tickers.json` and cached in memory.
- **Retry with exponential backoff.** SEC is generally reliable, but
  transient 5xx and network blips happen; a single blip should not
  abort a 32-ticker run.
- **Failures are typed.** `SecFetchError` on HTTP or malformed
  responses. A single bad ticker does not abort the run (caller
  decides).

Endpoints:
  GET https://www.sec.gov/files/company_tickers.json
  GET https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json

See ADR 0010.
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
from ingestion.sec.config import SecSettings, get_sec_settings
from ingestion.sec.schemas import SecCompanyFacts, SecFact, utcnow

__all__ = [
    "SEC_API_BASE_URL",
    "SEC_STATIC_BASE_URL",
    "SecClient",
    "SecFetchError",
]

log = get_logger(__name__)

# Two different hosts:
# - www.sec.gov serves static files (ticker map).
# - data.sec.gov serves the XBRL REST API (companyfacts).
# Hardcoding the wrong host returns 404 with no further explanation.
SEC_STATIC_BASE_URL = "https://www.sec.gov"
SEC_API_BASE_URL = "https://data.sec.gov"
SEC_TICKER_MAP_PATH = "/files/company_tickers.json"
SEC_COMPANYFACTS_PATH = "/api/xbrl/companyfacts/CIK{cik:010d}.json"


class SecFetchError(RuntimeError):
    """Raised when a SEC fetch fails after all retries are exhausted."""


@dataclass
class SecClient:
    """SEC EDGAR companyfacts client.

    Args:
        rate_limit_seconds: Minimum gap between requests.
        max_retries: Attempts before giving up.
        retry_min_seconds / retry_max_seconds: Backoff bounds.
        sleep: Injectable sleep for tests.
        settings: Optional SecSettings override.
    """

    rate_limit_seconds: float | None = None
    max_retries: int | None = None
    retry_min_seconds: float | None = None
    retry_max_seconds: float | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    settings: SecSettings = field(default_factory=get_sec_settings)

    _last_call_ts: float = field(default=0.0, init=False, repr=False)
    _ticker_to_cik: dict[str, int] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    # ── Public API ──────────────────────────────────────────
    def get_cik(self, ticker: str) -> int | None:
        """Return the CIK for a ticker, or None if not found.

        Fetches the ticker->CIK map on first call and caches it.
        """
        if self._ticker_to_cik is None:
            self._ticker_to_cik = self._fetch_ticker_map()
        return self._ticker_to_cik.get(ticker.upper())

    def fetch_company_facts(self, ticker: str) -> SecCompanyFacts:
        """Fetch and parse company facts for one ticker.

        Args:
            ticker: Symbol, e.g. "AAPL".

        Returns:
            A :class:`SecCompanyFacts` with all facts.

        Raises:
            SecFetchError: if ticker is unknown, HTTP fails, or the
                response cannot be parsed.
        """
        ticker = ticker.upper()
        cik = self.get_cik(ticker)
        if cik is None:
            raise SecFetchError(f"Unknown ticker (no CIK): {ticker}")

        path = SEC_COMPANYFACTS_PATH.format(cik=cik)
        data = self._get_json(path, base_url=SEC_API_BASE_URL)
        return self._parse_company_facts(ticker=ticker, cik=cik, data=data)

    # ── Ticker -> CIK map ───────────────────────────────────
    def _fetch_ticker_map(self) -> dict[str, int]:
        """Download and cache the ticker->CIK map from SEC."""
        data = self._get_json(SEC_TICKER_MAP_PATH, base_url=SEC_STATIC_BASE_URL)
        mapping: dict[str, int] = {}
        # Shape: {"0": {"cik_str": 320193, "ticker": "AAPL",
        #               "title": "Apple Inc."}, ...}
        if not isinstance(data, dict):
            raise SecFetchError(f"Unexpected ticker map shape: {type(data).__name__}")
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            t = entry.get("ticker")
            c = entry.get("cik_str")
            if isinstance(t, str) and isinstance(c, int):
                mapping[t.upper()] = c
        log.info("sec_ticker_map_loaded", n_tickers=len(mapping))
        return mapping

    # ── Parsing ─────────────────────────────────────────────
    def _parse_company_facts(
        self,
        *,
        ticker: str,
        cik: int,
        data: dict[str, Any],
    ) -> SecCompanyFacts:
        """Convert a raw companyfacts JSON into SecCompanyFacts."""
        entity_name = str(data.get("entityName") or f"CIK{cik}")
        facts_raw = data.get("facts")
        if not isinstance(facts_raw, dict):
            raise SecFetchError(f"No 'facts' object for {ticker} (cik={cik})")

        facts: list[SecFact] = []
        for namespace, tags in facts_raw.items():
            if not isinstance(tags, dict):
                continue
            for tag, tag_obj in tags.items():
                if not isinstance(tag_obj, dict):
                    continue
                units = tag_obj.get("units")
                if not isinstance(units, dict):
                    continue
                for unit, rows in units.items():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        fact = self._parse_fact_row(
                            ticker=ticker,
                            cik=cik,
                            namespace=namespace,
                            tag=tag,
                            unit=unit,
                            row=row,
                        )
                        if fact is not None:
                            facts.append(fact)

        snapshot = SecCompanyFacts(
            ticker=ticker,
            cik=cik,
            entity_name=entity_name,
            fetched_at=utcnow(),
            facts=facts,
        )
        log.info(
            "sec_companyfacts_parsed",
            ticker=ticker,
            cik=cik,
            n_facts=snapshot.n_facts,
            n_tags=snapshot.n_tags,
        )
        return snapshot

    @staticmethod
    def _parse_fact_row(
        *,
        ticker: str,
        cik: int,
        namespace: str,
        tag: str,
        unit: str,
        row: Any,
    ) -> SecFact | None:
        """Parse one row from a companyfacts response.

        Returns None if the row lacks required fields (end, filed,
        form) rather than aborting the whole fetch.
        """
        if not isinstance(row, dict):
            return None

        period_end = _parse_date(row.get("end"))
        filed = _parse_date(row.get("filed"))
        form = row.get("form")
        if period_end is None or filed is None or not isinstance(form, str):
            return None

        period_start = _parse_date(row.get("start"))

        value = row.get("val")
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None

        fy = row.get("fy")
        if not isinstance(fy, int):
            fy = None

        fp = row.get("fp")
        if not isinstance(fp, str) or not fp.strip():
            fp = None

        frame = row.get("frame")
        if not isinstance(frame, str) or not frame.strip():
            frame = None

        try:
            return SecFact(
                ticker=ticker,
                cik=cik,
                namespace=namespace,
                tag=tag,
                unit=unit,
                period_start=period_start,
                period_end=period_end,
                filed=filed,
                form=form[:16],
                fiscal_year=fy,
                fiscal_period=fp,
                frame=frame,
                value=value,
            )
        except Exception:
            return None

    # ── HTTP plumbing ───────────────────────────────────────
    def _rate_limit(self) -> None:
        limit = (
            self.rate_limit_seconds
            if self.rate_limit_seconds is not None
            else self.settings.sec_rate_limit_seconds
        )
        if limit <= 0:
            self._last_call_ts = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_call_ts
        wait = limit - elapsed
        if self._last_call_ts > 0 and wait > 0:
            self.sleep(wait)
        self._last_call_ts = time.monotonic()

    def _get_json(self, path: str, *, base_url: str) -> dict[str, Any]:
        self._rate_limit()
        return self._get_with_retry(path, base_url=base_url)

    def _get_with_retry(self, path: str, *, base_url: str) -> dict[str, Any]:
        max_retries = (
            self.max_retries if self.max_retries is not None else self.settings.sec_max_retries
        )
        retry_min = (
            self.retry_min_seconds
            if self.retry_min_seconds is not None
            else self.settings.sec_retry_min_seconds
        )
        retry_max = (
            self.retry_max_seconds
            if self.retry_max_seconds is not None
            else self.settings.sec_retry_max_seconds
        )

        @retry(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential_jitter(initial=retry_min, max=retry_max),
            retry=retry_if_exception_type((SecFetchError, ConnectionError, TimeoutError)),
            before_sleep=self._log_retry(path),
            reraise=True,
        )
        def _do() -> dict[str, Any]:
            return self._get_once(path, base_url=base_url)

        return _do()

    def _log_retry(self, path: str) -> Callable[[RetryCallState], None]:
        def _hook(state: RetryCallState) -> None:
            outcome = state.outcome
            exc = outcome.exception() if outcome is not None else None
            log.warning(
                "sec_fetch_retry",
                path=path,
                attempt=state.attempt_number,
                error=repr(exc) if exc is not None else None,
            )

        return _hook

    def _get_once(self, path: str, *, base_url: str) -> dict[str, Any]:
        user_agent = self.settings.sec_user_agent
        if not user_agent or "@" not in user_agent:
            raise SecFetchError(
                'SEC_USER_AGENT is not set or malformed. Expected "AppName ContactEmail".'
            )

        url = f"{base_url}{path}"

        log.info("sec_fetch_start", path=path)
        try:
            with httpx.Client(
                timeout=self.settings.sec_request_timeout_seconds,
                headers={
                    "User-Agent": user_agent,
                    "Accept-Encoding": "gzip, deflate",
                    # Do NOT set "Host" explicitly: httpx derives it
                    # from the URL. Hardcoding it returns 404.
                },
            ) as client:
                resp = client.get(url)
        except httpx.HTTPError as exc:
            raise SecFetchError(f"HTTP error for {path}: {exc!r}") from exc

        if resp.status_code == 404:
            raise SecFetchError(f"SEC returned 404 for {path}")
        if resp.status_code >= 400:
            raise SecFetchError(f"SEC returned {resp.status_code} for {path}: {resp.text[:200]}")

        try:
            data = resp.json()
        except Exception as exc:
            raise SecFetchError(f"SEC returned non-JSON for {path}: {exc!r}") from exc

        if not isinstance(data, dict):
            raise SecFetchError(
                f"SEC returned unexpected payload for {path}: {type(data).__name__}"
            )
        return data


def _parse_date(value: Any) -> date | None:
    """Parse a YYYY-MM-DD string; return None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
