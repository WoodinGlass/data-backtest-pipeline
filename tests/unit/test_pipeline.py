"""Unit tests for ingestion/pipeline.py — no network, no real client."""

from datetime import date
from typing import Any

import pandas as pd
import pytest

from ingestion.client import PriceFetchError
from ingestion.config import Settings
from ingestion.pipeline import (
    IngestResult,
    IngestSummary,
    ingest_ticker,
    ingest_universe,
)
from ingestion.raw_store import RawStore


# ─── helpers ────────────────────────────────────────────────
def _df(n: int = 3) -> pd.DataFrame:
    idx = pd.Index(
        [date(2024, 1, 2 + i) for i in range(n)],
        name="date",
    )
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [102.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [101.0 + i for i in range(n)],
            "adj_close": [101.0 + i for i in range(n)],
            "volume": [1000 * (i + 1) for i in range(n)],
        },
        index=idx,
    )


class FakeClient:
    """A PriceSource that returns a canned df or raises for named tickers."""

    def __init__(
        self,
        df: pd.DataFrame | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._df = df if df is not None else _df()
        self._errors = errors or {}
        self.calls: list[tuple[str, date, date]] = []

    def fetch(self, ticker: str, *, start: date, end: date) -> pd.DataFrame:
        self.calls.append((ticker, start, end))
        if ticker in self._errors:
            raise self._errors[ticker]
        return self._df.copy()


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(
        price_source="yfinance",
        raw_data_dir=tmp_path / "raw",
    )


@pytest.fixture
def store(settings: Settings) -> RawStore:
    return RawStore(settings=settings)


# ─── ingest_ticker ──────────────────────────────────────────
def test_ingest_ticker_writes_new_snapshot(store: RawStore) -> None:
    client = FakeClient()
    r = ingest_ticker(
        "AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        client=client,
        store=store,
    )
    assert r.status == "written"
    assert r.ticker == "AAPL"
    assert r.rows == 3
    assert r.hash is not None
    assert client.calls == [("AAPL", date(2024, 1, 1), date(2024, 2, 1))]


def test_ingest_ticker_is_idempotent(store: RawStore) -> None:
    client = FakeClient()
    r1 = ingest_ticker(
        "AAPL", start=date(2024, 1, 1), end=date(2024, 2, 1), client=client, store=store
    )
    r2 = ingest_ticker(
        "AAPL", start=date(2024, 1, 1), end=date(2024, 2, 1), client=client, store=store
    )
    assert r1.status == "written"
    assert r2.status == "skipped"
    assert r1.hash == r2.hash


def test_ingest_ticker_uppercases(store: RawStore) -> None:
    client = FakeClient()
    r = ingest_ticker(
        "aapl", start=date(2024, 1, 1), end=date(2024, 2, 1), client=client, store=store
    )
    assert r.ticker == "AAPL"
    assert client.calls[0][0] == "AAPL"  # normalized before fetch


def test_ingest_ticker_captures_fetch_error(store: RawStore) -> None:
    client = FakeClient(errors={"BAD": PriceFetchError("nope")})
    r = ingest_ticker(
        "BAD", start=date(2024, 1, 1), end=date(2024, 2, 1), client=client, store=store
    )
    assert r.status == "failed"
    assert r.error is not None
    assert "nope" in r.error


def test_ingest_ticker_does_not_raise_on_unexpected_error(store: RawStore) -> None:
    client = FakeClient(errors={"OOPS": RuntimeError("boom")})
    r = ingest_ticker(
        "OOPS", start=date(2024, 1, 1), end=date(2024, 2, 1), client=client, store=store
    )
    assert r.status == "failed"
    assert r.error is not None
    assert "boom" in r.error


# ─── ingest_universe ────────────────────────────────────────
def test_ingest_universe_all_success(settings: Settings) -> None:
    client = FakeClient()
    summary = ingest_universe(
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        client=client,
        settings=settings,
        tickers=["AAPL", "MSFT", "GOOGL"],
    )
    assert len(summary.succeeded) == 3
    assert len(summary.skipped) == 0
    assert len(summary.failed) == 0
    assert summary.total_rows == 9  # 3 tickers x 3 rows
    assert summary.exit_code == 0
    assert summary.correlation_id.startswith("ingest-")


def test_ingest_universe_isolates_failures(settings: Settings) -> None:
    client = FakeClient(errors={"MSFT": PriceFetchError("down")})
    summary = ingest_universe(
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        client=client,
        settings=settings,
        tickers=["AAPL", "MSFT", "GOOGL"],
    )
    assert len(summary.succeeded) == 2
    assert len(summary.failed) == 1
    assert summary.failed[0].ticker == "MSFT"
    assert summary.exit_code == 1


def test_ingest_universe_idempotent_on_rerun(settings: Settings) -> None:
    client = FakeClient()
    tickers = ["AAPL", "MSFT"]
    s1 = ingest_universe(
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        client=client,
        settings=settings,
        tickers=tickers,
    )
    s2 = ingest_universe(
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        client=client,
        settings=settings,
        tickers=tickers,
    )
    assert len(s1.succeeded) == 2
    assert len(s2.skipped) == 2
    assert s2.exit_code == 0


def test_ingest_universe_correlation_id_is_propagated(settings: Settings) -> None:
    client = FakeClient()
    summary = ingest_universe(
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        client=client,
        settings=settings,
        tickers=["AAPL"],
        correlation_id="ingest-test-9999",
    )
    assert summary.correlation_id == "ingest-test-9999"


# ─── IngestSummary properties ───────────────────────────────
def test_summary_classifies_results() -> None:
    s = IngestSummary(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 1),
        correlation_id="ingest-test",
        results=[
            IngestResult("A", "written", rows=10, hash="a"),
            IngestResult("B", "skipped", rows=0, hash="b"),
            IngestResult("C", "failed", error="x"),
        ],
    )
    assert [r.ticker for r in s.succeeded] == ["A"]
    assert [r.ticker for r in s.skipped] == ["B"]
    assert [r.ticker for r in s.failed] == ["C"]
    assert s.total_rows == 10
    assert s.exit_code == 1
