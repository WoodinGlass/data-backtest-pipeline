"""MLflow client utilities for M6. ADR 0015 §1, §3, §6, §8.

Optional import of mlflow. Path resolution for the SQLite backend.
Idempotent experiment setup. Thin context manager around
mlflow.start_run. Small git/pip helpers.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracking.config import TrackingSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Optional MLflow import
# ---------------------------------------------------------------------

try:
    import mlflow
    from mlflow.tracking import MlflowClient

    MLFLOW_AVAILABLE: bool = True
except ImportError:
    mlflow = None  # type: ignore[assignment]
    MlflowClient = None  # type: ignore[assignment]
    MLFLOW_AVAILABLE = False


def is_mlflow_available() -> bool:
    """True if the mlflow package can be imported."""
    return MLFLOW_AVAILABLE


# ---------------------------------------------------------------------
# Repo + path helpers
# ---------------------------------------------------------------------


def get_repo_root(start: Path | None = None) -> Path:
    """Find repo root (contains .git or pyproject.toml)."""
    cur = (start or Path.cwd()).resolve()
    for _ in range(15):
        if (cur / ".git").exists() or (cur / "pyproject.toml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError(
        "Could not locate repo root from "
        + str(start or Path.cwd())
        + "; expected a .git or pyproject.toml somewhere above."
    )


def resolve_tracking_uri(
    settings: TrackingSettings,
    repo_root: Path | None = None,
) -> str:
    """Return tracking URI with relative SQLite path made absolute."""
    uri = settings.tracking_uri
    if uri.startswith("sqlite:///") and not uri.startswith("sqlite:////"):
        rel = uri[len("sqlite:///") :]
        root = repo_root or get_repo_root()
        abs_path = (root / rel).resolve()
        return "sqlite:///" + str(abs_path)
    return uri


def resolve_artifact_root(
    settings: TrackingSettings,
    repo_root: Path | None = None,
) -> str | None:
    """Return absolute artifact root, or None to use MLflow default."""
    if settings.artifact_root is None:
        return None
    p = Path(settings.artifact_root)
    if not p.is_absolute():
        root = repo_root or get_repo_root()
        p = (root / p).resolve()
    return str(p)


# ---------------------------------------------------------------------
# Experiment setup
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentInfo:
    tracking_uri: str
    artifact_location: str | None
    experiment_name: str
    experiment_id: str


def setup_experiment(
    settings: TrackingSettings,
    repo_root: Path | None = None,
) -> ExperimentInfo | None:
    """Configure MLflow tracking + experiment. Idempotent."""
    if not settings.enabled:
        logger.debug("setup_experiment: tracking disabled")
        return None
    if not MLFLOW_AVAILABLE:
        logger.warning(
            "setup_experiment: tracking enabled but mlflow not installed; "
            "install with pip install -e .[tracking]."
        )
        return None

    uri = resolve_tracking_uri(settings, repo_root)
    artifact_root = resolve_artifact_root(settings, repo_root)

    mlflow.set_tracking_uri(uri)

    exp = mlflow.get_experiment_by_name(settings.experiment_name)
    if exp is None:
        exp_id = mlflow.create_experiment(
            settings.experiment_name,
            artifact_location=artifact_root,
        )
    else:
        exp_id = exp.experiment_id

    mlflow.set_experiment(settings.experiment_name)

    return ExperimentInfo(
        tracking_uri=uri,
        artifact_location=artifact_root or (exp.artifact_location if exp is not None else None),
        experiment_name=settings.experiment_name,
        experiment_id=str(exp_id),
    )


# ---------------------------------------------------------------------
# Run context manager
# ---------------------------------------------------------------------


@contextmanager
def start_tracking_run(
    run_name: str,
    settings: TrackingSettings,
    extra_tags: dict[str, str] | None = None,
    repo_root: Path | None = None,
) -> Iterator[Any | None]:
    """Start an MLflow run with our conventions.

    Yields the ActiveRun when tracking is enabled and MLflow is
    available; yields None otherwise.
    """
    if not settings.enabled:
        yield None
        return
    if not MLFLOW_AVAILABLE:
        logger.warning("start_tracking_run: tracking enabled but mlflow missing; yielding None.")
        yield None
        return

    info = setup_experiment(settings, repo_root)
    if info is None:
        yield None
        return

    tags: dict[str, str] = {}
    tags.update(settings.tags_extra)
    if extra_tags:
        tags.update(extra_tags)
    tags["mlflow.runName"] = run_name

    with mlflow.start_run(run_name=run_name, tags=tags) as run:
        yield run


# ---------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, ""
    return cp.returncode, (cp.stdout or "")


def get_git_info(repo_root: Path | None = None) -> dict[str, str]:
    """Return dict with sha, branch, dirty. Best-effort."""
    out: dict[str, str] = {"sha": "", "branch": "", "dirty": "unknown"}
    if repo_root is None:
        try:
            repo_root = get_repo_root()
        except RuntimeError:
            return out
    root = repo_root

    rc, sha = _run_git(["rev-parse", "HEAD"], root)
    if rc == 0:
        out["sha"] = sha.strip()

    rc, branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if rc == 0:
        out["branch"] = branch.strip()

    rc, status = _run_git(["status", "--porcelain"], root)
    if rc == 0:
        out["dirty"] = "true" if status.strip() else "false"

    return out


def get_git_diff(
    repo_root: Path | None = None,
    max_bytes: int = 100_000,
) -> str | None:
    """Return git diff HEAD, or None if clean. Truncated if oversized."""
    if repo_root is None:
        try:
            repo_root = get_repo_root()
        except RuntimeError:
            return None
    root = repo_root
    rc, diff = _run_git(["diff", "HEAD"], root)
    if rc != 0 or not diff.strip():
        return None
    if len(diff) > max_bytes:
        half = max_bytes // 2
        head = diff[:half]
        tail = diff[-half:]
        marker = (
            "# truncated at " + str(max_bytes) + " bytes " + "(original " + str(len(diff)) + ")\n"
        )
        sep = "\n\n# ... (middle omitted) ...\n\n"
        return marker + head + sep + tail + "\n"
    return diff


# ---------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------


def get_env_snippet(max_lines: int = 50) -> str:
    """Return a short pip-freeze style snapshot."""
    lines: list[str] = []
    try:
        from importlib.metadata import distributions

        for dist in distributions():
            name = dist.metadata.get("Name") if dist.metadata else None
            version = dist.version
            if name and version:
                lines.append(name + "==" + version)
    except Exception as e:
        return "# failed to enumerate packages: " + repr(e) + "\n"

    lines.sort(key=lambda s: s.lower())
    if len(lines) > max_lines:
        extra = len(lines) - max_lines
        lines = [*lines[:max_lines], "# ... (" + str(extra) + " more)"]

    header = (
        "# python " + sys.version.split()[0] + "\n" + "# platform " + platform.platform() + "\n"
    )
    return header + "\n".join(lines) + "\n"


def is_colab() -> bool:
    """True inside Google Colab."""
    return "google.colab" in sys.modules or Path("/content").is_dir()


__all__ = [
    "MLFLOW_AVAILABLE",
    "ExperimentInfo",
    "get_env_snippet",
    "get_git_diff",
    "get_git_info",
    "get_repo_root",
    "is_colab",
    "is_mlflow_available",
    "resolve_artifact_root",
    "resolve_tracking_uri",
    "setup_experiment",
    "start_tracking_run",
]
