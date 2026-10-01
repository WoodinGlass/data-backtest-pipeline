"""Integration tests for the quality gate.

These tests validate the gate end-to-end against a real warehouse
and real Parquet files. Marked `integration` so the default pytest
run does not execute them.

Run with:
    pytest -m integration -v
"""

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from quality.gate import run_gate

pytestmark = pytest.mark.integration


# ─── helpers ────────────────────────────────────────────────
@pytest.fixture(scope="module")
def warehouse_path() -> Path:
    """Ensure the local warehouse exists before running."""
    path = Path("data/warehouse.duckdb")
    if not path.exists():
        pytest.skip("data/warehouse.duckdb not found; run `make dbt-build` first")
    return path


@pytest.fixture(scope="module")
def raw_dir() -> Path:
    """Ensure raw Parquet snapshots exist before running."""
    path = Path("data/raw/prices/yfinance")
    if not path.exists() or not list(path.glob("*/*.parquet")):
        pytest.skip("data/raw/prices/yfinance/ not populated; run `make ingest`")
    return path


# ═══════════════════════════════════════════════════════════
# Happy path
# ═══════════════════════════════════════════════════════════
def test_gate_passes_on_healthy_warehouse(warehouse_path: Path, raw_dir: Path) -> None:
    report = run_gate(
        tickers=["AAPL", "MSFT"],
        warehouse_path=warehouse_path,
        skip=set(),
    )
    # 2 raw + staging + marts = 4 results
    assert len(report.results) == 4
    assert report.exit_code == 0, [r.error for r in report.failed]
    assert len(report.passed) == 4


def test_gate_skip_raw_only_runs_staging_and_marts(
    warehouse_path: Path,
) -> None:
    report = run_gate(
        warehouse_path=warehouse_path,
        skip={"raw"},
    )
    assert len(report.results) == 2
    assert {r.layer for r in report.results} == {
        "staging:stg_prices",
        "marts:fct_returns_daily",
    }
    assert report.exit_code == 0


def test_gate_json_report_is_serialisable(warehouse_path: Path, raw_dir: Path) -> None:
    import json

    report = run_gate(
        tickers=["AAPL"],
        warehouse_path=warehouse_path,
    )
    parsed = json.loads(report.to_json())
    assert parsed["n_fail"] == 0
    assert parsed["n_pass"] == 3


# ═══════════════════════════════════════════════════════════
# Sad path: corrupt data must be caught
# ═══════════════════════════════════════════════════════════
def test_gate_catches_ohlc_violation_in_warehouse(warehouse_path: Path, tmp_path: Path) -> None:
    """Corrupt one row in a copy of the warehouse; gate must fail."""
    # Copy the warehouse to tmp
    from shutil import copy2

    tmp_wh = tmp_path / "corrupt.duckdb"
    copy2(warehouse_path, tmp_wh)

    # staging.stg_prices is a view; materialise it first, then corrupt.
    # See the note in scripts/demo_bad_data.py for the full rationale.
    con = duckdb.connect(str(tmp_wh))
    con.execute("""
        CREATE TABLE staging.stg_prices_corrupt AS
        SELECT * FROM staging.stg_prices
    """)
    con.execute("""
        UPDATE staging.stg_prices_corrupt
        SET low = open + 1000.0
        WHERE ticker = 'AAPL'
          AND trade_date = (
              SELECT MIN(trade_date) FROM staging.stg_prices_corrupt
          )
    """)
    con.execute("DROP VIEW staging.stg_prices")
    con.execute("ALTER TABLE staging.stg_prices_corrupt RENAME TO stg_prices")
    con.close()

    # Run gate — must fail on staging (and probably marts too, but we
    # only assert staging).
    report = run_gate(
        warehouse_path=tmp_wh,
        skip={"raw"},
    )
    assert report.exit_code == 1
    failed_layers = {r.layer for r in report.failed}
    assert "staging:stg_prices" in failed_layers
    # The error message must reference the check that failed.
    staging_error = next(r.error for r in report.failed if r.layer == "staging:stg_prices")
    assert "ohlc_invariants" in staging_error


def test_gate_catches_label_null_mismatch(warehouse_path: Path, tmp_path: Path) -> None:
    """Corrupt next_return_positive in a copy; gate must fail."""
    from shutil import copy2

    tmp_wh = tmp_path / "corrupt_label.duckdb"
    copy2(warehouse_path, tmp_wh)

    con = duckdb.connect(str(tmp_wh))
    # Set a non-null label on a row that has NULL next_log_return
    con.execute("""
        UPDATE marts.fct_returns_daily
        SET next_return_positive = true
        WHERE next_log_return IS NULL
    """)
    con.close()

    report = run_gate(
        warehouse_path=tmp_wh,
        skip={"raw", "staging"},
    )
    assert report.exit_code == 1
    failed = [r for r in report.failed if r.layer == "marts:fct_returns_daily"]
    assert len(failed) == 1
    assert "label_null_equivalence" in failed[0].error


def test_gate_catches_bad_raw_parquet(raw_dir: Path, tmp_path: Path) -> None:
    """Corrupt a raw Parquet file; gate must fail on the raw layer."""
    from shutil import copy2

    from ingestion.config import Settings

    # Set up a fake raw tree with only 2 tickers
    fake_raw = tmp_path / "raw"
    src_dir = raw_dir
    for ticker in ("AAPL", "MSFT"):
        (fake_raw / "prices" / "yfinance" / ticker).mkdir(parents=True)
        src_file = next((src_dir / ticker).glob("*.parquet"))
        copy2(src_file, fake_raw / "prices" / "yfinance" / ticker / "fixture.parquet")

    # Corrupt the AAPL file: set low = 99999
    aapl_file = fake_raw / "prices" / "yfinance" / "AAPL" / "fixture.parquet"
    df = pd.read_parquet(aapl_file)
    df.loc[0, "low"] = 99999.0
    df.to_parquet(aapl_file, index=False)

    # Build a Settings that points at the fake tree
    settings = Settings(
        price_source="yfinance",
        raw_data_dir=fake_raw,
    )

    # Warehouse doesn't matter here — skip staging/marts.
    report = run_gate(
        tickers=["AAPL", "MSFT"],
        settings=settings,
        skip={"staging", "marts"},
    )
    assert report.exit_code == 1
    failed_layers = {r.layer for r in report.failed}
    assert "raw:AAPL" in failed_layers
    assert "raw:MSFT" not in failed_layers  # only AAPL was corrupted
