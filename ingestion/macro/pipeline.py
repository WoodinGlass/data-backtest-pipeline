"""End-to-end macro ingestion pipeline.

Iterates the curated macro registry, fetches each series (vintage-aware),
and writes every vintage snapshot to the immutable raw layer.

Design mirrors the prices pipeline: pure orchestration, injectable
dependencies, per-series isolation, structured summary.

Usage:
    from ingestion.macro.pipeline import ingest_macro_universe

    summary = ingest_macro_universe()
    if summary.failed:
        print("Failures:", summary.failed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from ingestion.logging import bind_correlation_id, get_logger
from ingestion.macro.client import FredClient, MacroFetchError
from ingestion.macro.config import (
    MacroSeries,
    MacroSettings,
    get_macro_settings,
    load_macro_registry,
)
from ingestion.macro.raw_store import MacroRawStore

__all__ = [
    "MacroIngestResult",
    "MacroIngestSummary",
    "ingest_macro_series",
    "ingest_macro_universe",
]


log = get_logger(__name__)


@dataclass(frozen=True)
class MacroIngestResult:
    """Outcome of ingesting a single macro series."""

    series_id: str
    status: Literal["written", "skipped", "failed"]
    n_vintages_written: int = 0
    n_vintages_skipped: int = 0
    n_observations: int = 0
    error: str | None = None


@dataclass
class MacroIngestSummary:
    """Aggregate outcome of a macro ingestion run."""

    start_date: date
    end_date: date
    correlation_id: str
    results: list[MacroIngestResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[MacroIngestResult]:
        return [r for r in self.results if r.status == "written"]

    @property
    def skipped(self) -> list[MacroIngestResult]:
        return [r for r in self.results if r.status == "skipped"]

    @property
    def failed(self) -> list[MacroIngestResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def total_vintages_written(self) -> int:
        return sum(r.n_vintages_written for r in self.results)

    @property
    def total_observations(self) -> int:
        return sum(r.n_observations for r in self.results)

    @property
    def exit_code(self) -> int:
        return 0 if not self.failed else 1


def ingest_macro_series(
    series: MacroSeries,
    *,
    start: date,
    end: date | None,
    client: FredClient,
    store: MacroRawStore,
) -> MacroIngestResult:
    """Fetch, validate, and store all vintages of one macro series.

    Never raises: all errors are captured in the returned result.

    Args:
        series: The registry entry to ingest.
        start: Earliest observation_date.
        end: Latest observation_date, or None for "today".
        client: The FRED client.
        store: The raw store to write to.

    Returns:
        A :class:`MacroIngestResult` describing what happened.
    """
    try:
        snapshots = client.fetch_vintages(
            series.series_id,
            observation_start=start,
            observation_end=end,
        )
    except MacroFetchError as exc:
        log.error(
            "macro_ingest_fetch_failed",
            series_id=series.series_id,
            error=str(exc),
        )
        return MacroIngestResult(
            series_id=series.series_id,
            status="failed",
            error=str(exc),
        )
    except Exception as exc:
        log.exception("macro_ingest_unexpected_error", series_id=series.series_id)
        return MacroIngestResult(
            series_id=series.series_id,
            status="failed",
            error=repr(exc),
        )

    if not snapshots:
        log.warning("macro_ingest_empty", series_id=series.series_id)
        return MacroIngestResult(
            series_id=series.series_id,
            status="skipped",
            error="no observations returned",
        )

    n_written = 0
    n_skipped = 0
    n_obs = 0
    for snap in snapshots:
        try:
            res = store.write_snapshot(snap)
        except Exception as exc:
            log.exception(
                "macro_ingest_write_failed",
                series_id=series.series_id,
                vintage_date=str(snap.vintage_date),
            )
            return MacroIngestResult(
                series_id=series.series_id,
                status="failed",
                n_vintages_written=n_written,
                n_vintages_skipped=n_skipped,
                error=repr(exc),
            )
        if res.status == "written":
            n_written += 1
            n_obs += res.n_observations
        else:
            n_skipped += 1

    status: Literal["written", "skipped"] = "written" if n_written > 0 else "skipped"
    return MacroIngestResult(
        series_id=series.series_id,
        status=status,
        n_vintages_written=n_written,
        n_vintages_skipped=n_skipped,
        n_observations=n_obs,
    )


def ingest_macro_universe(
    *,
    start: date | None = None,
    end: date | None = None,
    client: FredClient | None = None,
    store: MacroRawStore | None = None,
    settings: MacroSettings | None = None,
    series: list[MacroSeries] | None = None,
    correlation_id: str | None = None,
) -> MacroIngestSummary:
    """Ingest the curated macro registry (or a provided subset).

    Args:
        start: Earliest observation_date. Defaults to
            ``settings.macro_history_start``.
        end: Latest observation_date. Defaults to None ("today").
        client: Optional FredClient override.
        store: Optional MacroRawStore override.
        settings: Optional MacroSettings override.
        series: Optional subset of the registry.
        correlation_id: Optional correlation id for this run.

    Returns:
        A :class:`MacroIngestSummary`.
    """
    settings = settings or get_macro_settings()
    start = start or settings.macro_history_start
    client = client or FredClient(settings=settings)
    store = store or MacroRawStore(settings=settings)
    series = series if series is not None else load_macro_registry()

    with bind_correlation_id(correlation_id, prefix="macro") as cid:
        log.info(
            "macro_ingest_start",
            n_series=len(series),
            start=str(start),
            end=str(end) if end else "auto",
        )
        results: list[MacroIngestResult] = []
        for s in series:
            results.append(ingest_macro_series(s, start=start, end=end, client=client, store=store))

        summary = MacroIngestSummary(
            start_date=start,
            end_date=end or date.today(),
            correlation_id=cid,
            results=results,
        )
        log.info(
            "macro_ingest_end",
            n_written_series=len(summary.succeeded),
            n_skipped_series=len(summary.skipped),
            n_failed_series=len(summary.failed),
            n_vintages_written=summary.total_vintages_written,
            n_observations=summary.total_observations,
        )
        return summary
