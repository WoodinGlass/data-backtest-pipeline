"""Structured logging for the ingestion layer.

Design goals:
- JSON by default, so logs are machine-parseable everywhere.
- Console mode for local dev, so humans can read them too.
- A correlation ID bound once per run, automatically attached to
  every subsequent log line via contextvars.

Usage:
    from ingestion.logging import configure_logging, get_logger, new_correlation_id

    configure_logging()
    log = get_logger(__name__)
    with new_correlation_id() as cid:
        log.info("run_started", ticker="AAPL")
        # every log line here carries correlation_id=cid
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

__all__ = [
    "configure_logging",
    "get_correlation_id",
    "get_logger",
    "new_correlation_id",
]


_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_configured: bool = False


def _add_correlation_id(
    _logger: object,
    _method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    """structlog processor: attach the active correlation ID, if any."""
    cid = _correlation_id.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
) -> None:
    """Configure structlog + stdlib logging.

    Idempotent: safe to call multiple times. The first call wins
    unless ``force`` is added later.

    Args:
        level: One of DEBUG, INFO, WARNING, ERROR.
        fmt: ``"json"`` (default) or ``"console"``.
    """
    global _configured
    if _configured:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    # stdlib handler (used by third-party libs like yfinance, httpx)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # structlog's processor signatures are not exposed as a public
    # Protocol, so we annotate as Any and rely on runtime correctness.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_correlation_id,
    ]

    if fmt == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to ``name``.

    The returned logger is safe to call with arbitrary keyword
    arguments: they become structured fields in the JSON output.
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def new_correlation_id(prefix: str = "run") -> str:
    """Generate a short, human-friendly correlation ID.

    Format: ``<prefix>-<8 hex chars>``, e.g. ``run-3f9a1b2c``.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def get_correlation_id() -> str | None:
    """Return the currently-active correlation ID, if any."""
    return _correlation_id.get()


@contextmanager
def bind_correlation_id(cid: str | None = None, *, prefix: str = "run") -> Iterator[str]:
    """Context manager that binds a correlation ID for the duration of a block.

    Args:
        cid: Explicit ID; if None, a new one is generated.
        prefix: Prefix used when generating a new ID.

    Yields:
        The correlation ID that was bound.
    """
    cid = cid or new_correlation_id(prefix)
    token = _correlation_id.set(cid)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)
