"""Risk framework for the backtest pipeline.

All parameters live in ``risk.config.RiskSettings``. Downstream modules
(staking, entry, limits, cost) read from there — never hardcode values.

Contract and rationale: docs/adr/0013-risk-framework.md
"""

from risk.config import RISK_FRAMEWORK_VERSION, RiskSettings

__all__ = ["RiskSettings", "RISK_FRAMEWORK_VERSION"]
