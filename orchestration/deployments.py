"""Deployment specs for scheduled flows. ADR 0016 section 6.

In ephemeral mode (default) this module only describes the
schedules. In served mode it registers them with Prefect via
`flow.serve()`. Both paths share the same SCHEDULE_REGISTRY so
there is one source of truth for crons.

Public API:

    DeploymentSpec: dataclass(flow_name, cron, description)
    SCHEDULE_REGISTRY: dict[str, DeploymentSpec]
    list_deployments(settings) -> list[DeploymentSpec]
    serve_flows(settings, names=None) -> int  (blocks; returns 0)
    describe_deployments(settings) -> str
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from orchestration._compat import PREFECT_AVAILABLE
from orchestration.config import OrchestrationSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Deployment specs
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class DeploymentSpec:
    flow_name: str
    cron: str
    description: str


# Crons are filled at call time from settings (so env overrides apply).
_SCHEDULED_FLOWS: dict[str, str] = {
    "daily_refresh": "prices -> dbt -> quality -> features",
    "weekly_refresh": "macro + sec -> dbt -> quality -> features -> backtest",
    # full_refresh is intentionally not scheduled (manual only).
}

_MANUAL_FLOWS: dict[str, str] = {
    "full_refresh": "prices + macro + sec -> ... -> backtest (manual)",
}


def list_deployments(
    settings: OrchestrationSettings | None = None,
) -> list[DeploymentSpec]:
    """Return the scheduled deployments with crons from settings."""
    if settings is None:
        settings = OrchestrationSettings()
    crons = {
        "daily_refresh": settings.daily_cron,
        "weekly_refresh": settings.weekly_cron,
    }
    return [
        DeploymentSpec(
            flow_name=name,
            cron=crons[name],
            description=desc,
        )
        for name, desc in _SCHEDULED_FLOWS.items()
    ]


def list_manual_flows() -> list[DeploymentSpec]:
    """Return flows that are manual-only (not scheduled)."""
    return [
        DeploymentSpec(flow_name=name, cron="", description=desc)
        for name, desc in _MANUAL_FLOWS.items()
    ]


def describe_deployments(
    settings: OrchestrationSettings | None = None,
) -> str:
    """Human-readable summary of schedules + mode."""
    if settings is None:
        settings = OrchestrationSettings()
    lines: list[str] = []
    lines.append(f"Deployment mode: {settings.deployment_mode}")
    lines.append(f"Prefect available: {PREFECT_AVAILABLE}")
    lines.append("")
    lines.append("Scheduled flows:")
    for spec in list_deployments(settings):
        lines.append(f"  {spec.flow_name:18s}  {spec.cron:14s}  {spec.description}")
    lines.append("")
    lines.append("Manual flows:")
    for spec in list_manual_flows():
        lines.append(f"  {spec.flow_name:18s}  {'(manual)':14s}  {spec.description}")
    lines.append("")
    if not PREFECT_AVAILABLE:
        lines.append(
            "Note: Prefect is not installed. `serve` is a no-op. "
            "Install with: pip install -e .[orchestration]"
        )
    elif settings.deployment_mode == "ephemeral":
        lines.append(
            "Note: mode=ephemeral. `serve` will run but is intended "
            "for long-lived hosts (Docker, server). Use `run` for "
            "one-shot local execution."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Served mode
# ---------------------------------------------------------------------


def _resolve_flow_callable(flow_name: str) -> Any | None:
    """Return the flow object, or None if not found."""
    try:
        from orchestration import flows as flows_mod
    except ImportError as e:
        logger.warning("could not import orchestration.flows: %s", e)
        return None
    obj = getattr(flows_mod, flow_name, None)
    if obj is None:
        logger.warning("flow %r not found in orchestration.flows", flow_name)
    return obj


def serve_flows(
    settings: OrchestrationSettings | None = None,
    names: list[str] | None = None,
) -> int:
    """Register schedules via flow.serve(). Blocks forever.

    Returns 0 on clean shutdown, 1 if Prefect or a flow is missing.
    Intended to be called from a long-lived process (Docker,
    systemd, `dbp-orchestrate serve`).
    """
    if settings is None:
        settings = OrchestrationSettings()

    if not PREFECT_AVAILABLE:
        logger.error(
            "serve_flows: Prefect is not installed. Install with: pip install -e .[orchestration]"
        )
        return 1

    specs = list_deployments(settings)
    if names:
        wanted = set(names)
        specs = [s for s in specs if s.flow_name in wanted]
        if not specs:
            logger.error(
                "serve_flows: no matching scheduled flows for %s",
                names,
            )
            return 1

    # Import Prefect's CronSchedule lazily
    try:
        from prefect.schedules import CronSchedule
    except ImportError as e:
        logger.error("serve_flows: cannot import CronSchedule: %s", e)
        return 1

    # Build (flow, cron) pairs
    pairs: list[tuple[str, Any, str]] = []
    for spec in specs:
        obj = _resolve_flow_callable(spec.flow_name)
        if obj is None:
            return 1
        pairs.append((spec.flow_name, obj, spec.cron))

    logger.info(
        "serve_flows: registering %d schedules (mode=%s)",
        len(pairs),
        settings.deployment_mode,
    )
    for name, _, cron in pairs:
        logger.info("  %s  %s", name, cron)

    # flow.serve() blocks; loop over each schedule sequentially is
    # not possible, so we build a list of schedule objects and pass
    # them all to the first flow's serve() call is wrong. The
    # correct pattern is: each flow.serve() blocks. So we call them
    # one at a time, expecting the caller to have one process per
    # flow when running under a process manager.
    #
    # For a single-process convenience deployment, we serve only the
    # first spec and log a clear warning about the rest.
    first_name, first_flow, first_cron = pairs[0]
    logger.info(
        "serve_flows: serving %s (only). Run additional flows in "
        "separate processes if you need them all.",
        first_name,
    )
    schedule = CronSchedule(cron=first_cron, timezone="UTC")
    try:
        first_flow.serve(
            name=f"{first_name}-scheduled",
            schedules=[schedule],
            tags=["dbp", first_name],
        )
    except KeyboardInterrupt:
        logger.info("serve_flows: interrupted")
        return 0
    return 0


__all__ = [
    "DeploymentSpec",
    "describe_deployments",
    "list_deployments",
    "list_manual_flows",
    "serve_flows",
]
