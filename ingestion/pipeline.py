"""End-to-end ingestion pipeline.

Combines the price client and the raw store into a single operation:
fetch a universe, validate each ticker, write immutable snapshots,
return a summary that orchestration (Prefect, cron, CLI) can act on.

Design:
- **Pure orchestration.** No hidden state. Everything injectable for
  tests: ``client``, ``store``, ``log``.
- **Per-ticker isolation.** A failure on one ticker does not abort the
  run; it is recorded in the summary and surfaces in the exit code.
- **Idempotent.** Safe to run repeatedly. Snapshots with identical
  content are skipped (see :class:`~ingestion.raw_store.RawStore`).
- **One correlation ID per run**, attached to every log line.

Usage:
    from datetime import date
    from ingestion.pipeline import ingest_universe

    summary = ingest_universe(start=date(2024, 1, 1), end=date(2024, 2, 1))
    if summary.failed:
        print("Failures:", summary.failed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from ingestion.client import PriceFetchError, PriceSource, YFinanceClient
from ingestion.config import Settings, get_settings, load_universe
from ingestion.logging import bind_correlation_id, get_logger
from ingestion.raw_store import RawStore

__all__ = [
    "IngestResult",
    "IngestSummary",
    "ingest_ticker",
    "ingest_universe",
]


log = get_logger(__name__)


# ────────────────────────────────────────────────────────────
# Result types
# ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IngestResult:
    """Outcome of ingesting a single ticker."""

    ticker: str
    status: Literal["written", "skipped", "failed"]
    rows: int = 0
    hash: str | None = None
    error: str | None = None


@dataclass
class IngestSummary:
    """Aggregate outcome of an ingestion run."""

    start_date: date
    end_date: date
    correlation_id: str
    results: list[IngestResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[IngestResult]:
        """Tickers that were written (new snapshot)."""
        return [r for r in self.results if r.status == "written"]

    @property
    def skipped(self) -> list[IngestResult]:
        """Tickers whose content was unchanged (idempotent skip)."""
        return [r for r in self.results if r.status == "skipped"]

    @property
    def failed(self) -> list[IngestResult]:
        """Tickers that failed after retries."""
        return [r for r in self.results if r.status == "failed"]

    @property
    def total_rows(self) -> int:
        return sum(r.rows for r in self.results if r.status == "written")

    @property
    def exit_code(self) -> int:
        """0 if no failures, 1 otherwise. Suitable for ``sys.exit``."""
        return 0 if not self.failed else 1


# ────────────────────────────────────────────────────────────
# Single-ticker ingestion
# ────────────────────────────────────────────────────────────
def ingest_ticker(
    ticker: str,
    *,
    start: date,
    end: date,
    client: PriceSource,
    store: RawStore,
) -> IngestResult:
    """Fetch, validate, and store one ticker's snapshot.

    Never raises: all errors are captured in the returned result so
    that a caller can continue with other tickers.

    Args:
        ticker: Symbol, e.g. ``"AAPL"``.
        start: Inclusive start date.
        end: Exclusive end date.
        client: Any object satisfying the :class:`PriceSource` Protocol.
        store: The raw store to write to.

    Returns:
        An :class:`IngestResult` describing what happened.
    """
    ticker = ticker.upper()
    try:
        df = client.fetch(ticker, start=start, end=end)
    except PriceFetchError as exc:
        log.error("ingest_fetch_failed", ticker=ticker, error=str(exc))
        return IngestResult(ticker=ticker, status="failed", error=str(exc))
    except Exception as exc:
        log.exception("ingest_unexpected_error", ticker=ticker)
        return IngestResult(ticker=ticker, status="failed", error=repr(exc))

    try:
        write = store.write_snapshot(ticker, df)
    except Exception as exc:
        log.exception("ingest_write_failed", ticker=ticker)
        return IngestResult(ticker=ticker, status="failed", error=repr(exc))

    return IngestResult(
        ticker=ticker,
        status=write.status,  # "written" | "skipped"
        rows=write.rows,
        hash=write.hash,
    )


# ────────────────────────────────────────────────────────────
# Universe ingestion
# ────────────────────────────────────────────────────────────
def ingest_universe(
    *,
    start: date | None = None,
    end: date | None = None,
    client: PriceSource | None = None,
    store: RawStore | None = None,
    settings: Settings | None = None,
    tickers: list[str] | None = None,
    correlation_id: str | None = None,
) -> IngestSummary:
    """Ingest a list of tickers (default: the configured universe).

    All dependencies are injectable. Defaults are lazy so that this
    function can be imported without touching the network.

    Args:
        start: Inclusive start date; defaults to ``settings.price_history_start``.
        end: Exclusive end date; defaults to today + 1 (so today is included).
        client: Price source; defaults to :class:`YFinanceClient`.
        store: Raw store; defaults to :class:`RawStore`.
        settings: Override for :func:`get_settings`.
        tickers: Override the universe.
        correlation_id: Explicit correlation ID for this run.

    Returns:
        An :class:`IngestSummary` with one entry per ticker.
    """
    from datetime import timedelta

    settings = settings or get_settings()
    start = start or settings.price_history_start
    end = end if end is not None else date.today() + timedelta(days=1)
    client = client or YFinanceClient()
    store = store or RawStore(settings=settings)
    tickers = tickers if tickers is not None else load_universe()

    with bind_correlation_id(correlation_id, prefix="ingest") as cid:
        log.info(
            "ingest_run_start",
            n_tickers=len(tickers),
            start=str(start),
            end=str(end),
        )
        results: list[IngestResult] = []
        for ticker in tickers:
            results.append(ingest_ticker(ticker, start=start, end=end, client=client, store=store))

        summary = IngestSummary(
            start_date=start,
            end_date=end,
            correlation_id=cid,
            results=results,
        )
        log.info(
            "ingest_run_end",
            written=len(summary.succeeded),
            skipped=len(summary.skipped),
            failed=len(summary.failed),
            rows=summary.total_rows,
        )
        return summary
