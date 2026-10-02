"""Data quality gate runner.

Loads each layer from its source, validates against the corresponding
Pandera schema, and produces a structured report. A single failure
anywhere makes the gate exit non-zero.

Two consumers:
    - CLI (quality.cli): human output + optional JSON
    - Orchestration (M7): programmatic GateReport for Prefect

Design:
    - Per-ticker validation for the raw layer: a bad ticker is reported
      with its name, not lost in a 30,000-row aggregate.
    - All-at-once for staging and marts: those are single SQL sources.
    - Errors are captured (not raised) so a full report is produced
      even when multiple layers fail.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import pandas as pd
import pandera.pandas as pa

from ingestion.config import Settings, get_settings
from quality.schemas import (
    FctReturnsSchema,
    RawPricesSchema,
    SecFactsSchema,
    StgPricesSchema,
)

__all__ = [
    "GateReport",
    "LayerResult",
    "discover_tickers_from_raw",
    "load_marts_returns",
    "load_raw_prices",
    "load_sec_facts",
    "load_staging_prices",
    "run_gate",
]


# ═══════════════════════════════════════════════════════════
# Result types
# ═══════════════════════════════════════════════════════════
@dataclass(frozen=True)
class LayerResult:
    """Outcome of validating one layer (or one partition of one layer)."""

    layer: str
    status: str  # "pass" | "fail" | "skip"
    n_rows: int = 0
    n_columns: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateReport:
    """Aggregate report for a gate run."""

    started_at: datetime
    finished_at: datetime
    results: list[LayerResult] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def failed(self) -> list[LayerResult]:
        return [r for r in self.results if r.status == "fail"]

    @property
    def passed(self) -> list[LayerResult]:
        return [r for r in self.results if r.status == "pass"]

    @property
    def exit_code(self) -> int:
        return 0 if not self.failed else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "n_pass": len(self.passed),
            "n_fail": len(self.failed),
            "results": [asdict(r) for r in self.results],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


# ═══════════════════════════════════════════════════════════
# Loaders
# ═══════════════════════════════════════════════════════════
def load_raw_prices(
    *,
    ticker: str,
    settings: Settings | None = None,
) -> pd.DataFrame:
    """Load one ticker's raw Parquet snapshot.

    Args:
        ticker: Symbol, e.g. "AAPL".
        settings: Optional Settings override.

    Returns:
        DataFrame with the raw layer's columns.

    Raises:
        FileNotFoundError: if no snapshot exists for the ticker.
    """
    settings = settings or get_settings()
    pattern = str(settings.prices_raw_dir / ticker.upper() / "*.parquet")
    con = duckdb.connect()
    try:
        query = f"SELECT * FROM read_parquet('{pattern}', union_by_name = true)"
        df = cast("pd.DataFrame", con.sql(query).fetchdf())
    finally:
        con.close()
    if df.empty:
        raise FileNotFoundError(f"No raw snapshot for {ticker}: {pattern}")
    return df


def discover_tickers_from_raw(*, settings: Settings | None = None) -> list[str]:
    """Return ticker symbols present in the raw data directory.

    Scans ``settings.prices_raw_dir`` for immediate subdirectories and
    treats each directory name as a ticker. Used in CI, where the raw
    layer is a small committed fixture rather than the full universe.

    Args:
        settings: Optional Settings override.

    Returns:
        Sorted list of ticker symbols (uppercase). Empty if the raw
        directory does not exist.
    """
    settings = settings or get_settings()
    root = settings.prices_raw_dir
    if not root.exists():
        return []
    return sorted(d.name.upper() for d in root.iterdir() if d.is_dir())


def load_staging_prices(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
) -> pd.DataFrame:
    """Load `staging.stg_prices` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return cast("pd.DataFrame", con.sql("SELECT * FROM staging.stg_prices").fetchdf())
    finally:
        con.close()


def load_marts_returns(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
) -> pd.DataFrame:
    """Load `marts.fct_returns_daily` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return cast("pd.DataFrame", con.sql("SELECT * FROM marts.fct_returns_daily").fetchdf())
    finally:
        con.close()


def load_sec_facts(
    *,
    root: Path | str = "./data/raw/fundamentals/sec",
) -> pd.DataFrame:
    """Load all SEC fundamental facts from the raw Parquet tree.

    Uses DuckDB to read the glob so we do not materialize each ticker
    separately.
    """
    pattern = f"{root}/*/*.parquet"
    con = duckdb.connect()
    try:
        df = cast(
            "pd.DataFrame",
            con.sql(f"SELECT * FROM read_parquet('{pattern}', union_by_name = true)").fetchdf(),
        )
    finally:
        con.close()
    if df.empty:
        raise FileNotFoundError(f"No SEC facts found: {pattern}")
    return df


# ═══════════════════════════════════════════════════════════
# Validator
# ═══════════════════════════════════════════════════════════
def _validate(
    *,
    layer: str,
    df: pd.DataFrame,
    schema: type[pa.DataFrameModel],
) -> LayerResult:
    """Validate one DataFrame, capturing errors into a LayerResult."""
    started = time.monotonic()
    try:
        schema.validate(df, lazy=False)
        return LayerResult(
            layer=layer,
            status="pass",
            n_rows=len(df),
            n_columns=len(df.columns),
            duration_ms=(time.monotonic() - started) * 1000,
        )
    except pa.errors.SchemaError as exc:
        return LayerResult(
            layer=layer,
            status="fail",
            n_rows=len(df),
            n_columns=len(df.columns),
            duration_ms=(time.monotonic() - started) * 1000,
            error=str(exc)[:2000],  # keep errors bounded in the report
            detail={"failure_cases": _failure_cases(exc)},
        )
    except pa.errors.SchemaErrors as exc:
        # Lazy=True collects multiple failures; we do not use it, but
        # guard for it in case a caller passes lazy=True upstream.
        return LayerResult(
            layer=layer,
            status="fail",
            n_rows=len(df),
            n_columns=len(df.columns),
            duration_ms=(time.monotonic() - started) * 1000,
            error=str(exc)[:2000],
            detail={"failure_cases": _failure_cases(exc)},
        )


def _failure_cases(exc: BaseException) -> list[dict[str, Any]]:
    """Extract a small, JSON-serialisable list of failure cases from exc."""
    fc = getattr(exc, "failure_cases", None)
    if fc is None:
        return []
    try:
        # failure_cases is a DataFrame in 0.19+; convert to records.
        if isinstance(fc, pd.DataFrame):
            records = fc.head(10).to_dict(orient="records")
            return cast("list[dict[str, Any]]", records)
    except Exception:
        return []
    return []


# ═══════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════
def run_gate(
    *,
    tickers: list[str] | None = None,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
    settings: Settings | None = None,
    skip: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> GateReport:
    """Run the full gate: raw (per ticker) + staging + marts.

    Args:
        tickers: Raw tickers to check. If None, uses the configured
            universe.
        warehouse_path: Path to the DuckDB warehouse for staging/marts.
        settings: Optional Settings override.
        skip: Layer names to skip ("raw", "staging", "marts").
        log: Optional callback for progress lines.

    Returns:
        A GateReport with one LayerResult per layer (raw: one per
        ticker) and aggregate exit_code.
    """
    started = datetime.now(tz=UTC)
    settings = settings or get_settings()
    skip = skip or set()

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    results: list[LayerResult] = []

    # ── Raw (per ticker) ─────────────────────────────────────
    if "raw" not in skip:
        if tickers is None:
            from ingestion.config import load_universe

            tickers = load_universe(settings.universe_file)

        for ticker in tickers:
            ticker = ticker.upper()
            try:
                df = load_raw_prices(ticker=ticker, settings=settings)
            except Exception as exc:
                results.append(
                    LayerResult(
                        layer=f"raw:{ticker}",
                        status="fail",
                        error=f"load failed: {type(exc).__name__}: {exc}",
                    )
                )
                _log(f"  raw:{ticker}  FAIL  (load)")
                continue
            res = _validate(layer=f"raw:{ticker}", df=df, schema=RawPricesSchema)
            results.append(res)
            _log(
                f"  {res.layer:20s} {res.status.upper():4s}  "
                f"rows={res.n_rows}  cols={res.n_columns}  "
                f"{res.duration_ms:.0f}ms"
            )

    # ── Staging (single SQL source) ──────────────────────────
    if "staging" not in skip:
        try:
            df = load_staging_prices(warehouse_path=warehouse_path)
            res = _validate(layer="staging:stg_prices", df=df, schema=StgPricesSchema)
        except Exception as exc:
            res = LayerResult(
                layer="staging:stg_prices",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        results.append(res)
        _log(
            f"  {res.layer:20s} {res.status.upper():4s}  "
            f"rows={res.n_rows}  cols={res.n_columns}  "
            f"{res.duration_ms:.0f}ms"
        )

    # ── Marts (single SQL source) ────────────────────────────
    if "marts" not in skip:
        try:
            df = load_marts_returns(warehouse_path=warehouse_path)
            res = _validate(layer="marts:fct_returns_daily", df=df, schema=FctReturnsSchema)
        except Exception as exc:
            res = LayerResult(
                layer="marts:fct_returns_daily",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        results.append(res)
        _log(
            f"  {res.layer:20s} {res.status.upper():4s}  "
            f"rows={res.n_rows}  cols={res.n_columns}  "
            f"{res.duration_ms:.0f}ms"
        )

    # ── SEC fundamentals (raw Parquet tree) ──────────────────
    if "sec" not in skip:
        try:
            df = load_sec_facts()
            res = _validate(layer="sec:sec_facts", df=df, schema=SecFactsSchema)
        except Exception as exc:
            res = LayerResult(
                layer="sec:sec_facts",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        results.append(res)
        _log(
            f"  {res.layer:20s} {res.status.upper():4s}  "
            f"rows={res.n_rows}  cols={res.n_columns}  "
            f"{res.duration_ms:.0f}ms"
        )

    finished = datetime.now(tz=UTC)
    return GateReport(started_at=started, finished_at=finished, results=results)
