"""Monitoring layer for M10.

Design contract: docs/adr/0019-monitoring-dashboard.md

Three signals, three pure modules:
    freshness    MAX(trade_date) per mart vs threshold
    drift        PSI + KS between reference and current windows
    performance  rolling metrics from M5 backtest artifacts

IO lives at the edges: monitoring.report and monitoring.cli.
"""

from monitoring.config import MONITORING_VERSION, MonitoringSettings

__all__ = ["MONITORING_VERSION", "MonitoringSettings"]
