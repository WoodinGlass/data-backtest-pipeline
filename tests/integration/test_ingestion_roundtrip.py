"""Integration tests for the ingestion pipeline.

These hit the real network (yfinance). They are marked ``integration``
so the default ``pytest`` run does not execute them.

Run with:
    pytest -m integration -v
"""

from datetime import date

import pytest

from ingestion.client import PriceFetchError, YFinanceClient
from ingestion.config import Settings
from ingestion.pipeline import ingest_ticker, ingest_universe
from ingestion.raw_store import RawStore

pytestmark = pytest.mark.integration


# ─── helpers ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    tmp = tmp_path_factory.mktemp("integration_raw")
    return Settings(price_source="yfinance", raw_data_dir=tmp / "raw")


@pytest.fixture(scope="module")
def client() -> YFinanceClient:
    # rate_limit=0 to keep the test fast; retries capped at 3.
    return YFinanceClient(rate_limit_seconds=0.0, max_retries=3)


# ─── real yfinance ─────────────────────────────────────────
def test_fetch_real_aapl_returns_normalized_frame(client: YFinanceClient) -> None:
    df = client.fetch(
        "AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
    )
    assert len(df) > 5
    assert tuple(df.columns) == ("open", "high", "low", "close", "adj_close", "volume")
    assert df.index.name == "date"
    assert (df["high"] >= df["low"]).all()
    assert (df["volume"] >= 0).all()


def test_fetch_real_bad_ticker_raises(client: YFinanceClient) -> None:
    # A clearly invalid symbol that Yahoo will not resolve.
    with pytest.raises(PriceFetchError):
        client.fetch(
            "ZZZZNOPE999",
            start=date(2024, 1, 1),
            end=date(2024, 1, 15),
        )


# ─── end-to-end: fetch + write + read ─────────────────────
def test_roundtrip_fetch_write_read(client: YFinanceClient, settings: Settings) -> None:
    store = RawStore(settings=settings)
    r = ingest_ticker(
        "MSFT",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
        client=client,
        store=store,
    )
    assert r.status == "written", r.error
    assert r.rows > 5

    # Read back
    df = store.read_latest("MSFT")
    assert df is not None
    assert len(df) == r.rows
    assert df.index.name == "date"

    # Second call must skip (idempotent)
    r2 = ingest_ticker(
        "MSFT",
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
        client=client,
        store=store,
    )
    assert r2.status == "skipped"
    assert r2.hash == r.hash


def test_ingest_universe_smoke(client: YFinanceClient, settings: Settings) -> None:
    summary = ingest_universe(
        start=date(2024, 1, 1),
        end=date(2024, 1, 15),
        client=client,
        settings=settings,
        tickers=["AAPL", "MSFT", "GOOGL"],
    )
    assert len(summary.results) == 3
    assert len(summary.failed) == 0, [r.error for r in summary.failed]
    assert summary.exit_code == 0
    assert summary.total_rows > 15
