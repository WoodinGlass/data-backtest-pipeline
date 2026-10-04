"""Unit tests for tracking.client. ADR 0015 §1, §3, §6, §8."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracking import client
from tracking.config import TrackingSettings


class TestImportGuard:
    def test_availability_constant(self) -> None:
        assert isinstance(client.MLFLOW_AVAILABLE, bool)
        assert client.is_mlflow_available() == client.MLFLOW_AVAILABLE


class TestRepoRoot:
    def test_finds_repo_root(self) -> None:
        root = client.get_repo_root()
        assert (root / "pyproject.toml").exists()

    def test_works_from_subdir(self) -> None:
        root = client.get_repo_root()
        sub = root / "backtest"
        assert client.get_repo_root(start=sub) == root

    def test_raises_when_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="repo root"):
            client.get_repo_root(start=tmp_path)


class TestUriResolution:
    def test_relative_sqlite_becomes_absolute(self) -> None:
        ts = TrackingSettings(tracking_uri="sqlite:///mlruns/mlflow.db")
        uri = client.resolve_tracking_uri(ts)
        assert uri.startswith("sqlite:////")
        assert uri.endswith("/mlruns/mlflow.db")
        assert "mlruns/mlflow.db" in uri

    def test_absolute_sqlite_unchanged(self) -> None:
        ts = TrackingSettings(tracking_uri="sqlite:////tmp/x.db")
        assert client.resolve_tracking_uri(ts) == "sqlite:////tmp/x.db"

    def test_http_unchanged(self) -> None:
        ts = TrackingSettings(tracking_uri="http://localhost:5000")
        assert client.resolve_tracking_uri(ts) == "http://localhost:5000"

    def test_explicit_repo_root(self, tmp_path: Path) -> None:
        ts = TrackingSettings(tracking_uri="sqlite:///subdir/mlflow.db")
        uri = client.resolve_tracking_uri(ts, repo_root=tmp_path)
        assert str(tmp_path) in uri


class TestArtifactRoot:
    def test_none_returns_none(self) -> None:
        assert client.resolve_artifact_root(TrackingSettings()) is None

    def test_relative_made_absolute(self, tmp_path: Path) -> None:
        ts = TrackingSettings(artifact_root="arts")
        p = client.resolve_artifact_root(ts, repo_root=tmp_path)
        assert p is not None and Path(p).is_absolute()

    def test_absolute_unchanged(self) -> None:
        ts = TrackingSettings(artifact_root="/tmp/absolute-arts")
        assert client.resolve_artifact_root(ts) == "/tmp/absolute-arts"


class TestSetupExperiment:
    def test_disabled_returns_none(self) -> None:
        assert client.setup_experiment(TrackingSettings(enabled=False)) is None

    def test_enabled_without_mlflow_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(client, "MLFLOW_AVAILABLE", False)
        assert client.setup_experiment(TrackingSettings(enabled=True)) is None


class TestStartTrackingRun:
    def test_disabled_yields_none(self) -> None:
        with client.start_tracking_run("x", TrackingSettings(enabled=False)) as run:
            assert run is None

    def test_enabled_without_mlflow_yields_none(self, monkeypatch) -> None:
        monkeypatch.setattr(client, "MLFLOW_AVAILABLE", False)
        with client.start_tracking_run("x", TrackingSettings(enabled=True)) as run:
            assert run is None


class TestGitInfo:
    def test_returns_expected_keys(self) -> None:
        gi = client.get_git_info()
        assert set(gi.keys()) == {"sha", "branch", "dirty"}

    def test_dirty_is_string_tristate(self) -> None:
        gi = client.get_git_info()
        assert gi["dirty"] in ("true", "false", "unknown")

    def test_no_raise_outside_repo(self, tmp_path: Path) -> None:
        # Outside a repo, should return empty defaults — not raise.
        gi = client.get_git_info(repo_root=tmp_path)
        assert isinstance(gi, dict)
        assert set(gi.keys()) == {"sha", "branch", "dirty"}


class TestGitDiff:
    def test_returns_none_when_clean_or_no_repo(self, tmp_path: Path) -> None:
        # tmp_path is not a git repo -> None, no raise.
        assert client.get_git_diff(repo_root=tmp_path) is None

    def test_truncation_header(self) -> None:
        # Only assert if there is actually a diff available.
        d = client.get_git_diff(max_bytes=200)
        if d is None:
            pytest.skip("working tree is clean; nothing to truncate")
        assert "truncated at 200 bytes" in d


class TestEnvSnippet:
    def test_starts_with_python_header(self) -> None:
        s = client.get_env_snippet(max_lines=5)
        assert s.startswith("# python")

    def test_truncation_marker(self) -> None:
        s = client.get_env_snippet(max_lines=3)
        assert "more" in s or s.count("\n") <= 8


class TestIsColab:
    def test_returns_bool(self) -> None:
        assert isinstance(client.is_colab(), bool)
