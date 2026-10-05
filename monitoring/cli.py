"""dbp-monitor — CLI for the monitoring layer. ADR 0019.

Subcommands:

    run      Assemble the report, print the summary, optionally
             write JSON with --json PATH.
    schema   Print the JSON schema of the report (keys only).
    info     Print the current MonitoringSettings.

Everything is read-only. The CLI never mutates the warehouse,
features, or backtest artifacts.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from monitoring.config import MONITORING_VERSION, MonitoringSettings
from monitoring.report import build_report, format_summary, save_report

# ---------------------------------------------------------------------
# Report shape (for the `schema` subcommand)
# ---------------------------------------------------------------------

_REPORT_SHAPE: dict[str, Any] = {
    "generated_at": "str (ISO 8601 UTC)",
    "version": "str",
    "reference_date": "str (YYYY-MM-DD)",
    "repo_root": "str",
    "freshness": {
        "reference_date": "str",
        "marts": [
            {
                "mart": "str",
                "status": "PASS | WARN | FAIL",
                "rows": "int",
                "latest_date": "str | null",
                "age_days": "int | null",
                "warn_days": "int",
                "fail_days": "int",
                "reason": "str",
            }
        ],
        "overall": "PASS | WARN | FAIL",
    },
    "drift": {
        "n_features": "int",
        "flagged_count": "int",
        "features": [
            {
                "feature": "str",
                "n_ref": "int",
                "n_cur": "int",
                "psi": "float | null",
                "ks_stat": "float | null",
                "ks_pvalue": "float | null",
                "psi_severity": "PASS | WARN | FAIL",
                "ks_severity": "PASS | WARN | FAIL",
                "status": "PASS | WARN | FAIL",
                "reason": "str",
            }
        ],
        "overall": "PASS | WARN | FAIL",
        "reason": "str (optional)",
    },
    "performance": {
        "run_dir": "str | null",
        "metrics_path": "str",
        "model": "str",
        "window_folds": "int",
        "n_folds_total": "int",
        "n_folds_used": "int",
        "fold_range": "[int, int] | null",
        "metrics": {
            "<metric>": {
                "value": "float | null",
                "warn": "float",
                "fail": "float",
                "direction": "lower_is_better | higher_is_better",
                "severity": "PASS | WARN | FAIL",
            }
        },
        "overall": "PASS | WARN | FAIL",
        "reason": "str",
    },
    "errors": [{"section": "str", "error": "str"}],
    "overall": "PASS | WARN | FAIL",
}


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    report = build_report(settings=settings)
    print(format_summary(report))

    if args.json:
        saved = save_report(report, args.json)
        print()
        print(f"JSON written to: {saved}")

    # Exit code reflects overall severity, so `make monitor` can be
    # used in CI-like contexts:
    #   PASS / WARN -> 0
    #   FAIL        -> 1
    overall = report.get("overall", "PASS")
    if overall == "FAIL" and args.fail_on_fail:
        return 1
    return 0


def cmd_schema(_args: argparse.Namespace) -> int:
    print(json.dumps(_REPORT_SHAPE, indent=2, default=str))
    return 0


def cmd_info(_args: argparse.Namespace) -> int:
    settings = MonitoringSettings()
    print(f"version              = {MONITORING_VERSION}")
    print(f"settings             = {settings.describe()}")
    print(f"warehouse_path       = {settings.warehouse_path}")
    print(f"warehouse_schema     = {settings.warehouse_schema}")
    print(f"features_path        = {settings.features_path}")
    print(f"backtest_dir         = {settings.backtest_dir}")
    print(f"output_path          = {settings.output_path}")
    print()
    print("marts (warn/fail days):")
    for name, th in sorted(settings.marts.items()):
        print(f"  {name:26s}  warn={th.warn_days:>4d}  fail={th.fail_days:>4d}")
    print()
    print("drift thresholds:")
    print(f"  psi  warn/fail       = {settings.drift_psi_warn} / {settings.drift_psi_fail}")
    print(
        f"  ks p warn/fail       = "
        f"{settings.drift_ks_pvalue_warn} / {settings.drift_ks_pvalue_fail}"
    )
    print(
        f"  reference/current    = "
        f"{settings.drift_reference_days}d / {settings.drift_current_days}d"
    )
    print()
    print("performance thresholds (warn / fail):")
    print("  cls_log_loss  (lower better)  warn=0.72 fail=0.75")
    print("  cls_brier     (lower better)  warn=0.26 fail=0.28")
    print("  cls_auc       (higher better) warn=0.50 fail=0.48")
    print("  trd_sharpe    (higher better) warn=0.00 fail=-0.50")
    return 0


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _settings_from_args(args: argparse.Namespace) -> MonitoringSettings:
    kwargs: dict[str, Any] = {}
    if args.warehouse:
        kwargs["warehouse_path"] = args.warehouse
    if args.features:
        kwargs["features_path"] = args.features
    if args.backtest_dir:
        kwargs["backtest_dir"] = args.backtest_dir
    if args.reference_date:
        kwargs["reference_date"] = args.reference_date
    return MonitoringSettings(**kwargs)


# ---------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dbp-monitor",
        description="Freshness + drift + performance report for the pipeline.",
    )
    p.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    sub = p.add_subparsers(dest="command", required=True)

    # run
    rp = sub.add_parser("run", help="Build the report and print a summary.")
    rp.add_argument("--json", default=None, help="Write the report as JSON to this path.")
    rp.add_argument("--warehouse", default=None, help="Override warehouse_path.")
    rp.add_argument("--features", default=None, help="Override features_path.")
    rp.add_argument("--backtest-dir", default=None, help="Override backtest_dir.")
    rp.add_argument("--reference-date", default=None, help="Override reference date (YYYY-MM-DD).")
    rp.add_argument(
        "--fail-on-fail", action="store_true", help="Exit with code 1 if overall status is FAIL."
    )
    rp.set_defaults(func=cmd_run)

    # schema
    sp = sub.add_parser("schema", help="Print the JSON report schema.")
    sp.set_defaults(func=cmd_schema)

    # info
    ip = sub.add_parser("info", help="Print current settings + thresholds.")
    ip.set_defaults(func=cmd_info)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
