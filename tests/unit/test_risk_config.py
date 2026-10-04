"""Unit tests for risk.config.RiskSettings. See ADR 0013."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from risk.config import RISK_FRAMEWORK_VERSION, RiskSettings


class TestDefaults:
    def test_version_matches_constant(self) -> None:
        assert RiskSettings().version == RISK_FRAMEWORK_VERSION == "v1"

    def test_staking_defaults(self) -> None:
        rs = RiskSettings()
        assert rs.staking_method == "kelly"
        assert rs.kelly_fraction == 0.25
        assert rs.kelly_cap == 0.05
        assert rs.fixed_fractional == 0.03

    def test_vol_targeting_defaults(self) -> None:
        rs = RiskSettings()
        assert rs.target_vol_annual == 0.10
        assert rs.vol_lookback_days == 20
        assert rs.annualization_factor == 252

    def test_stop_loss_defaults(self) -> None:
        rs = RiskSettings()
        assert rs.stop_loss_pct == -0.08
        assert rs.stop_loss_cooldown_days == 5
        assert rs.dd_derisk_trigger == -0.10
        assert rs.dd_derisk_factor == 0.5
        assert rs.dd_halt_trigger == -0.20

    def test_direction_defaults(self) -> None:
        assert RiskSettings().allow_short is False

    def test_cash_defaults(self) -> None:
        rs = RiskSettings()
        assert rs.cash_rate_source == "fedfunds"
        assert rs.cash_rate_fallback == 0.0

    def test_cost_defaults(self) -> None:
        rs = RiskSettings()
        assert rs.commission_bp == 0.5
        assert rs.slippage_bp == 2.0
        assert rs.borrow_bp_annual == 50.0

    def test_rebalance_defaults(self) -> None:
        assert RiskSettings().rebalance_threshold == 0.01

    def test_entry_defaults(self) -> None:
        rs = RiskSettings()
        assert rs.entry_method == "threshold"
        assert rs.entry_prob_threshold == 0.55
        assert rs.entry_top_n == 10


class TestDerivedProperties:
    def test_one_way_cost_bp_default(self) -> None:
        assert RiskSettings().one_way_cost_bp == 2.5

    def test_round_trip_cost_bp_default(self) -> None:
        assert RiskSettings().round_trip_cost_bp == 5.0

    def test_cost_properties_respect_overrides(self) -> None:
        rs = RiskSettings(commission_bp=1.0, slippage_bp=3.0)
        assert rs.one_way_cost_bp == 4.0
        assert rs.round_trip_cost_bp == 8.0


class TestCrossFieldValidation:
    def test_rejects_derisk_deeper_than_halt(self) -> None:
        with pytest.raises(ValidationError) as exc:
            RiskSettings(dd_derisk_trigger=-0.25, dd_halt_trigger=-0.20)
        assert "shallower" in str(exc.value)

    def test_rejects_derisk_equal_to_halt(self) -> None:
        with pytest.raises(ValidationError) as exc:
            RiskSettings(dd_derisk_trigger=-0.20, dd_halt_trigger=-0.20)
        assert "shallower" in str(exc.value)

    def test_accepts_deep_stop_loss(self) -> None:
        # Deep stop is a valid way to disable the per-position stop.
        rs = RiskSettings(stop_loss_pct=-0.99)
        assert rs.stop_loss_pct == -0.99

    def test_accepts_high_kelly_cap(self) -> None:
        assert RiskSettings(kelly_cap=1.0).kelly_cap == 1.0

    def test_rejects_kelly_fraction_above_one(self) -> None:
        with pytest.raises(ValidationError):
            RiskSettings(kelly_fraction=1.5)

    def test_rejects_negative_kelly_cap(self) -> None:
        with pytest.raises(ValidationError):
            RiskSettings(kelly_cap=-0.05)

    def test_rejects_positive_stop_loss(self) -> None:
        with pytest.raises(ValidationError):
            RiskSettings(stop_loss_pct=0.05)

    def test_rejects_zero_vol_target(self) -> None:
        with pytest.raises(ValidationError):
            RiskSettings(target_vol_annual=0.0)


class TestEnvOverride:
    def test_env_override_kelly_fraction(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_RISK_KELLY_FRACTION", "0.5")
        assert RiskSettings().kelly_fraction == 0.5

    def test_env_override_entry_threshold(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_RISK_ENTRY_PROB_THRESHOLD", "0.60")
        assert RiskSettings().entry_prob_threshold == 0.60

    def test_kwarg_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_RISK_KELLY_FRACTION", "0.5")
        assert RiskSettings(kelly_fraction=0.75).kelly_fraction == 0.75


class TestDescribe:
    def test_describe_contains_key_fields(self) -> None:
        s = RiskSettings().describe()
        assert "k=0.25" in s
        assert "cap=0.05" in s
        assert "target_vol=0.1" in s
        assert "cost_bp_rt=5.0" in s
        assert "version=v1" in s
