"""Public terminal outcomes for one workflow-level Hyper invocation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HyperWorkflowOutcome(StrEnum):
    """Current terminal point reached by the Hyper workflow invocation."""

    EXECUTION_READY = "execution_ready"
    PLANNER_REJECTED = "planner_rejected"
    STATECHART_REJECTED = "statechart_rejected"
    MISSION_REJECTED = "mission_rejected"


@dataclass(frozen=True, slots=True)
class MissionRejection:
    """Recorded operator-facing refusal of an out-of-scope Mission Intent."""

    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Mission rejection reason must be a non-empty string")


__all__ = ["HyperWorkflowOutcome", "MissionRejection"]
