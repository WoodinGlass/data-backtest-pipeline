"""Subprocess runner for orchestration tasks. ADR 0016 section 4.

All pipeline flows are thin wrappers around existing CLI entry
points. This module provides a single, well-behaved function that
runs one command as a subprocess and returns a small result object.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float
    cmd: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _tail(text: str, n: int) -> str:
    if not text:
        return ""
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


def run_command(
    cmd: list[str],
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    *,
    check: bool = True,
    log_tail_lines: int = 10,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Run cmd as a subprocess. Returns CommandResult.

    If check is True (the default) and the command exits non-zero,
    raises subprocess.CalledProcessError with the tail of stderr
    attached so hooks and Prefect retries see a real exception.
    """
    if not cmd:
        raise ValueError("run_command: empty command list")

    cwd_str = str(cwd) if cwd is not None else str(Path.cwd())
    full_env = {**os.environ, **(env or {})}

    logger.info("run: %s  (cwd=%s)", " ".join(cmd), cwd_str)
    t0 = time.monotonic()

    try:
        cp = subprocess.run(
            cmd,
            cwd=cwd_str,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as e:
        elapsed = (time.monotonic() - t0) * 1000.0
        logger.error("run: command not found: %s", cmd[0])
        if check:
            raise
        return CommandResult(
            returncode=127,
            stdout="",
            stderr=str(e),
            elapsed_ms=elapsed,
            cmd=list(cmd),
        )
    except subprocess.TimeoutExpired as e:
        elapsed = (time.monotonic() - t0) * 1000.0
        logger.error(
            "run: timeout after %.1fs: %s",
            timeout_seconds or 0.0,
            " ".join(cmd),
        )
        if check:
            raise
        return CommandResult(
            returncode=124,
            stdout=e.stdout or "",
            stderr=e.stderr or "timeout",
            elapsed_ms=elapsed,
            cmd=list(cmd),
        )

    elapsed = (time.monotonic() - t0) * 1000.0
    result = CommandResult(
        returncode=cp.returncode,
        stdout=cp.stdout or "",
        stderr=cp.stderr or "",
        elapsed_ms=elapsed,
        cmd=list(cmd),
    )

    logger.info(
        "run: exit=%s  elapsed=%.1fms  cmd=%s",
        cp.returncode,
        elapsed,
        " ".join(cmd),
    )
    if cp.returncode != 0:
        tail = _tail(result.stderr, log_tail_lines)
        if tail:
            logger.error("run: stderr tail:\n%s", tail)
        if check:
            raise subprocess.CalledProcessError(
                cp.returncode,
                cmd,
                output=cp.stdout,
                stderr=cp.stderr,
            )

    return result


def python_executable() -> str:
    """Path to the current Python interpreter (for -m invocations)."""
    return sys.executable


__all__ = ["CommandResult", "python_executable", "run_command"]
