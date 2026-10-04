"""Unit tests for tracking.config. ADR 0015."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracking.config import TRACKING_VERSION, TrackingSettings


class TestDefaults:
    def test_version(self) -> None:
        assert TrackingSettings().version == TRACKING_VERSION == "v1"

    def test_backend_defaults(self) -> None:
        ts = TrackingSettings()
        assert ts.tracking_uri == "sqlite:///mlruns/mlflow.db"
        assert ts.artifact_root is None

    def test_experiment_default(self) -> None:
        assert TrackingSettings().experiment_name == "daily-direction"

    def test_opt_in_default(self) -> None:
        assert TrackingSettings().enabled is False

    def test_metrics_defaults(self) -> None:
        ts = TrackingSettings()
        assert ts.log_fold_metrics is True
        assert ts.max_fold_metrics == 50

    def test_artifacts_defaults(self) -> None:
        ts = TrackingSettings()
        assert ts.log_git_diff is True
        assert ts.git_diff_max_bytes == 100_000
        assert ts.log_env is True
        assert ts.env_max_lines == 50

    def test_registry_defaults(self) -> None:
        ts = TrackingSettings()
        assert ts.register_model is True
        assert ts.model_name == "daily-direction-model"
        assert ts.challenger_alias == "challenger"
        assert ts.champion_alias == "champion"

    def test_tags_extra_default(self) -> None:
        assert TrackingSettings().tags_extra == {}


class TestUriValidator:
    @pytest.mark.parametrize(
        "uri",
        [
            "sqlite:///mlruns/mlflow.db",
            "sqlite:////abs/path/mlflow.db",
            "file:///tmp/mlruns",
            "http://localhost:5000",
            "https://mlflow.example.com",
            "postgresql://user:pw@host/db",
            "postgres://user:pw@host/db",
            "mysql://user:pw@host/db",
            "databricks",
            "databricks://profile",
        ],
    )
    def test_accepts_valid_uri(self, uri: str) -> None:
        assert TrackingSettings(tracking_uri=uri).tracking_uri == uri

    @pytest.mark.parametrize(
        "uri",
        [
            "not-a-uri",
            "ftp://x",
            "://missing",
            "/just/a/path",
        ],
    )
    def test_rejects_invalid_uri(self, uri: str) -> None:
        with pytest.raises(ValidationError) as exc:
            TrackingSettings(tracking_uri=uri)
        assert "tracking_uri" in str(exc.value)


class TestEnvOverride:
    def test_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_TRACK_ENABLED", "true")
        assert TrackingSettings().enabled is True

    def test_experiment_name(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_TRACK_EXPERIMENT_NAME", "my-exp")
        assert TrackingSettings().experiment_name == "my-exp"

    def test_tracking_uri(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_TRACK_TRACKING_URI", "sqlite:////tmp/foo.db")
        assert TrackingSettings().tracking_uri == "sqlite:////tmp/foo.db"

    def test_kwarg_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_TRACK_MODEL_NAME", "from-env")
        assert TrackingSettings(model_name="from-kwarg").model_name == "from-kwarg"


class TestDescribe:
    def test_contains_key_fields(self) -> None:
        s = TrackingSettings().describe()
        assert "version=v1" in s
        assert "enabled=False" in s
        assert "sqlite:///mlruns/mlflow.db" in s
        assert "daily-direction" in s
        assert "daily-direction-model" in s
