"""Public terminal outcomes for one workflow-level Hyper invocation."""

from __future__ import annotations

from enum import StrEnum


class HyperWorkflowOutcome(StrEnum):
    """Current terminal point reached by the Hyper workflow invocation."""

    PLANNER_REJECTED = "planner_rejected"


__all__ = ["HyperWorkflowOutcome"]
