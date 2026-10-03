"""Data quality gate runner.

Loads each layer from its source, validates against the corresponding
Pandera schema, and produces a structured report. A single failure
anywhere makes the gate exit non-zero.

Two consumers:
    - CLI (quality.cli): human output + optional JSON
    - Orchestration (M7): programmatic GateReport for Prefect

Design:
    - Per-ticker validation for the raw prices layer: a bad ticker is
      reported with its name, not lost in a 30,000-row aggregate.
    - All-at-once for warehouse layers.
    - SQL-level sampling for very large tables (see SAMPLING_SIZE):
      the full row count is still reported, but validation runs on a
      random subset to keep the gate fast.
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
    IntFundamentalsPitSchema,
    IntMacroDailySchema,
    RawPricesSchema,
    SecFactsSchema,
    StgMacroSeriesSchema,
    StgPricesSchema,
    StgSecFactsSchema,
)

__all__ = [
    "GateReport",
    "LayerResult",
    "discover_tickers_from_raw",
    "load_int_fundamentals",
    "load_int_macro",
    "load_marts_returns",
    "load_raw_prices",
    "load_sec_facts",
    "load_staging_prices",
    "load_stg_macro",
    "load_stg_sec",
    "run_gate",
]


# ═══════════════════════════════════════════════════════════
# Result types
# ═══════════════════════════════════════════════════════════
@dataclass(frozen=True)
class LayerResult:
    """Outcome of validating one layer (or one partition of one layer).

    Attributes:
        n_rows: Total rows in the layer (not just the validated sample).
        n_sampled: Rows actually validated. Equals n_rows when no
            sampling was applied.
    """

    layer: str
    status: str  # "pass" | "fail" | "skip"
    n_rows: int = 0
    n_columns: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    n_sampled: int = 0


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
def _sample_sql(
    con: duckdb.DuckDBPyConnection,
    *,
    source_sql: str,
    count_sql: str,
    sample_rows: int | None,
) -> pd.DataFrame:
    """Helper: return full table or SQL-level sample with n_total in attrs."""
    if sample_rows is not None:
        row = con.sql(count_sql).fetchone()
        n = int(row[0]) if row is not None else 0
        if n > sample_rows:
            df = cast(
                "pd.DataFrame",
                con.sql(
                    f"SELECT * FROM ({source_sql}) USING SAMPLE {sample_rows} ROWS (reservoir, 42)"
                ).fetchdf(),
            )
            df.attrs["n_total"] = int(n)
            return df
    df = cast("pd.DataFrame", con.sql(source_sql).fetchdf())
    df.attrs["n_total"] = len(df)
    return df


def discover_tickers_from_raw(*, settings: Settings | None = None) -> list[str]:
    """Return ticker symbols present in the raw data directory."""
    settings = settings or get_settings()
    root = settings.prices_raw_dir
    if not root.exists():
        return []
    return sorted(d.name.upper() for d in root.iterdir() if d.is_dir())


def load_raw_prices(
    *,
    ticker: str,
    settings: Settings | None = None,
) -> pd.DataFrame:
    """Load one ticker's raw Parquet snapshot."""
    settings = settings or get_settings()
    pattern = str(settings.prices_raw_dir / ticker.upper() / "*.parquet")
    con = duckdb.connect()
    try:
        df = cast(
            "pd.DataFrame",
            con.sql(f"SELECT * FROM read_parquet('{pattern}', union_by_name = true)").fetchdf(),
        )
    finally:
        con.close()
    if df.empty:
        raise FileNotFoundError(f"No raw snapshot for {ticker}: {pattern}")
    return df


def load_staging_prices(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
) -> pd.DataFrame:
    """Load `staging.stg_prices` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return cast(
            "pd.DataFrame",
            con.sql("SELECT * FROM staging.stg_prices").fetchdf(),
        )
    finally:
        con.close()


def load_marts_returns(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
) -> pd.DataFrame:
    """Load `marts.fct_returns_daily` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return cast(
            "pd.DataFrame",
            con.sql("SELECT * FROM marts.fct_returns_daily").fetchdf(),
        )
    finally:
        con.close()


def load_sec_facts(
    *,
    root: Path | str = "./data/raw/fundamentals/sec",
    sample_rows: int | None = None,
) -> pd.DataFrame:
    """Load SEC fundamental facts from the raw Parquet tree."""
    pattern = f"{root}/*/*.parquet"
    con = duckdb.connect()
    try:
        df = _sample_sql(
            con,
            source_sql=(f"SELECT * FROM read_parquet('{pattern}', union_by_name = true)"),
            count_sql=(f"SELECT COUNT(*) FROM read_parquet('{pattern}', union_by_name = true)"),
            sample_rows=sample_rows,
        )
    finally:
        con.close()
    if df.empty:
        raise FileNotFoundError(f"No SEC facts found: {pattern}")
    return df


def load_stg_macro(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
    sample_rows: int | None = None,
) -> pd.DataFrame:
    """Load `staging.stg_macro_series` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return _sample_sql(
            con,
            source_sql="SELECT * FROM staging.stg_macro_series",
            count_sql="SELECT COUNT(*) FROM staging.stg_macro_series",
            sample_rows=sample_rows,
        )
    finally:
        con.close()


def load_int_macro(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
    sample_rows: int | None = None,
) -> pd.DataFrame:
    """Load `intermediate.int_macro_daily` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return _sample_sql(
            con,
            source_sql="SELECT * FROM intermediate.int_macro_daily",
            count_sql="SELECT COUNT(*) FROM intermediate.int_macro_daily",
            sample_rows=sample_rows,
        )
    finally:
        con.close()


def load_stg_sec(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
    sample_rows: int | None = None,
) -> pd.DataFrame:
    """Load `staging.stg_sec_facts` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return _sample_sql(
            con,
            source_sql="SELECT * FROM staging.stg_sec_facts",
            count_sql="SELECT COUNT(*) FROM staging.stg_sec_facts",
            sample_rows=sample_rows,
        )
    finally:
        con.close()


def load_int_fundamentals(
    *,
    warehouse_path: Path | str = "./data/warehouse.duckdb",
    sample_rows: int | None = None,
) -> pd.DataFrame:
    """Load `intermediate.int_fundamentals_pit` from the warehouse."""
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return _sample_sql(
            con,
            source_sql="SELECT * FROM intermediate.int_fundamentals_pit",
            count_sql="SELECT COUNT(*) FROM intermediate.int_fundamentals_pit",
            sample_rows=sample_rows,
        )
    finally:
        con.close()


# ═══════════════════════════════════════════════════════════
# Validator
# ═══════════════════════════════════════════════════════════
# Sampling thresholds for large tables. Above SAMPLING_THRESHOLD rows
# the loader samples at the SQL level (see _sample_sql).
SAMPLING_THRESHOLD = 500_000
SAMPLING_SIZE = 200_000


def _failure_cases(exc: BaseException) -> list[dict[str, Any]]:
    """Extract a small, JSON-serialisable list of failure cases from exc."""
    fc = getattr(exc, "failure_cases", None)
    if fc is None:
        return []
    try:
        if isinstance(fc, pd.DataFrame):
            records = fc.head(10).to_dict(orient="records")
            return cast("list[dict[str, Any]]", records)
    except Exception:
        return []
    return []


def _validate(
    *,
    layer: str,
    df: pd.DataFrame,
    schema: type[pa.DataFrameModel],
) -> LayerResult:
    """Validate one DataFrame, capturing errors into a LayerResult."""
    started = time.monotonic()
    n_total = int(df.attrs.get("n_total", len(df)))
    n_sampled = len(df)
    try:
        schema.validate(df, lazy=False)
        return LayerResult(
            layer=layer,
            status="pass",
            n_rows=n_total,
            n_columns=len(df.columns),
            duration_ms=(time.monotonic() - started) * 1000,
            n_sampled=n_sampled,
        )
    except (pa.errors.SchemaError, pa.errors.SchemaErrors) as exc:
        return LayerResult(
            layer=layer,
            status="fail",
            n_rows=n_total,
            n_columns=len(df.columns),
            duration_ms=(time.monotonic() - started) * 1000,
            error=str(exc)[:2000],
            detail={"failure_cases": _failure_cases(exc), "n_sampled": n_sampled},
            n_sampled=n_sampled,
        )


def _emit(
    results: list[LayerResult],
    res: LayerResult,
    log: Callable[[str], None],
) -> None:
    """Append a result and log a compact line."""
    results.append(res)
    suffix = (
        f"  (sampled {res.n_sampled:,})" if res.n_sampled and res.n_sampled < res.n_rows else ""
    )
    log(
        f"  {res.layer:26s} {res.status.upper():4s}  "
        f"rows={res.n_rows:>10,}  cols={res.n_columns:>2d}  "
        f"{res.duration_ms:6.0f}ms{suffix}"
    )


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
    """Run the full gate across every configured layer.

    Args:
        tickers: Raw tickers to check. If None, uses the configured
            universe.
        warehouse_path: Path to the DuckDB warehouse.
        settings: Optional Settings override.
        skip: Layer names to skip. See CLI --help for the full list.
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

    # ── Raw prices (per ticker) ──────────────────────────────
    if "raw" not in skip:
        if tickers is None:
            from ingestion.config import load_universe

            tickers = load_universe(settings.universe_file)

        for ticker in tickers:
            ticker = ticker.upper()
            try:
                df = load_raw_prices(ticker=ticker, settings=settings)
            except Exception as exc:
                _emit(
                    results,
                    LayerResult(
                        layer=f"raw:{ticker}",
                        status="fail",
                        error=f"load failed: {type(exc).__name__}: {exc}",
                    ),
                    _log,
                )
                continue
            _emit(
                results,
                _validate(layer=f"raw:{ticker}", df=df, schema=RawPricesSchema),
                _log,
            )

    # ── Prices staging + marts ───────────────────────────────
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
        _emit(results, res, _log)

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
        _emit(results, res, _log)

    # ── Raw SEC facts ────────────────────────────────────────
    if "sec" not in skip:
        try:
            df = load_sec_facts(sample_rows=SAMPLING_SIZE)
            res = _validate(layer="sec:sec_facts", df=df, schema=SecFactsSchema)
        except Exception as exc:
            res = LayerResult(
                layer="sec:sec_facts",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        _emit(results, res, _log)

    # ── Macro staging + intermediate ─────────────────────────
    if "stg_macro" not in skip:
        try:
            df = load_stg_macro(warehouse_path=warehouse_path, sample_rows=SAMPLING_SIZE)
            res = _validate(
                layer="stg_macro:stg_macro_series",
                df=df,
                schema=StgMacroSeriesSchema,
            )
        except Exception as exc:
            res = LayerResult(
                layer="stg_macro:stg_macro_series",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        _emit(results, res, _log)

    if "int_macro" not in skip:
        try:
            df = load_int_macro(warehouse_path=warehouse_path, sample_rows=SAMPLING_SIZE)
            res = _validate(
                layer="int_macro:int_macro_daily",
                df=df,
                schema=IntMacroDailySchema,
            )
        except Exception as exc:
            res = LayerResult(
                layer="int_macro:int_macro_daily",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        _emit(results, res, _log)

    # ── SEC staging + intermediate ───────────────────────────
    if "stg_sec" not in skip:
        try:
            df = load_stg_sec(warehouse_path=warehouse_path, sample_rows=SAMPLING_SIZE)
            res = _validate(
                layer="stg_sec:stg_sec_facts",
                df=df,
                schema=StgSecFactsSchema,
            )
        except Exception as exc:
            res = LayerResult(
                layer="stg_sec:stg_sec_facts",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        _emit(results, res, _log)

    if "int_fundamentals" not in skip:
        try:
            df = load_int_fundamentals(warehouse_path=warehouse_path, sample_rows=SAMPLING_SIZE)
            res = _validate(
                layer="int_fundamentals:int_fundamentals_pit",
                df=df,
                schema=IntFundamentalsPitSchema,
            )
        except Exception as exc:
            res = LayerResult(
                layer="int_fundamentals:int_fundamentals_pit",
                status="fail",
                error=f"load failed: {type(exc).__name__}: {exc}",
            )
        _emit(results, res, _log)

    finished = datetime.now(tz=UTC)
    return GateReport(started_at=started, finished_at=finished, results=results)
