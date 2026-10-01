"""Demonstrate the quality gate catching bad data.

Copies the real warehouse to a temp location, introduces three kinds
of corruption, and runs the gate against each. The output is meant to
be read top-to-bottom as a story:

    1. Healthy state       -> gate passes
    2. OHLC violation      -> gate fails on staging
    3. Label NULL mismatch -> gate fails on marts
    4. All restored        -> gate passes again

Nothing in the real warehouse or the real raw layer is touched.

Usage:
    python scripts/demo_bad_data.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import duckdb

from quality.gate import run_gate

REAL_WH = Path("data/warehouse.duckdb")


def _header(msg: str) -> None:
    print()
    print("=" * 70)
    print(f"  {msg}")
    print("=" * 70)


def _run_and_report(wh: Path, *, label: str) -> bool:
    """Run the gate on `wh`; print a compact summary. Return True if pass."""
    report = run_gate(warehouse_path=wh, skip={"raw"})
    n_pass = len(report.passed)
    n_fail = len(report.failed)
    verdict = "PASS" if report.exit_code == 0 else "FAIL"
    print(f"  [{verdict}]  {label}:  {n_pass} pass, {n_fail} fail")
    for r in report.failed:
        first_line = (r.error or "").splitlines()[0][:100]
        print(f"          {r.layer}")
        print(f"          └─ {first_line}")
    return report.exit_code == 0


def main() -> int:
    if not REAL_WH.exists():
        print(f"ERROR: {REAL_WH} not found. Run `make dbt-build` first.")
        return 1

    with tempfile.TemporaryDirectory(prefix="demo_bad_data_") as tmp:
        tmp_dir = Path(tmp)
        tmp_wh = tmp_dir / "warehouse.duckdb"
        shutil.copy2(REAL_WH, tmp_wh)

        # ── 1) Healthy ─────────────────────────────────────
        _header("1. Healthy warehouse (baseline)")
        healthy = _run_and_report(tmp_wh, label="baseline")
        assert healthy, "baseline must pass before demo continues"

        # ── 2) Corrupt OHLC in staging ─────────────────────
        _header("2. Corrupt OHLC in staging.stg_prices (low > open on one row)")
        # staging.stg_prices is a VIEW. To introduce corruption we must
        # materialise it as a table first. Steps:
        #   1. CREATE TABLE stg_prices_corrupt AS SELECT * FROM view
        #   2. UPDATE the table (now allowed)
        #   3. DROP VIEW, RENAME table to the original name
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
        caught = not _run_and_report(tmp_wh, label="OHLC violation")
        assert caught, "gate must catch OHLC violation"

        # ── 3) Restore, then corrupt label in marts ────────
        _header("3. Restore warehouse, then corrupt label in marts")
        shutil.copy2(REAL_WH, tmp_wh)
        con = duckdb.connect(str(tmp_wh))
        con.execute("""
            UPDATE marts.fct_returns_daily
            SET next_return_positive = true
            WHERE next_log_return IS NULL
        """)
        con.close()
        caught = not _run_and_report(tmp_wh, label="label NULL mismatch")
        assert caught, "gate must catch label NULL mismatch"

        # ── 4) Restore, verify pass again ──────────────────
        _header("4. Restore warehouse (verify clean state)")
        shutil.copy2(REAL_WH, tmp_wh)
        healthy = _run_and_report(tmp_wh, label="restored")
        assert healthy, "restored warehouse must pass"

    print()
    print("=" * 70)
    print("  ✅  Demo complete: gate passed on healthy data and failed")
    print("      loudly on each corruption, with actionable messages.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
