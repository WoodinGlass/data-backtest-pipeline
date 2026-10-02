"""Unit tests for ingestion/sec/client.py — no real SEC calls."""

import pytest

from ingestion.sec.client import (
    SEC_API_BASE_URL,
    SEC_STATIC_BASE_URL,
    SecClient,
    SecFetchError,
)
from ingestion.sec.config import SecSettings


@pytest.fixture
def settings() -> SecSettings:
    return SecSettings(
        sec_user_agent="test-app test@example.com",
        sec_rate_limit_seconds=0.0,
        sec_max_retries=1,
        sec_retry_min_seconds=0.001,
        sec_retry_max_seconds=0.01,
    )


@pytest.fixture
def client(settings: SecSettings) -> SecClient:
    return SecClient(
        rate_limit_seconds=0.0,
        max_retries=1,
        retry_min_seconds=0.001,
        retry_max_seconds=0.01,
        sleep=lambda s: None,
        settings=settings,
    )


# ─── CIK map ───────────────────────────────────────────────
def test_get_cik_uses_static_base_url(client: SecClient, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_get(path: str, *, base_url: str) -> dict:
        calls.append((path, base_url))
        return {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
        }

    monkeypatch.setattr(client, "_get_json", fake_get)
    cik = client.get_cik("AAPL")
    assert cik == 320193
    assert calls == [("/files/company_tickers.json", SEC_STATIC_BASE_URL)]


def test_get_cik_case_insensitive(client: SecClient, monkeypatch) -> None:
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda p, *, base_url: {"0": {"cik_str": 320193, "ticker": "AAPL"}},
    )
    assert client.get_cik("aapl") == 320193
    assert client.get_cik("AAPL") == 320193


def test_get_cik_returns_none_for_unknown(client: SecClient, monkeypatch) -> None:
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda p, *, base_url: {"0": {"cik_str": 320193, "ticker": "AAPL"}},
    )
    assert client.get_cik("NOTREAL") is None


def test_get_cik_caches_map(client: SecClient, monkeypatch) -> None:
    calls = []

    def fake_get(path: str, *, base_url: str) -> dict:
        calls.append(path)
        return {"0": {"cik_str": 1, "ticker": "AAPL"}}

    monkeypatch.setattr(client, "_get_json", fake_get)
    client.get_cik("AAPL")
    client.get_cik("AAPL")
    assert calls.count("/files/company_tickers.json") == 1


# ─── Company facts fetch ───────────────────────────────────
def test_fetch_company_facts_uses_api_base_url(client: SecClient, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_get(path: str, *, base_url: str) -> dict:
        calls.append((path, base_url))
        if path == "/files/company_tickers.json":
            return {"0": {"cik_str": 320193, "ticker": "AAPL"}}
        return {
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2024-01-01",
                                    "end": "2024-03-31",
                                    "val": 90753000000.0,
                                    "fy": 2024,
                                    "fp": "Q2",
                                    "form": "10-Q",
                                    "filed": "2024-05-03",
                                    "frame": "CY2024Q1",
                                }
                            ]
                        }
                    }
                }
            },
        }

    monkeypatch.setattr(client, "_get_json", fake_get)
    snapshot = client.fetch_company_facts("AAPL")

    assert snapshot.entity_name == "Apple Inc."
    assert snapshot.n_facts == 1
    # Verify the companyfacts call used data.sec.gov
    cf_calls = [c for c in calls if "companyfacts" in c[0]]
    assert len(cf_calls) == 1
    assert cf_calls[0][1] == SEC_API_BASE_URL


def test_fetch_unknown_ticker_raises(client: SecClient, monkeypatch) -> None:
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda p, *, base_url: {"0": {"cik_str": 1, "ticker": "AAPL"}},
    )
    with pytest.raises(SecFetchError, match="Unknown ticker"):
        client.fetch_company_facts("NOTREAL")


def test_fetch_missing_facts_object_raises(client: SecClient, monkeypatch) -> None:
    def fake_get(path: str, *, base_url: str) -> dict:
        if "company_tickers" in path:
            return {"0": {"cik_str": 1, "ticker": "AAPL"}}
        return {"cik": 1, "entityName": "X"}  # no facts key

    monkeypatch.setattr(client, "_get_json", fake_get)
    with pytest.raises(SecFetchError, match="No 'facts'"):
        client.fetch_company_facts("AAPL")


def test_parse_skips_rows_without_required_fields(client: SecClient, monkeypatch) -> None:
    def fake_get(path: str, *, base_url: str) -> dict:
        if "company_tickers" in path:
            return {"0": {"cik_str": 1, "ticker": "AAPL"}}
        return {
            "cik": 1,
            "entityName": "X",
            "facts": {
                "us-gaap": {
                    "Tag": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2024-01-01",
                                    "filed": "2024-02-01",
                                    "form": "10-K",
                                    "val": 1.0,
                                },  # ok
                                {"end": "2024-01-01", "val": 2.0},  # missing filed, form
                                {"filed": "2024-02-01", "form": "10-K", "val": 3.0},  # missing end
                                {
                                    "start": "invalid",
                                    "end": "2024-01-01",
                                    "filed": "2024-02-01",
                                    "form": "10-K",
                                    "val": 4.0,
                                },  # ok, start invalid -> None
                            ]
                        }
                    }
                }
            },
        }

    monkeypatch.setattr(client, "_get_json", fake_get)
    snapshot = client.fetch_company_facts("AAPL")
    # Only 2 rows have all required fields (end, filed, form)
    assert snapshot.n_facts == 2


# ─── _get_once ─────────────────────────────────────────────
def test_get_once_raises_without_user_agent(monkeypatch) -> None:
    s = SecSettings(sec_user_agent="", sec_rate_limit_seconds=0.0)
    c = SecClient(rate_limit_seconds=0.0, max_retries=1, settings=s)
    with pytest.raises(SecFetchError, match="SEC_USER_AGENT"):
        c._get_once("/x", base_url="https://www.sec.gov")


def test_get_once_raises_on_malformed_user_agent(monkeypatch) -> None:
    s = SecSettings(
        sec_user_agent="nope-no-email",
        sec_rate_limit_seconds=0.0,
    )
    c = SecClient(rate_limit_seconds=0.0, max_retries=1, settings=s)
    with pytest.raises(SecFetchError, match="SEC_USER_AGENT"):
        c._get_once("/x", base_url="https://www.sec.gov")
