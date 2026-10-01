"""Unit tests for ingestion/logging.py — pure, no external services."""

import io
import json
import logging as stdlib_logging

import pytest
import structlog

from ingestion.logging import (
    bind_correlation_id,
    configure_logging,
    get_correlation_id,
    get_logger,
    new_correlation_id,
)


def test_new_correlation_id_has_prefix_and_suffix() -> None:
    cid = new_correlation_id()
    assert cid.startswith("run-")
    assert len(cid) == len("run-") + 8
    int(cid.split("-", 1)[1], 16)  # hex


def test_new_correlation_id_is_unique() -> None:
    ids = {new_correlation_id() for _ in range(100)}
    assert len(ids) == 100


def test_correlation_id_is_none_outside_context() -> None:
    assert get_correlation_id() is None


def test_bind_correlation_id_sets_and_restores() -> None:
    assert get_correlation_id() is None
    with bind_correlation_id("run-test1234") as cid:
        assert cid == "run-test1234"
        assert get_correlation_id() == "run-test1234"
    assert get_correlation_id() is None


def test_bind_correlation_id_generates_when_missing() -> None:
    with bind_correlation_id() as cid:
        assert cid.startswith("run-")
        assert get_correlation_id() == cid


def test_nested_contexts_restore_outer() -> None:
    with bind_correlation_id("outer-aaaa1111"):
        assert get_correlation_id() == "outer-aaaa1111"
        with bind_correlation_id("inner-bbbb2222"):
            assert get_correlation_id() == "inner-bbbb2222"
        assert get_correlation_id() == "outer-aaaa1111"


@pytest.fixture
def capture_logs(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Redirect structlog output to an in-memory buffer for assertions."""
    buf = io.StringIO()
    # Reset the configure-once guard so tests can reconfigure freely.
    import ingestion.logging as ing_logging

    monkeypatch.setattr(ing_logging, "_configured", False)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            ing_logging._add_correlation_id,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(stdlib_logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    return buf


def test_log_emits_json(capture_logs: io.StringIO) -> None:
    log = get_logger("test")
    log.info("hello", ticker="AAPL", n=3)
    out = capture_logs.getvalue().strip()
    record = json.loads(out)
    assert record["event"] == "hello"
    assert record["ticker"] == "AAPL"
    assert record["n"] == 3
    assert record["level"] == "info"


def test_log_includes_correlation_id_when_bound(capture_logs: io.StringIO) -> None:
    log = get_logger("test")
    with bind_correlation_id("run-abc12345"):
        log.info("with_cid")
    record = json.loads(capture_logs.getvalue().strip())
    assert record["correlation_id"] == "run-abc12345"


def test_log_omits_correlation_id_when_unbound(capture_logs: io.StringIO) -> None:
    log = get_logger("test")
    log.info("no_cid")
    record = json.loads(capture_logs.getvalue().strip())
    assert "correlation_id" not in record


def test_configure_logging_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    import ingestion.logging as ing_logging

    monkeypatch.setattr(ing_logging, "_configured", False)
    configure_logging(level="DEBUG", fmt="json")
    assert ing_logging._configured is True
    # Second call must be a no-op and must not raise
    configure_logging(level="ERROR", fmt="console")
    assert ing_logging._configured is True
