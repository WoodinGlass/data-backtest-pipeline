"""Failure alerts for orchestration flows. ADR 0016 §8.

Two layers, always applied in order:
    1. Structured log (always).
    2. Webhook POST (opt-in, best-effort).

The webhook post never masks the original failure and never
changes the flow exit status. Any exception during posting is
logged and swallowed.

Public API:

    build_failure_payload(flow_name, flow_run_id, task_name,
                          attempt, error, duration_ms) -> dict

    post_webhook(url, payload, timeout_seconds) -> bool

    on_flow_failure(flow, flow_run, state) -> None
        Prefect hook. Bound to settings via closure (see
        make_flow_failure_hook).

    make_flow_failure_hook(settings) -> callable
        Returns the hook used by @flow(on_failure=[...]).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from orchestration.config import OrchestrationSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------


def build_failure_payload(
    flow_name: str,
    flow_run_id: str | None = None,
    task_name: str | None = None,
    attempt: int | None = None,
    error: BaseException | str | None = None,
    duration_ms: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-serialisable payload describing a failure.

    The shape is compatible with Slack incoming webhooks
    (which expect a top-level ``text`` key) and with generic
    endpoints (which can ignore ``text`` and read the rest).
    """
    if isinstance(error, BaseException):
        err_type = type(error).__name__
        err_msg = str(error)
    elif error is None:
        err_type = ""
        err_msg = ""
    else:
        err_type = "Error"
        err_msg = str(error)

    where = "flow"
    if task_name:
        where = "task:" + task_name

    text = (
        "[data-backtest-pipeline] "
        + flow_name
        + " failed"
        + (" (" + where + ")" if where != "flow" else "")
    )
    if attempt is not None:
        text += " on attempt " + str(attempt)
    if err_type or err_msg:
        text += ": " + err_type + ": " + err_msg

    payload: dict[str, Any] = {
        "text": text,
        "flow_name": flow_name,
        "flow_run_id": flow_run_id or "",
        "task_name": task_name or "",
        "attempt": attempt,
        "error_type": err_type,
        "error_msg": err_msg,
        "duration_ms": duration_ms,
        "source": "data-backtest-pipeline",
    }
    if extra:
        payload["extra"] = extra
    return payload


# ---------------------------------------------------------------------
# Webhook POST (best-effort)
# ---------------------------------------------------------------------


def post_webhook(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: float = 5.0,
) -> bool:
    """POST payload as JSON. Returns True on 2xx, False otherwise.

    Never raises. All errors are logged at WARNING level.
    """
    try:
        body = json.dumps(payload, default=str).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "data-backtest-pipeline/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            ok = 200 <= int(status) < 300
            if ok:
                logger.info(
                    "alert webhook: delivered (HTTP %s) to %s",
                    status,
                    _safe_url(url),
                )
            else:
                logger.warning(
                    "alert webhook: non-2xx (HTTP %s) to %s",
                    status,
                    _safe_url(url),
                )
            return ok
    except urllib.error.HTTPError as e:
        logger.warning(
            "alert webhook: HTTPError %s to %s: %s",
            e.code,
            _safe_url(url),
            e.reason,
        )
        return False
    except urllib.error.URLError as e:
        logger.warning(
            "alert webhook: URLError to %s: %s",
            _safe_url(url),
            e.reason,
        )
        return False
    except Exception as e:
        logger.warning(
            "alert webhook: unexpected error to %s: %s",
            _safe_url(url),
            e,
        )
        return False


def _safe_url(url: str) -> str:
    """Hide the path portion of a webhook URL for logging."""
    try:
        head, _, _tail = url.partition("//")
        if not _tail:
            return url
        host, _, _rest = _tail.partition("/")
        return head + "//" + host + "/..."
    except Exception:
        return "<url>"


# ---------------------------------------------------------------------
# Prefect on_failure hook
# ---------------------------------------------------------------------


def _noop_hook(*_args, **_kwargs) -> None:
    """Placeholder used when Prefect is not installed."""


def make_flow_failure_hook(settings: OrchestrationSettings):
    """Return a callable suitable for @flow(on_failure=[hook]).

    The returned hook accepts the Prefect 3 signature
    (flow, flow_run, state) and ignores anything else. It
    never raises — a hook failure must not mask the original
    flow failure.
    """

    def _hook(flow=None, flow_run=None, state=None, *_args, **_kwargs) -> None:
        try:
            flow_name = getattr(flow, "name", None) or "unknown_flow"
            run_id = getattr(flow_run, "id", None)
            # state.message may carry the exception string
            err_msg = ""
            err_type = ""
            if state is not None:
                msg = getattr(state, "message", None)
                if msg:
                    err_msg = str(msg)
            # Structured log
            logger.error(
                "flow failure: name=%s run_id=%s msg=%s",
                flow_name,
                run_id or "-",
                err_msg or "-",
            )
            # Webhook (opt-in)
            if settings.alert_webhook_url:
                payload = build_failure_payload(
                    flow_name=flow_name,
                    flow_run_id=run_id,
                    error=err_msg,
                    extra={"error_type": err_type},
                )
                post_webhook(
                    settings.alert_webhook_url,
                    payload,
                    timeout_seconds=settings.alert_webhook_timeout_seconds,
                )
        except Exception as e:
            # Last line of defence: never let a hook crash the
            # flow failure path.
            logger.warning("on_failure hook itself failed: %s", e)

    return _hook


# ---------------------------------------------------------------------
# Task-level emitter
# ---------------------------------------------------------------------


def emit_task_failure(
    settings: OrchestrationSettings,
    flow_name: str,
    task_name: str,
    error: BaseException | str | None,
    attempt: int | None = None,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    """Emit a structured log + optional webhook for a task failure.

    Returns the payload that was (or would have been) posted,
    so callers can include it in their own logs.
    """
    payload = build_failure_payload(
        flow_name=flow_name,
        task_name=task_name,
        attempt=attempt,
        error=error,
        duration_ms=duration_ms,
    )
    logger.error(
        "task failure: flow=%s task=%s attempt=%s error=%s",
        flow_name,
        task_name,
        attempt,
        payload["error_msg"] or "-",
    )
    if settings.alert_webhook_url:
        post_webhook(
            settings.alert_webhook_url,
            payload,
            timeout_seconds=settings.alert_webhook_timeout_seconds,
        )
    return payload


# ---------------------------------------------------------------------
# Timing helper (used by task wrappers)
# ---------------------------------------------------------------------


class Timer:
    """Minimal context manager that records elapsed milliseconds."""

    def __init__(self) -> None:
        self.t0 = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> Timer:
        self.t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.elapsed_ms = (time.monotonic() - self.t0) * 1000.0


__all__ = [
    "Timer",
    "build_failure_payload",
    "emit_task_failure",
    "make_flow_failure_hook",
    "post_webhook",
]
