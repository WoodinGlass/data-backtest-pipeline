"""Prefect compat layer. ADR 0016.

The orchestration layer must be usable with or without Prefect
installed. Colab and CI run in ephemeral mode without a Prefect
server; Prefect may or may not be importable. This module
provides decorators that behave like @flow / @task when Prefect
is available, and fall through to plain functions otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


try:
    from prefect import flow as _prefect_flow
    from prefect import task as _prefect_task

    PREFECT_AVAILABLE: bool = True
except ImportError:
    _prefect_flow = None  # type: ignore[assignment]
    _prefect_task = None  # type: ignore[assignment]
    PREFECT_AVAILABLE = False


def is_prefect_available() -> bool:
    """True if the prefect package can be imported."""
    return PREFECT_AVAILABLE


def flow(*args: Any, **kwargs: Any) -> Callable[[F], F]:
    """Decorator that uses Prefect if available, else no-op.

    Supports both bare usage (@flow) and call-with-args
    (@flow(retries=3)).
    """
    if PREFECT_AVAILABLE:
        return _prefect_flow(*args, **kwargs)  # type: ignore[no-any-return]

    if args and callable(args[0]) and len(args) == 1 and not kwargs:
        return args[0]  # bare @flow

    def _decorator(fn: F) -> F:
        return fn

    return _decorator


def task(*args: Any, **kwargs: Any) -> Callable[[F], F]:
    """Decorator that uses Prefect if available, else no-op.

    Same call conventions as :func:`flow`.
    """
    if PREFECT_AVAILABLE:
        return _prefect_task(*args, **kwargs)  # type: ignore[no-any-return]

    if args and callable(args[0]) and len(args) == 1 and not kwargs:
        return args[0]  # bare @task

    def _decorator(fn: F) -> F:
        return fn

    return _decorator


__all__ = [
    "PREFECT_AVAILABLE",
    "flow",
    "is_prefect_available",
    "task",
]
