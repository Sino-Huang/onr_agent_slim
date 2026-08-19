"""Bounded PDDL generation, correction, planning, and VAL verification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable

from onr.application.symbolic_planning import normalize_symbolic_actions
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planner_translation import (
    PlannerCorrectionFeedback,
    PlannerCorrectionStage,
    PlannerGenerationContext,
    PlanningTranslationOutcome,
    PlanningTranslationResult,
)
from onr.contracts.planning import (
    NormalizedPlan,
    PlanningOutcome,
    SymbolicManeuver,
    SymbolicMissionSpec,
    SymbolicPlannerExecutionResult,
)
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.transport import TransportEvent
from onr.ports.planning import FastDownwardPlannerExecutor, SymbolicPlanValidator

_REQUIRED_ASSETS = {"domain.pddl", "problem.pddl"}


@dataclass(frozen=True, slots=True)
class PDDLProblem:
    """Generated PDDL assets plus the post-solver normalization template."""

    assets: Mapping[str, bytes]
    maneuvers: tuple[SymbolicManeuver, ...]
    domain_revision: int
    translator_id: str
    translator_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.assets, Mapping):
            raise ValueError("PDDL assets must be a mapping")
        assets = dict(self.assets)
        if not all(
            isinstance(name, str) and isinstance(value, bytes)
            for name, value in assets.items()
        ):
            raise ValueError("PDDL assets must map filenames to bytes")
        if (
            isinstance(self.domain_revision, bool)
            or not isinstance(self.domain_revision, int)
            or self.domain_revision < 1
        ):
            raise ValueError("PDDL domain revision must be a positive integer")
        if not isinstance(self.translator_id, str) or not self.translator_id.strip():
            raise ValueError("translator ID must be a non-empty string")
        if (
            not isinstance(self.translator_version, str)
            or not self.translator_version.strip()
        ):
            raise ValueError("translator version must be a non-empty string")
        object.__setattr__(self, "assets", MappingProxyType(assets))
        object.__setattr__(self, "maneuvers", tuple(self.maneuvers))


class PDDLTranslation:
    """Run bounded PDDL correction and expose only VAL-verified plans."""

    def __init__(
        self,
        planner: FastDownwardPlannerExecutor,
        validator: SymbolicPlanValidator,
        *,
        max_corrections: int = 2,
    ) -> None:
        if (
            isinstance(max_corrections, bool)
            or not isinstance(max_corrections, int)
            or max_corrections < 0
        ):
            raise ValueError("maximum corrections must be a non-negative integer")
        if not callable(getattr(planner, "check", None)) or not callable(
            getattr(planner, "execute", None)
        ):
            raise TypeError("Fast Downward planner must expose check and execute")
        if not callable(getattr(validator, "validate", None)):
            raise TypeError("symbolic plan validator must expose validate")
        self._planner = planner
        self._validator = validator
        self.max_corrections = max_corrections

    def plan(
        self,
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        scene_graph: TransportEvent,
        generator: object,
        *,
        plan_revision: int,
    ) -> PlanningTranslationResult:
        self._validate_context(mission_input, planner_choice, snapshot, scene_graph)
        generate = self._generator(generator)
        feedback: PlannerCorrectionFeedback | None = None
        feedback_history: list[PlannerCorrectionFeedback] = []
        last_evidence = None

        for attempt_number in range(1, self.max_corrections + 2):
            request = PlannerGenerationContext(
                mission_input=mission_input,
                planner_choice=planner_choice,
                mission_snapshot=snapshot,
                scene_graph=scene_graph,
                attempt_number=attempt_number,
                correction_feedback=feedback,
            )
            problem = generate(request)
            if not isinstance(problem, PDDLProblem) or not self._static_check(problem):
                feedback = PlannerCorrectionFeedback(PlannerCorrectionStage.STATIC)
                feedback_history.append(feedback)
                continue

            execution = self._planner.execute(problem.assets)
            if not isinstance(execution, SymbolicPlannerExecutionResult):
                feedback = PlannerCorrectionFeedback(
                    PlannerCorrectionStage.SOLUTION_CHECKER
                )
                feedback_history.append(feedback)
                continue
            last_evidence = execution.evidence
            if execution.outcome is PlanningOutcome.UNSOLVABLE:
                return PlanningTranslationResult(
                    PlanningTranslationOutcome.UNSOLVABLE,
                    attempt_number,
                    correction_feedback=tuple(feedback_history),
                    evidence=last_evidence,
                )
            if execution.outcome is PlanningOutcome.TIMEOUT:
                return PlanningTranslationResult(
                    PlanningTranslationOutcome.TIMEOUT,
                    attempt_number,
                    correction_feedback=tuple(feedback_history),
                    evidence=last_evidence,
                )
            normalized = self._normalize(
                mission_input,
                planner_choice,
                snapshot,
                problem,
                execution,
                plan_revision,
            )
            if normalized is not None:
                return PlanningTranslationResult(
                    PlanningTranslationOutcome.VERIFIED,
                    attempt_number,
                    normalized_plan=normalized,
                    correction_feedback=tuple(feedback_history),
                    evidence=last_evidence,
                )
            feedback = PlannerCorrectionFeedback(
                PlannerCorrectionStage.SOLUTION_CHECKER
            )
            feedback_history.append(feedback)

        return PlanningTranslationResult(
            PlanningTranslationOutcome.REPAIR_EXHAUSTED,
            self.max_corrections + 1,
            correction_feedback=tuple(feedback_history),
            evidence=last_evidence,
        )

    def _static_check(self, problem: PDDLProblem) -> bool:
        if set(problem.assets) != _REQUIRED_ASSETS or any(
            not content for content in problem.assets.values()
        ):
            return False
        return self._planner.check(problem.assets) is True

    def _normalize(
        self,
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        problem: PDDLProblem,
        execution: SymbolicPlannerExecutionResult,
        plan_revision: int,
    ) -> NormalizedPlan | None:
        if (
            execution.outcome is not PlanningOutcome.SOLVED
            or execution.evidence is None
            or self._validator.validate(execution.evidence) is not True
        ):
            return None
        try:
            mission_spec = SymbolicMissionSpec(
                mission_id=mission_input.mission_id,
                objective=mission_input.mission_text,
                planner_choice=planner_choice.planner_choice,
                maneuvers=problem.maneuvers,
                source_authority=mission_input.source_authority,
                domain_revision=problem.domain_revision,
            )
        except (TypeError, ValueError):
            return None
        maneuvers = normalize_symbolic_actions(
            mission_spec,
            execution.action_calls,
            execution.total_plan_cost,
        )
        if maneuvers is None:
            return None
        return NormalizedPlan(
            mission_spec=mission_spec,
            plan_revision=plan_revision,
            mission_snapshot_id=(
                f"{mission_input.mission_id}:snapshot:{snapshot.version}"
            ),
            planner_choice=planner_choice.planner_choice,
            outcome=PlanningOutcome.SOLVED,
            maneuvers=maneuvers,
        )

    @staticmethod
    def _generator(
        generator: object,
    ) -> Callable[[PlannerGenerationContext], object]:
        method = getattr(generator, "generate", None)
        if callable(method):
            return method
        if callable(generator):
            return generator
        raise TypeError("planner asset generator must be callable or expose generate")

    @staticmethod
    def _validate_context(
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        scene_graph: TransportEvent,
    ) -> None:
        if planner_choice.mission_id != mission_input.mission_id:
            raise ValueError("Planner Choice does not match Mission Input")
        if (
            str(planner_choice.planner_choice.planning_profile) != "symbolic"
            or planner_choice.planner_choice.planner_id != "fast-downward"
        ):
            raise ValueError(
                "PDDL translation requires the Fast Downward Planner Choice"
            )
        source = "operational_scene_graph"
        if (
            snapshot.mission_id != mission_input.mission_id
            or scene_graph.mission_id != mission_input.mission_id
            or scene_graph.event_kind != "operational_scene_graph"
            or snapshot.source_references[source] != scene_graph.event_id
            or snapshot.source_health[source] != "healthy"
            or not snapshot.source_freshness[source]
        ):
            raise ValueError(
                "PDDL translation requires snapshot-authorized scene evidence"
            )


__all__ = ["PDDLProblem", "PDDLTranslation"]
