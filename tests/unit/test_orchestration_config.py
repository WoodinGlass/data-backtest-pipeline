"""Unit tests for orchestration.config. ADR 0016."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestration.config import ORCHESTRATION_VERSION, OrchestrationSettings


class TestDefaults:
    def test_version(self) -> None:
        assert OrchestrationSettings().version == ORCHESTRATION_VERSION == "v1"

    def test_deployment_mode(self) -> None:
        s = OrchestrationSettings()
        assert s.deployment_mode == "ephemeral"
        assert s.prefect_api_url is None
        assert s.prefect_home is None

    def test_schedules(self) -> None:
        s = OrchestrationSettings()
        assert s.daily_cron == "0 22 * * 1-5"
        assert s.weekly_cron == "0 23 * * 0"

    def test_retry_policy(self) -> None:
        s = OrchestrationSettings()
        assert s.retry_attempts == 3
        assert s.retry_delay_seconds == [10, 60, 300]

    def test_alert_defaults(self) -> None:
        s = OrchestrationSettings()
        assert s.alert_webhook_url is None
        assert s.alert_webhook_timeout_seconds == 5.0
        assert s.alert_on_retry is False

    def test_subprocess_defaults(self) -> None:
        s = OrchestrationSettings()
        assert s.subprocess_log_level == "INFO"
        assert s.subprocess_capture_output is True

    def test_repo_root_default(self) -> None:
        assert OrchestrationSettings().repo_root is None


class TestWebhookValidator:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/hook",
            "https://hooks.slack.com/services/T/B/X",
            "https://example.com",
        ],
    )
    def test_accepts_http_https(self, url: str) -> None:
        assert OrchestrationSettings(alert_webhook_url=url).alert_webhook_url == url

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://x",
            "not-a-url",
            "slack.com/hook",
            "",
        ],
    )
    def test_rejects_other_schemes(self, url: str) -> None:
        with pytest.raises(ValidationError) as exc:
            OrchestrationSettings(alert_webhook_url=url)
        assert "alert_webhook_url" in str(exc.value)

    def test_none_allowed(self) -> None:
        assert OrchestrationSettings(alert_webhook_url=None).alert_webhook_url is None


class TestRetryDelayValidator:
    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            OrchestrationSettings(retry_delay_seconds=[])

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            OrchestrationSettings(retry_delay_seconds=[10, -5])

    def test_accepts_single(self) -> None:
        assert OrchestrationSettings(retry_delay_seconds=[1]).retry_delay_seconds == [1]


class TestNumericRanges:
    @pytest.mark.parametrize("attempts", [0, 11])
    def test_retry_attempts_out_of_range(self, attempts: int) -> None:
        with pytest.raises(ValidationError):
            OrchestrationSettings(retry_attempts=attempts)

    @pytest.mark.parametrize("timeout", [0.0, -1.0, 100.0])
    def test_webhook_timeout_out_of_range(self, timeout: float) -> None:
        with pytest.raises(ValidationError):
            OrchestrationSettings(alert_webhook_timeout_seconds=timeout)


class TestEnvOverride:
    def test_deployment_mode(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_ORCH_DEPLOYMENT_MODE", "served")
        assert OrchestrationSettings().deployment_mode == "served"

    def test_retry_attempts(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_ORCH_RETRY_ATTEMPTS", "5")
        assert OrchestrationSettings().retry_attempts == 5

    def test_webhook(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_ORCH_ALERT_WEBHOOK_URL", "https://hooks.example.com/x")
        assert OrchestrationSettings().alert_webhook_url == "https://hooks.example.com/x"

    def test_kwarg_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_ORCH_RETRY_ATTEMPTS", "99")
        assert OrchestrationSettings(retry_attempts=7).retry_attempts == 7


class TestDescribe:
    def test_contains_key_fields(self) -> None:
        s = OrchestrationSettings().describe()
        assert "version=v1" in s
        assert "mode=ephemeral" in s
        assert "retries=3" in s
        assert "webhook=off" in s
        assert "daily_cron='0 22 * * 1-5'" in s
        assert "weekly_cron='0 23 * * 0'" in s
