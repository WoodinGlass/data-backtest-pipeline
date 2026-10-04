"""Unit tests for orchestration.alerts. ADR 0016 section 8."""

from __future__ import annotations

import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

from orchestration import alerts
from orchestration.config import OrchestrationSettings

# ---------------------------------------------------------------------
# build_failure_payload
# ---------------------------------------------------------------------


class TestBuildPayload:
    def test_flow_only(self) -> None:
        p = alerts.build_failure_payload(
            flow_name="daily_refresh",
            flow_run_id="abc123",
            error=ValueError("boom"),
        )
        assert p["flow_name"] == "daily_refresh"
        assert p["flow_run_id"] == "abc123"
        assert p["error_type"] == "ValueError"
        assert p["error_msg"] == "boom"
        assert "daily_refresh failed" in p["text"]
        assert "ValueError" in p["text"]

    def test_with_task(self) -> None:
        p = alerts.build_failure_payload(
            flow_name="daily_refresh",
            task_name="ingest_prices",
            attempt=2,
            error="string error",
            duration_ms=1234.5,
        )
        assert p["task_name"] == "ingest_prices"
        assert p["attempt"] == 2
        assert p["duration_ms"] == 1234.5
        assert "task:ingest_prices" in p["text"]
        assert "attempt 2" in p["text"]

    def test_error_none(self) -> None:
        p = alerts.build_failure_payload("x", error=None)
        assert p["error_type"] == ""
        assert p["error_msg"] == ""

    def test_extra(self) -> None:
        p = alerts.build_failure_payload("x", extra={"foo": "bar"})
        assert p["extra"] == {"foo": "bar"}

    def test_json_serializable(self) -> None:
        p = alerts.build_failure_payload(
            flow_name="f",
            error=RuntimeError("boom"),
            duration_ms=1.0,
        )
        json.dumps(p)


# ---------------------------------------------------------------------
# _safe_url
# ---------------------------------------------------------------------


class TestSafeUrl:
    def test_redacts_path(self) -> None:
        url = "https://hooks.slack.com/services/T00/B00/secret"
        safe = alerts._safe_url(url)
        assert "hooks.slack.com" in safe
        assert "secret" not in safe
        assert safe.endswith("/...")

    def test_handles_no_path(self) -> None:
        assert alerts._safe_url("https://example.com") == "https://example.com/..."

    def test_handles_weird_input(self) -> None:
        assert isinstance(alerts._safe_url("not-a-url"), str)


# ---------------------------------------------------------------------
# post_webhook
# ---------------------------------------------------------------------


def _fake_resp(status: int):
    r = MagicMock()
    r.status = status
    r.__enter__ = lambda self: self
    r.__exit__ = lambda *a: None
    return r


class TestPostWebhook:
    def test_2xx_returns_true(self) -> None:
        with patch(
            "orchestration.alerts.urllib.request.urlopen",
            return_value=_fake_resp(200),
        ):
            assert alerts.post_webhook("https://x.example/hook", {"text": "hi"}, 2.0) is True

    def test_non_2xx_returns_false(self) -> None:
        with patch(
            "orchestration.alerts.urllib.request.urlopen",
            return_value=_fake_resp(500),
        ):
            assert alerts.post_webhook("https://x.example/hook", {"text": "hi"}, 2.0) is False

    def test_urlerror_returns_false(self) -> None:
        with patch(
            "orchestration.alerts.urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns"),
        ):
            assert alerts.post_webhook("https://bad.example/x", {"text": "hi"}, 2.0) is False

    def test_httperror_returns_false(self) -> None:
        err = urllib.error.HTTPError(
            "https://x.example/hook",
            429,
            "too many",
            {},
            None,
        )
        with patch(
            "orchestration.alerts.urllib.request.urlopen",
            side_effect=err,
        ):
            assert alerts.post_webhook("https://x.example/hook", {"text": "hi"}, 2.0) is False

    def test_timeout_returns_false(self) -> None:
        with patch(
            "orchestration.alerts.urllib.request.urlopen",
            side_effect=TimeoutError("timeout"),
        ):
            assert alerts.post_webhook("https://slow.example/x", {"text": "hi"}, 0.1) is False


# ---------------------------------------------------------------------
# make_flow_failure_hook
# ---------------------------------------------------------------------


class _FakeFlow:
    name = "daily_refresh"


class _FakeRun:
    id = "run-123"


class _FakeState:
    message = "boom"


class TestFlowFailureHook:
    def test_no_webhook_logs_only(self) -> None:
        hook = alerts.make_flow_failure_hook(OrchestrationSettings())
        hook(flow=_FakeFlow(), flow_run=_FakeRun(), state=_FakeState())

    def test_with_webhook_posts(self) -> None:
        s = OrchestrationSettings(alert_webhook_url="https://hooks.example.com/x")
        hook = alerts.make_flow_failure_hook(s)
        with patch("orchestration.alerts.post_webhook") as mock_post:
            mock_post.return_value = True
            hook(flow=_FakeFlow(), flow_run=_FakeRun(), state=_FakeState())
            assert mock_post.call_count == 1
            args, _ = mock_post.call_args
            assert args[0] == "https://hooks.example.com/x"
            payload = args[1]
            assert payload["flow_name"] == "daily_refresh"
            assert payload["flow_run_id"] == "run-123"

    def test_hook_swallows_webhook_exceptions(self) -> None:
        s = OrchestrationSettings(alert_webhook_url="https://hooks.example.com/x")
        hook = alerts.make_flow_failure_hook(s)
        with patch(
            "orchestration.alerts.post_webhook",
            side_effect=RuntimeError("webhook explode"),
        ):
            hook(flow=_FakeFlow(), flow_run=_FakeRun(), state=_FakeState())

    def test_hook_handles_none_objects(self) -> None:
        hook = alerts.make_flow_failure_hook(OrchestrationSettings())
        hook()  # no crash


# ---------------------------------------------------------------------
# emit_task_failure
# ---------------------------------------------------------------------


class TestEmitTaskFailure:
    def test_returns_payload_and_posts(self) -> None:
        s = OrchestrationSettings(alert_webhook_url="https://hooks.example.com/x")
        with patch("orchestration.alerts.post_webhook") as mock_post:
            mock_post.return_value = True
            payload = alerts.emit_task_failure(
                settings=s,
                flow_name="daily_refresh",
                task_name="ingest_prices",
                error=RuntimeError("api 503"),
                attempt=2,
                duration_ms=123.4,
            )
            assert payload["flow_name"] == "daily_refresh"
            assert payload["task_name"] == "ingest_prices"
            assert payload["error_type"] == "RuntimeError"
            assert "api 503" in payload["error_msg"]
            assert mock_post.call_count == 1

    def test_no_webhook_skips_post(self) -> None:
        s = OrchestrationSettings()
        with patch("orchestration.alerts.post_webhook") as mock_post:
            alerts.emit_task_failure(s, "f", "t", "err")
            assert mock_post.call_count == 0


# ---------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------


class TestTimer:
    def test_records_elapsed(self) -> None:
        with alerts.Timer() as t:
            time.sleep(0.05)
        assert 40.0 <= t.elapsed_ms <= 500.0

    def test_initial_zero(self) -> None:
        t = alerts.Timer()
        assert t.elapsed_ms == 0.0
