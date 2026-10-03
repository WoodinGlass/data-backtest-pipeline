"""Unit tests for features/fundamental_features.py."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from features.config import FeatureSettings
from features.fundamental_features import (
    add_fundamental_features,
    fundamental_feature_columns,
)


def _prices_df(*, ticker: str = "AAPL", n: int = 500) -> pd.DataFrame:
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({"ticker": [ticker] * n, "trade_date": dates})


def _fund_df(
    *,
    ticker: str = "AAPL",
    n: int = 500,
    rev_tag: str = "Revenues",
) -> pd.DataFrame:
    """Fundamental facts, one value per day for simplicity."""
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    rows = []
    for d in dates:
        rev = 100_000.0
        ni = 25_000.0
        eq = 500_000.0
        capex = 10_000.0
        emp = 150_000
        for tag, val in [
            (rev_tag, rev),
            ("NetIncomeLoss", ni),
            ("StockholdersEquity", eq),
            ("PaymentsToAcquirePropertyPlantAndEquipment", capex),
            ("EntityNumberOfEmployees", emp),
        ]:
            rows.append({"ticker": ticker, "trade_date": d, "tag": tag, "value": val})
    return pd.DataFrame(rows)


@pytest.fixture
def settings() -> FeatureSettings:
    return FeatureSettings()


# ─── contract ───────────────────────────────────────────────
def test_missing_df_columns_raises(settings: FeatureSettings) -> None:
    df = pd.DataFrame({"foo": [1]})
    with pytest.raises(ValueError, match="df missing"):
        add_fundamental_features(df, _fund_df(), settings=settings)


def test_missing_fund_columns_raises(settings: FeatureSettings) -> None:
    fund = _fund_df().drop(columns=["value"])
    with pytest.raises(ValueError, match="int_fund_df missing"):
        add_fundamental_features(_prices_df(), fund, settings=settings)


def test_returns_all_declared_features(settings: FeatureSettings) -> None:
    out = add_fundamental_features(_prices_df(), _fund_df(), settings=settings)
    for col in fundamental_feature_columns(settings):
        assert col in out.columns


def test_empty_facts_yields_all_nan(settings: FeatureSettings) -> None:
    fund = pd.DataFrame(
        {
            "ticker": ["AAPL"],
            "trade_date": [date(2020, 1, 1)],
            "tag": ["SomeIrrelevantTag"],
            "value": [1.0],
        }
    )
    out = add_fundamental_features(_prices_df(n=10), fund, settings=settings)
    for col in fundamental_feature_columns(settings):
        assert out[col].isna().all()


# ─── per-feature ────────────────────────────────────────────
def test_net_margin_correct(settings: FeatureSettings) -> None:
    out = add_fundamental_features(_prices_df(), _fund_df(), settings=settings)
    margin = out["fd_net_margin"].dropna().iloc[0]
    assert margin == pytest.approx(0.25)


def test_capex_intensity_correct(settings: FeatureSettings) -> None:
    out = add_fundamental_features(_prices_df(), _fund_df(), settings=settings)
    capex = out["fd_capex_intensity"].dropna().iloc[0]
    assert capex == pytest.approx(0.10)


def test_roe_correct(settings: FeatureSettings) -> None:
    out = add_fundamental_features(_prices_df(), _fund_df(), settings=settings)
    roe = out["fd_roe"].dropna().iloc[0]
    assert roe == pytest.approx(25_000.0 / 500_000.0)


def test_revenue_coalesce_asc606(settings: FeatureSettings) -> None:
    out = add_fundamental_features(
        _prices_df(),
        _fund_df(rev_tag="RevenueFromContractWithCustomerExcludingAssessedTax"),
        settings=settings,
    )
    margin = out["fd_net_margin"].dropna().iloc[0]
    assert margin == pytest.approx(0.25)


def test_employees_coalesce(settings: FeatureSettings) -> None:
    out = add_fundamental_features(_prices_df(), _fund_df(), settings=settings)
    emp = out["fd_employees"].dropna().iloc[0]
    assert emp == pytest.approx(150_000.0)


# ─── anti-leakage ───────────────────────────────────────────
def test_anti_leakage_future_fundamental_poisoned(
    settings: FeatureSettings,
) -> None:
    df = _prices_df(n=500)
    fund = _fund_df(n=500)
    clean = add_fundamental_features(df, fund, settings=settings)

    cutoff_idx = 300
    cutoff_date = clean.iloc[cutoff_idx]["trade_date"]

    poisoned = fund.copy()
    mask = poisoned["trade_date"] > cutoff_date
    rng = np.random.default_rng(0)
    poisoned.loc[mask, "value"] = rng.uniform(1e7, 1e8, size=mask.sum())

    after = add_fundamental_features(df, poisoned, settings=settings)

    past = slice(0, cutoff_idx + 1)
    for col in fundamental_feature_columns(settings):
        np.testing.assert_array_equal(
            clean[col].iloc[past].to_numpy(),
            after[col].iloc[past].to_numpy(),
            err_msg=f"feature {col} changed when future facts were poisoned",
        )
