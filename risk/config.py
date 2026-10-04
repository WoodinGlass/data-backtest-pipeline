"""Risk framework settings — single source of truth for M4.5 / M5.

Every parameter used by the walk-forward backtest lives here. Nothing
is hardcoded downstream. Override at runtime via environment variables
prefixed with ``DBP_RISK_`` (e.g. ``DBP_RISK_KELLY_FRACTION=0.5``), or
by passing explicit kwargs to ``RiskSettings(...)``.

Contract and rationale: docs/adr/0013-risk-framework.md
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


RISK_FRAMEWORK_VERSION: str = "v1"


class RiskSettings(BaseSettings):
    """All risk parameters for the walk-forward backtest.

    Fields are grouped by the eight decisions in ADR 0013. Env override
    uses ``DBP_RISK_<UPPER_FIELD_NAME>``, case-insensitive.
    """

    model_config = SettingsConfigDict(
        env_prefix="DBP_RISK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Metadata ---
    version: str = Field(
        default=RISK_FRAMEWORK_VERSION,
        description="Risk framework version. Bump on breaking changes.",
    )

    # --- 1. Staking (ADR 0013 §1) ---
    staking_method: Literal[
        "kelly", "fixed_fractional", "equal_weight", "vol_target"
    ] = Field(
        default="kelly",
        description="Staking rule selector. See risk/staking.py registry.",
    )
    kelly_fraction: float = Field(
        default=0.25,
        gt=0,
        le=1,
        description="Fractional Kelly multiplier (0.25 = quarter-Kelly).",
    )
    kelly_cap: float = Field(
        default=0.05,
        gt=0,
        le=1,
        description="Per-name position cap as fraction of NAV.",
    )
    fixed_fractional: float = Field(
        default=0.03,
        gt=0,
        le=1,
        description="Position size for the fixed_fractional staking rule.",
    )

    # --- 2. Volatility targeting (ADR 0013 §2) ---
    target_vol_annual: float = Field(
        default=0.10,
        gt=0,
        le=1,
        description="Portfolio annualized vol target (0.10 = 10%).",
    )
    vol_lookback_days: int = Field(
        default=20,
        gt=1,
        le=252,
        description="Rolling window (trading days) for realized vol.",
    )
    annualization_factor: int = Field(
        default=252,
        gt=0,
        description="Trading days per year, for scaling daily vol.",
    )

    # --- 3. Stop loss and drawdown halt (ADR 0013 §3) ---
    stop_loss_pct: float = Field(
        default=-0.08,
        lt=0,
        gt=-1,
        description="Per-position return trigger for exit (e.g. -0.08 = -8%).",
    )
    stop_loss_cooldown_days: int = Field(
        default=5,
        ge=0,
        description="Days to block re-entry after a stop-out.",
    )
    dd_derisk_trigger: float = Field(
        default=-0.10,
        lt=0,
        gt=-1,
        description="Portfolio drawdown level that halves exposure.",
    )
    dd_derisk_factor: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="Position multiplier applied when derisk trigger fires.",
    )
    dd_halt_trigger: float = Field(
        default=-0.20,
        lt=0,
        gt=-1,
        description="Portfolio drawdown level that halts trading for the run.",
    )

    # --- 4. Direction (ADR 0013 §4) ---
    allow_short: bool = Field(
        default=False,
        description="v1 = long+flat only. Short deferred to M5.5.",
    )

    # --- 5. Idle cash (ADR 0013 §5) ---
    cash_rate_source: Literal["zero", "fedfunds"] = Field(
        default="fedfunds",
        description="Interest paid on idle cash. 'fedfunds' reads mc_fedfunds.",
    )
    cash_rate_fallback: float = Field(
        default=0.0,
        ge=0,
        le=1,
        description="Annualized rate used when the source is unavailable.",
    )

    # --- 6. Transaction costs (ADR 0013 §6) ---
    commission_bp: float = Field(
        default=0.5,
        ge=0,
        description="Commission, one way, in basis points.",
    )
    slippage_bp: float = Field(
        default=2.0,
        ge=0,
        description="Slippage, one way, in basis points.",
    )
    borrow_bp_annual: float = Field(
        default=50.0,
        ge=0,
        description="Annualized short borrow cost. Unused in v1.",
    )

    # --- 7. Rebalance (ADR 0013 §7) ---
    rebalance_threshold: float = Field(
        default=0.01,
        gt=0,
        le=1,
        description="Min |target - current| NAV drift to trigger a trade.",
    )

    # --- 8. Entry rule (ADR 0013 §8) ---
    entry_method: Literal["threshold", "top_n", "cross_sectional"] = Field(
        default="threshold",
        description="Entry rule selector. See risk/entry.py registry.",
    )
    entry_prob_threshold: float = Field(
        default=0.55,
        gt=0.5,
        lt=1,
        description="Min p to go long (threshold method).",
    )
    entry_top_n: int = Field(
        default=10,
        gt=0,
        description="Number of names to hold (top_n method).",
    )

    # --- Derived properties ---
    @property
    def one_way_cost_bp(self) -> float:
        """Commission + slippage, one way, in basis points."""
        return self.commission_bp + self.slippage_bp

    @property
    def round_trip_cost_bp(self) -> float:
        """Commission + slippage, round trip, in basis points."""
        return 2.0 * self.one_way_cost_bp

    # --- Cross-field validation ---
    #
    # The only hard invariant we enforce is that ``dd_derisk_trigger``
    # is shallower (closer to 0) than ``dd_halt_trigger``. If it were
    # deeper, the "derisk" state would be unreachable: the portfolio
    # would halt before it could ever be scaled down. That is a bug in
    # the configuration, not a preference.
    #
    # We deliberately do NOT require stop_loss_pct to be shallower than
    # dd_halt_trigger: a deeper stop is a valid way to disable the
    # per-position stop and let the DD halt be the only circuit breaker.
    #
    @model_validator(mode="after")
    def _check_dd_ordering(self) -> "RiskSettings":
        if self.dd_derisk_trigger <= self.dd_halt_trigger:
            raise ValueError(
                f"dd_derisk_trigger ({self.dd_derisk_trigger}) must be "
                f"shallower (closer to 0) than dd_halt_trigger "
                f"({self.dd_halt_trigger}); otherwise the derisk state "
                f"is unreachable."
            )
        return self

    # --- Helpers ---
    def describe(self) -> str:
        """Compact one-line summary for logs and MLflow tags."""
        return (
            f"RiskSettings(version={self.version}, "
            f"staking={self.staking_method}, "
            f"k={self.kelly_fraction}, cap={self.kelly_cap}, "
            f"target_vol={self.target_vol_annual}, "
            f"lookback={self.vol_lookback_days}, "
            f"stop={self.stop_loss_pct}, "
            f"derisk={self.dd_derisk_trigger}x{self.dd_derisk_factor}, "
            f"halt={self.dd_halt_trigger}, "
            f"entry={self.entry_method}@{self.entry_prob_threshold}, "
            f"cost_bp_rt={self.round_trip_cost_bp})"
        )
