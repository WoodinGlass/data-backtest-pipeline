"""Streamlit dashboard for the pipeline. ADR 0019 section 8-9.

Four tabs: Health, Backtest, Drift, About.

Run locally:  streamlit run app/streamlit_app.py
or via:       make app

The dashboard reads from monitoring.* and from the latest
backtest artifact directory. It never reads Parquet directly,
so there is exactly one place that parses each input.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from monitoring.config import MonitoringSettings
from monitoring.report import build_report, load_latest_run

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_TTL_SECONDS = 600


def _resolve_roots() -> tuple[Path, Path, Path, bool]:
    """Return (warehouse, features, backtest_dir, is_demo).

    Prefers data/ if it exists; falls back to data_demo/. The two
    layouts differ:
      - real data/:    warehouse.duckdb (file), features/v1/features_daily.parquet
      - demo data_demo/: warehouse_demo/ (dir of per-mart Parquet),
                          features_demo.parquet
    See ADR 0020 section 2.
    """
    real = REPO_ROOT / "data"
    demo = REPO_ROOT / "data_demo"
    if real.exists():
        return (
            real / "warehouse.duckdb",
            real / "features" / "v1" / "features_daily.parquet",
            real / "backtest",
            False,
        )
    return (
        demo / "warehouse_demo",
        demo / "features_demo.parquet",
        demo / "backtest",
        True,
    )


# ---------------------------------------------------------------------
# Cached loaders
# ---------------------------------------------------------------------


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Building monitoring report...")
def get_report(reference_date: str | None = None) -> dict[str, Any]:
    """Build the monitoring report (cached)."""
    warehouse, features, backtest_dir, _is_demo = _resolve_roots()
    settings = MonitoringSettings(
        reference_date=reference_date,
        warehouse_path=str(warehouse),
        features_path=str(features),
        backtest_dir=str(backtest_dir),
    )
    return build_report(settings=settings, repo_root=REPO_ROOT)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Loading backtest run...")
def get_latest_run_dir() -> str | None:
    """Return the latest backtest run directory, or None."""
    _w, _f, backtest_dir, _is_demo = _resolve_roots()
    run_dir = load_latest_run(backtest_dir)
    return str(run_dir) if run_dir else None


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner="Loading metrics...")
def get_metrics_table(run_dir: str) -> pd.DataFrame:
    """Load per-fold metrics.parquet from a run directory."""
    path = Path(run_dir) / "metrics.parquet"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def _status_badge(status: str) -> str:
    """Markdown colour badge for a status string."""
    s = (status or "").upper()
    if s == "PASS":
        return ":green[PASS]"
    if s == "WARN":
        return ":orange[WARN]"
    if s == "FAIL":
        return ":red[FAIL]"
    return s or "-"


# ---------------------------------------------------------------------
# Tab: Health
# ---------------------------------------------------------------------


def render_health_tab(report: dict[str, Any]) -> None:
    """Health tab: overall status, freshness, latest run."""
    st.subheader("Pipeline health")
    overall = report.get("overall", "?")
    generated = report.get("generated_at", "?")
    st.markdown(f"**Overall:** {_status_badge(overall)}  *(generated {generated})*")

    st.markdown("### Freshness")
    fr = report.get("freshness", {})
    st.markdown(f"Overall: {_status_badge(fr.get('overall', '?'))}")
    marts = fr.get("marts", [])
    if marts:
        df = pd.DataFrame(
            [
                {
                    "mart": m.get("mart"),
                    "status": m.get("status"),
                    "age_days": m.get("age_days"),
                    "rows": m.get("rows"),
                    "warn_days": m.get("warn_days"),
                    "fail_days": m.get("fail_days"),
                    "latest_date": m.get("latest_date"),
                }
                for m in marts
            ]
        )
        st.dataframe(df, width="stretch", hide_index=True)
    else:
        st.info("No marts to display.")

    st.markdown("### Latest backtest run")
    run_dir = get_latest_run_dir()
    if run_dir:
        st.code(run_dir, language=None)
    else:
        st.warning("No backtest run found.")

    errs = report.get("errors", [])
    if errs:
        st.markdown("### Errors")
        msg = "Some sections failed to build. See the JSON report for details."
        st.warning(msg)
        st.json(errs)


# ---------------------------------------------------------------------
# Tab: Backtest
# ---------------------------------------------------------------------


def render_backtest_tab() -> None:
    """Backtest tab: latest run config, tables, and plots."""
    st.subheader("Latest backtest")
    run_dir = get_latest_run_dir()
    if not run_dir:
        st.warning("No backtest run found under data/backtest/.")
        return

    st.caption(f"Run directory: `{run_dir}`")

    col1, col2 = st.columns(2)
    with col1:
        cfg_path = Path(run_dir) / "config.json"
        if cfg_path.exists():
            with st.expander("config.json"):
                st.json(json.loads(cfg_path.read_text()))
    with col2:
        sm_path = Path(run_dir) / "summary.json"
        if sm_path.exists():
            with st.expander("summary.json"):
                st.json(json.loads(sm_path.read_text()))

    pooled_path = Path(run_dir) / "table_pooled.parquet"
    if pooled_path.exists():
        st.markdown("### Pooled metrics")
        tbl = pd.read_parquet(pooled_path)
        st.dataframe(tbl, width="stretch", hide_index=True)

    deflated_path = Path(run_dir) / "table_deflated.parquet"
    if deflated_path.exists():
        st.markdown("### Deflated Sharpe")
        st.dataframe(
            pd.read_parquet(deflated_path),
            width="stretch",
            hide_index=True,
        )

    st.markdown("### Plots")
    c1, c2 = st.columns(2)
    eq = Path(run_dir) / "equity_curves.png"
    cal = Path(run_dir) / "calibration.png"
    with c1:
        if eq.exists():
            st.image(str(eq), caption="Equity curves (net of cost)")
        else:
            st.info("equity_curves.png not found")
    with c2:
        if cal.exists():
            st.image(str(cal), caption="Calibration (pooled)")
        else:
            st.info("calibration.png not found")


# ---------------------------------------------------------------------
# Tab: Drift
# ---------------------------------------------------------------------


def render_drift_tab(report: dict[str, Any]) -> None:
    """Drift tab: PSI + KS per feature, two bar charts."""
    st.subheader("Feature drift (PSI + KS)")
    dr = report.get("drift", {})
    overall = dr.get("overall", "?")
    n_feat = dr.get("n_features", 0)
    flagged = dr.get("flagged_count", 0)
    st.markdown(f"**Overall:** {_status_badge(overall)}  features={n_feat}  flagged={flagged}")

    feats = dr.get("features", [])
    if not feats:
        reason = dr.get("reason")
        if reason:
            st.info(reason)
        else:
            st.info("No drift data available.")
        return

    df = pd.DataFrame(
        [
            {
                "feature": d.get("feature"),
                "status": d.get("status"),
                "psi": d.get("psi"),
                "ks_pvalue": d.get("ks_pvalue"),
                "n_ref": d.get("n_ref"),
                "n_cur": d.get("n_cur"),
            }
            for d in feats
        ]
    )
    df = df.sort_values(["status", "psi"], ascending=[False, False], na_position="last")
    st.dataframe(df, width="stretch", hide_index=True)

    st.markdown("### PSI per feature")
    psi_df = df.dropna(subset=["psi"]).set_index("feature")["psi"]
    if not psi_df.empty:
        st.bar_chart(psi_df)

    st.markdown("### KS p-value per feature")
    ks_df = df.dropna(subset=["ks_pvalue"]).set_index("feature")["ks_pvalue"]
    if not ks_df.empty:
        st.bar_chart(ks_df)


# ---------------------------------------------------------------------
# Tab: About
# ---------------------------------------------------------------------


def render_about_tab() -> None:
    """About tab: project links and runtime info."""
    st.subheader("About")

    about_md = (
        "- **Project:** data-backtest-pipeline\n"
        "- **Purpose:** reproducible, leakage-free walk-forward "
        "backtest of daily US equity direction.\n"
        "- **Not:** trading bot, financial advice, or production system.\n"
        "- **Design docs:** every major decision lives in an ADR under `docs/adr/`."
    )
    st.markdown(about_md)

    st.markdown("### Repository")
    links_md = (
        "- [GitHub](https://github.com/WoodinGlass/data-backtest-pipeline)\n"
        "- [ADRs](https://github.com/WoodinGlass/data-backtest-pipeline/tree/main/docs/adr)\n"
        "- [Runbook](https://github.com/WoodinGlass/data-backtest-pipeline/blob/main/docs/runbook.md)"
    )
    st.markdown(links_md)

    st.markdown("### Runtime")
    st.markdown(f"- Repo root: `{REPO_ROOT}`")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    st.markdown(f"- Dashboard rendered at `{now}`")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def _render_demo_banner() -> None:
    """Show a loud banner when running on the committed demo data."""
    _w, _f, _b, is_demo = _resolve_roots()
    if is_demo:
        st.warning(
            "**DEMO DATA** — synthetic market, not a real backtest. "
            "See `data_demo/README.md` for details."
        )


def main() -> None:
    """Entrypoint: 4 tabs + refresh button in the sidebar."""
    st.set_page_config(
        page_title="data-backtest-pipeline",
        page_icon=":bar_chart:",
        layout="wide",
    )
    st.title("data-backtest-pipeline")
    st.caption(
        "Daily US equity direction - ingestion, warehouse, features, "
        "risk, backtest, tracking, orchestration."
    )

    _render_demo_banner()

    with st.sidebar:
        st.header("Controls")
        if st.button("Refresh (clear cache)"):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"Cache TTL: {CACHE_TTL_SECONDS}s.")

    tab_health, tab_backtest, tab_drift, tab_about = st.tabs(
        ["Health", "Backtest", "Drift", "About"]
    )

    with tab_health:
        try:
            report = get_report()
            render_health_tab(report)
        except Exception as e:
            st.error(f"Failed to build report: {e}")

    with tab_backtest:
        try:
            render_backtest_tab()
        except Exception as e:
            st.error(f"Failed to load backtest: {e}")

    with tab_drift:
        try:
            report = get_report()
            render_drift_tab(report)
        except Exception as e:
            st.error(f"Failed to build drift report: {e}")

    with tab_about:
        render_about_tab()


if __name__ == "__main__":
    main()
