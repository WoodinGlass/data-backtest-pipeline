"""Unit tests for ingestion/macro/client.py — no real FRED calls."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from ingestion.macro.client import FredClient, MacroFetchError
from ingestion.macro.config import MacroSettings


@pytest.fixture
def settings() -> MacroSettings:
    return MacroSettings(
        fred_api_key="test-key",
        macro_rate_limit_seconds=0.0,
        macro_max_retries=1,
        macro_retry_min_seconds=0.001,
        macro_retry_max_seconds=0.01,
    )


@pytest.fixture
def client(settings: MacroSettings) -> FredClient:
    return FredClient(
        rate_limit_seconds=0.0,
        max_retries=1,
        retry_min_seconds=0.001,
        retry_max_seconds=0.01,
        sleep=lambda s: None,
        settings=settings,
    )


# ─── Metadata ──────────────────────────────────────────────
def test_fetch_metadata_returns_first_series(client: FredClient, monkeypatch) -> None:
    fake_data = {"seriess": [{"id": "FEDFUNDS", "title": "Fed Funds", "frequency": "M"}]}
    monkeypatch.setattr(client, "_get_json", lambda path, params: fake_data)
    meta = client.fetch_metadata("FEDFUNDS")
    assert meta["id"] == "FEDFUNDS"
    assert meta["title"] == "Fed Funds"


def test_fetch_metadata_empty_on_unknown_series(client: FredClient, monkeypatch) -> None:
    monkeypatch.setattr(client, "_get_json", lambda path, params: {"seriess": []})
    meta = client.fetch_metadata("NOPE")
    assert meta == {}


# ─── Vintages: core vintage reconstruction ─────────────────
def test_fetch_vintages_builds_cumulative_snapshots(client: FredClient, monkeypatch) -> None:
    # Mock FRED's row format: one row per (obs_date, [rt_start, rt_end])
    fake = {
        "observations": [
            # obs 2024-01, first published 2024-02-01, still valid (through forever)
            {
                "date": "2024-01-01",
                "realtime_start": "2024-02-01",
                "realtime_end": "9999-12-31",
                "value": "5.33",
            },
            # obs 2024-02, first published 2024-03-01
            {
                "date": "2024-02-01",
                "realtime_start": "2024-03-01",
                "realtime_end": "9999-12-31",
                "value": "5.33",
            },
            # obs 2024-03, first published 2024-04-01
            {
                "date": "2024-03-01",
                "realtime_start": "2024-04-01",
                "realtime_end": "9999-12-31",
                "value": "5.33",
            },
        ]
    }
    monkeypatch.setattr(client, "_get_json", lambda path, params: fake)
    snapshots = client.fetch_vintages(
        "FEDFUNDS",
        observation_start=date(2024, 1, 1),
        observation_end=date(2024, 6, 30),
    )
    # Expect 3 vintages: 2024-02-01, 2024-03-01, 2024-04-01
    assert len(snapshots) == 3
    assert snapshots[0].vintage_date == date(2024, 2, 1)
    assert snapshots[0].n_observations == 1  # cumulative: only jan
    assert snapshots[1].vintage_date == date(2024, 3, 1)
    assert snapshots[1].n_observations == 2  # jan + feb
    assert snapshots[2].vintage_date == date(2024, 4, 1)
    assert snapshots[2].n_observations == 3  # jan + feb + mar


def test_fetch_vintages_picks_most_recent_revision(client: FredClient, monkeypatch) -> None:
    # Same obs_date, two revisions: v1 valid [2024-02-01, 2024-03-01),
    # v2 valid [2024-03-01, forever).
    fake = {
        "observations": [
            {
                "date": "2024-01-01",
                "realtime_start": "2024-02-01",
                "realtime_end": "2024-02-29",
                "value": "5.00",
            },
            {
                "date": "2024-01-01",
                "realtime_start": "2024-03-01",
                "realtime_end": "9999-12-31",
                "value": "5.33",
            },
        ]
    }
    monkeypatch.setattr(client, "_get_json", lambda path, params: fake)
    snapshots = client.fetch_vintages(
        "FEDFUNDS",
        observation_start=date(2024, 1, 1),
    )
    assert len(snapshots) == 2
    assert snapshots[0].vintage_date == date(2024, 2, 1)
    assert snapshots[0].observations[0].value == pytest.approx(5.00)
    assert snapshots[1].vintage_date == date(2024, 3, 1)
    assert snapshots[1].observations[0].value == pytest.approx(5.33)


def test_fetch_vintages_returns_empty_on_no_data(client: FredClient, monkeypatch) -> None:
    monkeypatch.setattr(client, "_get_json", lambda path, params: {"observations": []})
    assert client.fetch_vintages("NOPE", observation_start=date(2024, 1, 1)) == []


def test_fetch_vintages_skips_bad_date_rows(client: FredClient, monkeypatch) -> None:
    fake = {
        "observations": [
            {
                "date": "garbage",
                "realtime_start": "2024-02-01",
                "realtime_end": "9999-12-31",
                "value": "5.0",
            },
            {
                "date": "2024-01-01",
                "realtime_start": "2024-02-01",
                "realtime_end": "9999-12-31",
                "value": "5.33",
            },
        ]
    }
    monkeypatch.setattr(client, "_get_json", lambda path, params: fake)
    snapshots = client.fetch_vintages("FEDFUNDS", observation_start=date(2024, 1, 1))
    assert len(snapshots) == 1
    assert snapshots[0].n_observations == 1


# ─── Error paths ───────────────────────────────────────────
def test_get_once_raises_without_api_key(settings: MacroSettings, monkeypatch) -> None:
    settings_no_key = MacroSettings(
        fred_api_key="",
        macro_rate_limit_seconds=0.0,
        macro_max_retries=1,
    )
    c = FredClient(rate_limit_seconds=0.0, max_retries=1, settings=settings_no_key)
    with pytest.raises(MacroFetchError, match="FRED_API_KEY is not set"):
        c._get_once("/fred/series", {})


def test_get_once_raises_on_http_error(monkeypatch) -> None:
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "internal error"
    fake_client = MagicMock()
    fake_client.__enter__ = lambda self: fake_client
    fake_client.__exit__ = lambda self, *a: None
    fake_client.get = lambda url, params: fake_resp
    monkeypatch.setattr("ingestion.macro.client.httpx.Client", lambda **kw: fake_client)

    c = FredClient(
        rate_limit_seconds=0.0,
        max_retries=1,
        settings=MacroSettings(fred_api_key="test-key"),
    )
    with pytest.raises(MacroFetchError, match="FRED returned 500"):
        c._get_once("/fred/series", {})
