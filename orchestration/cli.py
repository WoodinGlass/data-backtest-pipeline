"""dbp-orchestrate — CLI for the orchestration layer. ADR 0016.

Subcommands:

    list                     List available flows.
    run <flow>               Run one flow in the current process
                             (ephemeral mode; no Prefect server).
    schedule                 Print the configured schedules.
    info <flow>              Print a short description of one flow.

Works with or without Prefect installed. Without Prefect, `run`
calls the underlying flow function directly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from orchestration._compat import PREFECT_AVAILABLE
from orchestration.config import OrchestrationSettings

# ---------------------------------------------------------------------
# Flow registry
# ---------------------------------------------------------------------

# Lazy import map: name -> (module_path, attr)
_FLOW_REGISTRY: dict[str, str] = {
    # stage flows
    "ingest_prices": "ingest_prices",
    "ingest_macro": "ingest_macro",
    "ingest_sec": "ingest_sec",
    "dbt_build": "dbt_build",
    "quality_gate": "quality_gate",
    "build_features": "build_features",
    "run_backtest": "run_backtest",
    # composite flows
    "daily_refresh": "daily_refresh",
    "weekly_refresh": "weekly_refresh",
    "full_refresh": "full_refresh",
}

_STAGE_FLOWS = {
    "ingest_prices",
    "ingest_macro",
    "ingest_sec",
    "dbt_build",
    "quality_gate",
    "build_features",
    "run_backtest",
}
_COMPOSITE_FLOWS = {"daily_refresh", "weekly_refresh", "full_refresh"}

_FLOW_DESCRIPTIONS: dict[str, str] = {
    "ingest_prices": "Fetch daily OHLCV bars via yfinance (M1)",
    "ingest_macro": "Fetch FRED + ALFRED macro series (M3.5)",
    "ingest_sec": "Fetch SEC EDGAR XBRL facts (M3.7)",
    "dbt_build": "Build warehouse via dbt (M2, M3.6, M3.8)",
    "quality_gate": "Run the Pandera quality gate (M3, M3.9)",
    "build_features": "Build the PIT feature table (M4)",
    "run_backtest": "Run the walk-forward backtest with tracking (M5 + M6)",
    "daily_refresh": "Composite: prices -> dbt -> quality -> features",
    "weekly_refresh": "Composite: macro + sec -> dbt -> quality -> features -> backtest",
    "full_refresh": "Composite: prices + macro + sec -> ... -> backtest (manual)",
}


def _load_flow(name: str):
    """Import and return the flow callable for a given name."""
    if name not in _FLOW_REGISTRY:
        raise ValueError(f"Unknown flow: {name!r}. Available: {sorted(_FLOW_REGISTRY)}")
    from orchestration import flows as flows_mod

    obj = getattr(flows_mod, _FLOW_REGISTRY[name])
    # Prefect 3 wraps @flow into an object with .fn; unwrap for direct call.
    return getattr(obj, "fn", obj)


# ---------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------


def cmd_list(_args: argparse.Namespace) -> int:
    print("Available flows:")
    print()
    print("  Stage flows:")
    for name in sorted(_STAGE_FLOWS):
        print(f"    {name:18s}  {_FLOW_DESCRIPTIONS.get(name, '')}")
    print()
    print("  Composite flows:")
    for name in sorted(_COMPOSITE_FLOWS):
        print(f"    {name:18s}  {_FLOW_DESCRIPTIONS.get(name, '')}")
    print()
    print(f"Prefect available: {PREFECT_AVAILABLE}")
    print("  (when False, `run` invokes the flow directly without a server)")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    name = args.flow
    if name not in _FLOW_REGISTRY:
        print(f"Unknown flow: {name!r}", file=sys.stderr)
        return 2
    kind = "composite" if name in _COMPOSITE_FLOWS else "stage"
    print(f"Flow: {name}")
    print(f"Kind: {kind}")
    print(f"Desc: {_FLOW_DESCRIPTIONS.get(name, '-')}")
    print(f"Prefect available: {PREFECT_AVAILABLE}")
    return 0


def cmd_schedule(_args: argparse.Namespace) -> int:
    settings = OrchestrationSettings()
    print("Configured schedules (UTC cron):")
    print()
    print(f"  daily_refresh   {settings.daily_cron}")
    print(f"  weekly_refresh  {settings.weekly_cron}")
    print("  full_refresh    (manual only)")
    print()
    print(f"Deployment mode: {settings.deployment_mode}")
    print(
        f"Retry policy:    {settings.retry_attempts} attempts, "
        f"delays {settings.retry_delay_seconds}s"
    )
    print(f"Webhook alerts:  {'on' if settings.alert_webhook_url else 'off'}")
    return 0


def _parse_kwargs(raw: list[str]) -> dict[str, Any]:
    """Parse repeated KEY=VALUE into a dict. Values are best-effort typed.

    Supported value forms:
        true / false         -> bool
        <int>                -> int
        <float>              -> float
        json object/array    -> dict/list
        anything else        -> str
    """
    out: dict[str, Any] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--arg must be KEY=VALUE; got {item!r}")
        k, _, v = item.partition("=")
        v = v.strip()
        if v.lower() == "true":
            out[k] = True
        elif v.lower() == "false":
            out[k] = False
        elif v.lower() == "none":
            out[k] = None
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    if (v.startswith("{") and v.endswith("}")) or (
                        v.startswith("[") and v.endswith("]")
                    ):
                        try:
                            out[k] = json.loads(v)
                            continue
                        except json.JSONDecodeError:
                            pass
                    out[k] = v
    return out


def cmd_run(args: argparse.Namespace) -> int:
    name = args.flow
    if name not in _FLOW_REGISTRY:
        print(f"Unknown flow: {name!r}", file=sys.stderr)
        return 2

    extra = _parse_kwargs(args.arg or [])
    settings = OrchestrationSettings()

    if args.dry_run:
        print(f"[dry-run] Would run flow: {name}")
        print(f"[dry-run] Settings: {settings.describe()}")
        print(f"[dry-run] Extra args: {extra}")
        return 0

    flow_fn = _load_flow(name)
    print(f"Running flow: {name}")
    print(f"Prefect available: {PREFECT_AVAILABLE}")
    print(f"Settings: {settings.describe()}")
    print()
    try:
        result = flow_fn(settings=settings, **extra)
    except TypeError as e:
        print(f"Flow call failed (bad kwargs?): {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Flow failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print()
    print(f"Flow {name} completed.")
    if args.json_out:
        try:
            with Path(args.json_out).open("w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"Result written to {args.json_out}")
        except OSError as e:
            print(f"Could not write {args.json_out}: {e}", file=sys.stderr)
    else:
        # Compact summary
        if isinstance(result, dict) and "steps" in result:
            for step_name, step_result in result["steps"].items():
                ok = isinstance(step_result, dict) and step_result.get("returncode", 0) == 0
                mark = "OK" if ok else "FAIL"
                print(f"  [{mark}] {step_name}")
        else:
            print(f"Result: {result}")
    return 0


# ---------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dbp-orchestrate",
        description="Run and inspect the pipeline's Prefect flows.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    sub = p.add_subparsers(dest="command", required=True)

    # list
    lp = sub.add_parser("list", help="List available flows.")
    lp.set_defaults(func=cmd_list)

    # info
    ip = sub.add_parser("info", help="Show one flow's metadata.")
    ip.add_argument("flow")
    ip.set_defaults(func=cmd_info)

    # schedule
    sp = sub.add_parser("schedule", help="Print configured schedules.")
    sp.set_defaults(func=cmd_schedule)

    # run
    rp = sub.add_parser("run", help="Run one flow (ephemeral).")
    rp.add_argument("flow")
    rp.add_argument(
        "--arg",
        action="append",
        default=[],
        help="Flow kwarg as KEY=VALUE. Repeatable.",
    )
    rp.add_argument("--json-out", default=None, help="Write the flow result as JSON to this path.")
    rp.add_argument("--dry-run", action="store_true", help="Print what would run; do not execute.")
    rp.set_defaults(func=cmd_run)

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
