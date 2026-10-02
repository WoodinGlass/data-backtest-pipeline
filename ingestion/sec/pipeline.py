"""End-to-end SEC fundamental ingestion pipeline.

Iterates the equity universe, fetches each ticker's companyfacts
from SEC EDGAR, writes an immutable raw Parquet snapshot.

Design mirrors the prices and macro pipelines: pure orchestration,
injectable dependencies, per-ticker isolation, structured summary.

Usage:
    from ingestion.sec.pipeline import ingest_sec_universe

    summary = ingest_sec_universe()
    if summary.failed:
        print("Failures:", summary.failed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from ingestion.config import Settings, get_settings, load_universe
from ingestion.logging import bind_correlation_id, get_logger
from ingestion.sec.client import SecClient, SecFetchError
from ingestion.sec.config import (
    SecSettings,
    get_sec_settings,
    load_sec_skip_tickers,
)
from ingestion.sec.raw_store import SecRawStore

__all__ = [
    "SecIngestResult",
    "SecIngestSummary",
    "ingest_sec_ticker",
    "ingest_sec_universe",
]


log = get_logger(__name__)


@dataclass(frozen=True)
class SecIngestResult:
    """Outcome of ingesting a single ticker's fundamentals."""

    ticker: str
    status: Literal["written", "skipped", "failed"]
    cik: int | None = None
    n_facts: int = 0
    n_non_null: int = 0
    error: str | None = None


@dataclass
class SecIngestSummary:
    """Aggregate outcome of a SEC ingestion run."""

    started_at: datetime
    finished_at: datetime
    correlation_id: str
    results: list[SecIngestResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SecIngestResult]:
        return [r for r in self.results if r.status == "written"]

    @property
    def skipped(self) -> list[SecIngestResult]:
        return [r for r in self.results if r.status == "skipped"]

    @property
    def failed(self) -> list[SecIngestResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def total_facts(self) -> int:
        return sum(r.n_facts for r in self.results if r.status == "written")

    @property
    def exit_code(self) -> int:
        return 0 if not self.failed else 1


def ingest_sec_ticker(
    ticker: str,
    *,
    client: SecClient,
    store: SecRawStore,
) -> SecIngestResult:
    """Fetch, parse, and store one ticker's fundamental facts.

    Never raises: all errors are captured in the returned result.

    Args:
        ticker: Symbol, e.g. "AAPL".
        client: The SEC client.
        store: The raw store to write to.

    Returns:
        A :class:`SecIngestResult` describing what happened.
    """
    ticker = ticker.upper()
    try:
        snapshot = client.fetch_company_facts(ticker)
    except SecFetchError as exc:
        log.error("sec_ingest_fetch_failed", ticker=ticker, error=str(exc))
        return SecIngestResult(ticker=ticker, status="failed", error=str(exc))
    except Exception as exc:
        log.exception("sec_ingest_unexpected_error", ticker=ticker)
        return SecIngestResult(ticker=ticker, status="failed", error=repr(exc))

    try:
        res = store.write_snapshot(snapshot)
    except Exception as exc:
        log.exception("sec_ingest_write_failed", ticker=ticker)
        return SecIngestResult(ticker=ticker, status="failed", error=repr(exc))

    return SecIngestResult(
        ticker=ticker,
        status=res.status,
        cik=res.cik,
        n_facts=res.n_facts,
        n_non_null=res.n_non_null,
    )


def ingest_sec_universe(
    *,
    client: SecClient | None = None,
    store: SecRawStore | None = None,
    settings: Settings | None = None,
    sec_settings: SecSettings | None = None,
    tickers: list[str] | None = None,
    correlation_id: str | None = None,
    only_missing: bool = False,
) -> SecIngestSummary:
    """Ingest fundamentals for the universe (or a provided subset).

    Args:
        client: Optional SecClient override.
        store: Optional SecRawStore override.
        settings: Optional main Settings override.
        sec_settings: Optional SecSettings override.
        tickers: Optional subset.
        correlation_id: Optional correlation id.
        only_missing: If True, skip tickers that already have a snapshot.

    Returns:
        A :class:`SecIngestSummary`.
    """
    settings = settings or get_settings()
    sec_settings = sec_settings or get_sec_settings()
    client = client or SecClient(settings=sec_settings)
    store = store or SecRawStore(settings=sec_settings)
    tickers = tickers if tickers is not None else load_universe(settings.universe_file)

    # Drop tickers that are not SEC-reporting companies (e.g. ETFs).
    skip_set = load_sec_skip_tickers()
    skipped_tickers = [t for t in tickers if t.upper() in skip_set]
    tickers = [t for t in tickers if t.upper() not in skip_set]
    if skipped_tickers:
        log.info(
            "sec_skip_filter",
            skipped=skipped_tickers,
            remaining=len(tickers),
        )

    if only_missing:
        before = len(tickers)
        tickers = [t for t in tickers if not store.list_snapshots(t)]
        log.info(
            "sec_only_missing_filter",
            before=before,
            after=len(tickers),
            skipped=before - len(tickers),
        )

    started = datetime.now(tz=UTC)
    with bind_correlation_id(correlation_id, prefix="sec") as cid:
        log.info("sec_ingest_start", n_tickers=len(tickers))
        results: list[SecIngestResult] = []
        for t in tickers:
            results.append(ingest_sec_ticker(t, client=client, store=store))
        finished = datetime.now(tz=UTC)

        summary = SecIngestSummary(
            started_at=started,
            finished_at=finished,
            correlation_id=cid,
            results=results,
        )
        log.info(
            "sec_ingest_end",
            written=len(summary.succeeded),
            skipped=len(summary.skipped),
            failed=len(summary.failed),
            total_facts=summary.total_facts,
        )
        return summary
