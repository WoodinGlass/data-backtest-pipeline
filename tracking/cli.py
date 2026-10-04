"""dbp-tracking — CLI for inspecting MLflow runs. ADR 0015.

Subcommands:

    list-runs  [--experiment NAME] [--limit N] [--order-by METRIC]
    best-run   [--experiment NAME] [--metric METRIC]
    compare    RUN_ID_A RUN_ID_B [--experiment NAME]

All subcommands are read-only. If MLflow is not installed, they
exit with a clear message instead of a stack trace.
"""

from __future__ import annotations

import argparse
import logging
import sys

from tracking.client import (
    MLFLOW_AVAILABLE,
    resolve_tracking_uri,
)
from tracking.config import TrackingSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _require_mlflow() -> None:
    if not MLFLOW_AVAILABLE:
        print(
            "MLflow is not installed. Install with:",
            file=sys.stderr,
        )
        print("  pip install -e .[tracking]", file=sys.stderr)
        raise SystemExit(2)


def _prepare_client(args: argparse.Namespace):
    """Resolve settings + experiment, return (client, experiment_id)."""
    _require_mlflow()
    import mlflow
    from mlflow.tracking import MlflowClient

    settings = TrackingSettings(
        experiment_name=args.experiment,
        tracking_uri=args.tracking_uri or TrackingSettings().tracking_uri,
    )
    uri = resolve_tracking_uri(settings)
    mlflow.set_tracking_uri(uri)

    client = MlflowClient()
    exp = client.get_experiment_by_name(settings.experiment_name)
    if exp is None:
        print(
            "Experiment " + repr(settings.experiment_name) + " not found at " + uri,
            file=sys.stderr,
        )
        raise SystemExit(1)
    return client, exp.experiment_id, uri


def _fmt(value: object, width: int = 10) -> str:
    """Compact repr for tabular output."""
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        if value != value:  # NaN
            return "NaN".rjust(width)
        return f"{value:>{width}.4f}"
    s = str(value)
    return s[:width].rjust(width)


# ---------------------------------------------------------------------
# list-runs
# ---------------------------------------------------------------------


def cmd_list_runs(args: argparse.Namespace) -> int:
    client, exp_id, uri = _prepare_client(args)

    order = ["metrics." + args.order_by + " DESC"] if args.order_by else []
    runs = client.search_runs(
        experiment_ids=[exp_id],
        order_by=order,
        max_results=args.limit,
    )

    if not runs:
        print("No runs found in " + repr(args.experiment))
        return 0

    print(f"Tracking URI: {uri}")
    print(f"Experiment:   {args.experiment}  ({len(runs)} runs)")
    print()
    header = f"{'run_id':>12s}  {'name':>40s}  {'sharpe':>10s}  {'auc':>10s}  {'status':>8s}"
    print(header)
    print("-" * len(header))
    for r in runs:
        name = r.data.tags.get("mlflow.runName", "")
        sharpe = r.data.metrics.get("pooled/sharpe")
        auc = r.data.metrics.get("pooled/auc")
        print(
            f"{r.info.run_id[:12]:>12s}  {name[:40]:>40s}  "
            + _fmt(sharpe, 10)
            + "  "
            + _fmt(auc, 10)
            + f"  {r.info.status:>8s}"
        )
    return 0


# ---------------------------------------------------------------------
# best-run
# ---------------------------------------------------------------------


def cmd_best_run(args: argparse.Namespace) -> int:
    client, exp_id, uri = _prepare_client(args)

    metric = "pooled/" + args.metric
    runs = client.search_runs(
        experiment_ids=[exp_id],
        order_by=["metrics." + metric + " DESC"],
        max_results=1,
    )
    if not runs:
        print("No runs found.", file=sys.stderr)
        return 1

    r = runs[0]
    name = r.data.tags.get("mlflow.runName", "")
    value = r.data.metrics.get(metric)

    print(f"Tracking URI: {uri}")
    print(f"Experiment:   {args.experiment}")
    print(f"Metric:       {metric}")
    print()
    print(f"Best run:     {name}")
    print(f"run_id:       {r.info.run_id}")
    print(f"value:        {value}")
    return 0


# ---------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------


def _fetch_run(client, run_id: str):
    """Return run, or None if not found."""
    try:
        return client.get_run(run_id)
    except Exception:
        return None


def _diff_metrics(a: dict, b: dict, tol: float = 1e-9) -> tuple[list, list, list]:
    """Return (only_a, only_b, changed)."""
    keys_a, keys_b = set(a), set(b)
    only_a = sorted(keys_a - keys_b)
    only_b = sorted(keys_b - keys_a)
    changed = []
    for k in sorted(keys_a & keys_b):
        if abs(a[k] - b[k]) > tol:
            changed.append((k, a[k], b[k], a[k] - b[k]))
    return only_a, only_b, changed


def _diff_params(a: dict, b: dict) -> list:
    keys = sorted(set(a) | set(b))
    diffs = []
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if va != vb:
            diffs.append((k, va, vb))
    return diffs


def cmd_compare(args: argparse.Namespace) -> int:
    client, _exp_id, uri = _prepare_client(args)

    ra = _fetch_run(client, args.run_id_a)
    rb = _fetch_run(client, args.run_id_b)
    if ra is None:
        print("Run not found: " + args.run_id_a, file=sys.stderr)
        return 1
    if rb is None:
        print("Run not found: " + args.run_id_b, file=sys.stderr)
        return 1

    name_a = ra.data.tags.get("mlflow.runName", ra.info.run_id[:12])
    name_b = rb.data.tags.get("mlflow.runName", rb.info.run_id[:12])

    print(f"Tracking URI: {uri}")
    print(f"A: {name_a}  ({ra.info.run_id})")
    print(f"B: {name_b}  ({rb.info.run_id})")
    print()

    # Params
    pdiff = _diff_params(ra.data.params, rb.data.params)
    if pdiff:
        print("Params differing (A -> B):")
        for k, va, vb in pdiff:
            print(f"  {k}: {va!r} -> {vb!r}")
    else:
        print("Params: identical")
    print()

    # Metrics
    only_a, only_b, changed = _diff_metrics(
        ra.data.metrics,
        rb.data.metrics,
    )
    if changed:
        print("Metrics changed (A -> B, delta = A - B):")
        for k, va, vb, d in changed[:30]:
            print(f"  {k:40s}  {va:>10.6f}  ->  {vb:>10.6f}   ({d:+.6f})")
        if len(changed) > 30:
            print(f"  ... ({len(changed) - 30} more)")
    else:
        print("Metrics: identical")
    if only_a:
        print(f"Only in A: {only_a[:10]}")
    if only_b:
        print(f"Only in B: {only_b[:10]}")
    return 0


# ---------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dbp-tracking",
        description="Inspect MLflow runs for data-backtest-pipeline.",
    )
    p.add_argument("--experiment", default="daily-direction")
    p.add_argument("--tracking-uri", default=None)
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = p.add_subparsers(dest="command", required=True)

    # list-runs
    lr = sub.add_parser("list-runs", help="List recent runs.")
    lr.add_argument("--limit", type=int, default=20)
    lr.add_argument("--order-by", default=None, help="Metric to sort by (descending).")
    lr.set_defaults(func=cmd_list_runs)

    # best-run
    br = sub.add_parser("best-run", help="Show the best run for a metric.")
    br.add_argument(
        "--metric", default="sharpe", help="Metric suffix, e.g. sharpe -> pooled/sharpe"
    )
    br.set_defaults(func=cmd_best_run)

    # compare
    cp = sub.add_parser("compare", help="Diff two runs by run_id.")
    cp.add_argument("run_id_a")
    cp.add_argument("run_id_b")
    cp.set_defaults(func=cmd_compare)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
