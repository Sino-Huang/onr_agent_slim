"""Public contracts for bounded planner-asset correction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import NormalizedPlan, PlannerExecutionEvidence
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.transport import TransportEvent


class PlannerCorrectionStage(StrEnum):
    """Code-owned validation stage that rejected generated planner assets."""

    STATIC = "static"
    SOLUTION_CHECKER = "solution_checker"


_CORRECTION_MESSAGES = {
    PlannerCorrectionStage.STATIC: "Generated planner assets failed static validation.",
    PlannerCorrectionStage.SOLUTION_CHECKER: (
        "Planner output failed independent solution validation."
    ),
}


@dataclass(frozen=True, slots=True)
class PlannerCorrectionFeedback:
    """Sanitized correction returned to a planner-asset generator."""

    stage: PlannerCorrectionStage | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", PlannerCorrectionStage(self.stage))

    @property
    def message(self) -> str:
        return _CORRECTION_MESSAGES[PlannerCorrectionStage(self.stage)]


@dataclass(frozen=True, slots=True)
class PlannerGenerationContext:
    """Authoritative inputs and optional safe feedback for one generation attempt."""

    mission_input: MissionInput
    planner_choice: PlannerChoiceRecord
    mission_snapshot: MissionSnapshot
    scene_graph: TransportEvent
    attempt_number: int
    correction_feedback: PlannerCorrectionFeedback | None = None

    def __post_init__(self) -> None:
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise ValueError("generation attempt number must be positive")


class PlanningTranslationOutcome(StrEnum):
    """Terminal classification for a bounded planner translation run."""

    VERIFIED = "verified"
    REPAIR_EXHAUSTED = "repair_exhausted"
    UNSOLVABLE = "unsolvable"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class PlanningTranslationResult:
    """Result of bounded generation, planner execution, and independent checking."""

    outcome: PlanningTranslationOutcome | str
    attempt_count: int
    normalized_plan: NormalizedPlan | None = None
    correction_feedback: tuple[PlannerCorrectionFeedback, ...] = ()
    evidence: PlannerExecutionEvidence | None = None

    def __post_init__(self) -> None:
        outcome = PlanningTranslationOutcome(self.outcome)
        if isinstance(self.attempt_count, bool) or self.attempt_count < 1:
            raise ValueError("translation attempt count must be positive")
        if outcome is PlanningTranslationOutcome.VERIFIED:
            if self.normalized_plan is None:
                raise ValueError("verified translation requires a Normalized Plan")
        elif self.normalized_plan is not None:
            raise ValueError("only verified translation may contain a Normalized Plan")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "correction_feedback", tuple(self.correction_feedback))


__all__ = [
    "PlannerCorrectionFeedback",
    "PlannerCorrectionStage",
    "PlannerGenerationContext",
    "PlanningTranslationOutcome",
    "PlanningTranslationResult",
]
