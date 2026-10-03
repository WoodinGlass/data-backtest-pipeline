"""Unit tests for features/assembler.py."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from features.assembler import (
    all_feature_columns,
    assemble_features,
    feature_content_hash,
)
from features.config import FeatureSettings


@pytest.fixture
def settings() -> FeatureSettings:
    return FeatureSettings(
        window_short=3,
        window_medium=5,
        window_long=10,
        window_rsi=5,
    )


def _build_sources(
    *, n: int = 500, seed: int = 0
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    _ = seed
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]

    # Returns: 2 stocks + benchmark
    rows = []
    for tkr, base, sector in [
        ("AAPL", 100.0, "Tech"),
        ("MSFT", 200.0, "Tech"),
        ("SPY", 400.0, "Benchmark"),
    ]:
        rng = np.random.default_rng(hash(tkr) % 2**31)
        log_ret = np.concatenate([[np.nan], rng.normal(0.0, 0.01, size=n - 1)])
        price = base * np.exp(np.cumsum(np.nan_to_num(log_ret)))
        for i, d in enumerate(dates):
            rows.append(
                {
                    "ticker": tkr,
                    "trade_date": d,
                    "sector": sector,
                    "adj_close": float(price[i]),
                    "log_return": float(log_ret[i]),
                    "next_log_return": float(log_ret[i]) if i < n - 1 else np.nan,
                    "next_return_positive": (bool(log_ret[i] > 0) if i < n - 1 else None),
                    "is_benchmark": tkr == "SPY",
                }
            )
    returns_df = pd.DataFrame(rows)

    # Macro
    macro_df = pd.DataFrame(
        {
            "trade_date": dates,
            "macro_fedfunds": 5.0,
            "macro_dgs10": 4.0,
            "macro_dgs2": 4.5,
            "macro_cpiaucsl": 300.0 + np.arange(n) * 0.02,
            "macro_payems": 150_000.0 + np.arange(n),
            "macro_unrate": 4.0,
        }
    )

    # Fundamentals: only AAPL
    fund_rows = []
    for d in dates:
        for tag, val in [
            ("Revenues", 100_000.0),
            ("NetIncomeLoss", 25_000.0),
            ("StockholdersEquity", 500_000.0),
            ("PaymentsToAcquirePropertyPlantAndEquipment", 10_000.0),
            ("EntityNumberOfEmployees", 150_000),
        ]:
            fund_rows.append({"ticker": "AAPL", "trade_date": d, "tag": tag, "value": val})
    fund_df = pd.DataFrame(fund_rows)

    return returns_df, macro_df, fund_df


def test_assemble_returns_all_feature_columns(settings: FeatureSettings) -> None:
    ret, mac, fund = _build_sources()
    out = assemble_features(ret, mac, fund, settings=settings)
    for col in all_feature_columns(settings):
        assert col in out.columns, f"missing {col}"


def test_assemble_preserves_labels(settings: FeatureSettings) -> None:
    ret, mac, fund = _build_sources()
    out = assemble_features(ret, mac, fund, settings=settings)
    assert "next_log_return" in out.columns
    assert "next_return_positive" in out.columns


def test_assemble_preserves_row_count(settings: FeatureSettings) -> None:
    ret, mac, fund = _build_sources()
    out = assemble_features(ret, mac, fund, settings=settings)
    assert len(out) == len(ret)


def test_assemble_sorted_by_ticker_then_date(
    settings: FeatureSettings,
) -> None:
    ret, mac, fund = _build_sources()
    out = assemble_features(ret, mac, fund, settings=settings)
    keys = list(zip(out["ticker"], out["trade_date"], strict=True))
    assert keys == sorted(keys)


def test_hash_deterministic(settings: FeatureSettings) -> None:
    ret, mac, fund = _build_sources()
    out = assemble_features(ret, mac, fund, settings=settings)
    h1 = feature_content_hash(out)
    h2 = feature_content_hash(out)
    assert h1 == h2


def test_hash_insensitive_to_row_order(settings: FeatureSettings) -> None:
    ret, mac, fund = _build_sources()
    out = assemble_features(ret, mac, fund, settings=settings)
    h1 = feature_content_hash(out)
    shuffled = out.sample(frac=1.0, random_state=0).reset_index(drop=True)
    h2 = feature_content_hash(shuffled)
    assert h1 == h2


def test_hash_sensitive_to_value(settings: FeatureSettings) -> None:
    ret, mac, fund = _build_sources()
    out = assemble_features(ret, mac, fund, settings=settings)
    h1 = feature_content_hash(out)
    modified = out.copy()
    modified.loc[100, "px_ret_1d"] = 999.999
    h2 = feature_content_hash(modified)
    assert h1 != h2
