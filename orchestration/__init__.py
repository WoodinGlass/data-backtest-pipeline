"""Prefect orchestration layer for M7.

Design contract: docs/adr/0016-prefect-orchestration.md

The orchestration layer wraps every pipeline stage (M1-M6) as a
Prefect flow. It contains no pipeline logic of its own.
"""

from orchestration.config import ORCHESTRATION_VERSION, OrchestrationSettings

__all__ = ["ORCHESTRATION_VERSION", "OrchestrationSettings"]
