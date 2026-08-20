"""Bounded MiniZinc generation, correction, execution, and normalization."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planner_translation import (
    PlannerCorrectionFeedback,
    PlannerCorrectionStage,
    PlannerGenerationContext,
    PlanningTranslationOutcome,
    PlanningTranslationResult,
    create_generation_attempt_evidence,
    environment_data_sha256,
    validate_environment_data,
    verifiable_file_reference,
)
from onr.contracts.planning import (
    NormalizedPlan,
    PlannerExecutionResult,
    PlanningOutcome,
    PlanProvenance,
    ScheduledManeuver,
    TemporalManeuver,
    VerifiableReference,
)
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
    TranslationAttemptOutcome,
)
from onr.contracts.transport import TransportEvent
from onr.ports.planning import MiniZincPlannerExecutor

_REQUIRED_ASSETS = {"model.mzn", "data.dzn"}


@dataclass(frozen=True, slots=True)
class MiniZincProblem:
    """Generated MiniZinc assets plus the post-solver normalization template."""

    assets: Mapping[str, bytes]
    maneuvers: tuple[TemporalManeuver, ...]
    horizon: int
    translator_id: str
    translator_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.assets, Mapping):
            raise ValueError("MiniZinc assets must be a mapping")
        assets = dict(self.assets)
        if not all(
            isinstance(name, str) and isinstance(value, bytes)
            for name, value in assets.items()
        ):
            raise ValueError("MiniZinc assets must map filenames to bytes")
        if (
            isinstance(self.horizon, bool)
            or not isinstance(self.horizon, int)
            or self.horizon < 1
        ):
            raise ValueError("MiniZinc horizon must be a positive integer")
        if not isinstance(self.translator_id, str) or not self.translator_id.strip():
            raise ValueError("translator ID must be a non-empty string")
        if (
            not isinstance(self.translator_version, str)
            or not self.translator_version.strip()
        ):
            raise ValueError("translator version must be a non-empty string")
        object.__setattr__(self, "assets", MappingProxyType(assets))
        object.__setattr__(self, "maneuvers", tuple(self.maneuvers))


class MiniZincTranslation:
    """Run bounded correction and expose only independently verified optimal plans."""

    def __init__(
        self,
        planner: MiniZincPlannerExecutor,
        attempt_artifact_root: Path | str,
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
            raise TypeError("MiniZinc planner must expose check and execute")
        self._planner = planner
        self.attempt_artifact_root = Path(attempt_artifact_root).resolve()
        self.max_corrections = max_corrections

    def plan(
        self,
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
        generator: object,
        *,
        plan_revision: int,
        start_attempt_number: int = 1,
    ) -> PlanningTranslationResult:
        self._validate_context(
            mission_input, planner_choice, snapshot, environment_event
        )
        if (
            isinstance(start_attempt_number, bool)
            or not isinstance(start_attempt_number, int)
            or start_attempt_number < 1
        ):
            raise ValueError("starting generation attempt number must be positive")
        generate = self._generator(generator)
        feedback: PlannerCorrectionFeedback | None = None
        feedback_history: list[PlannerCorrectionFeedback] = []
        generation_attempts: list[PlannerGenerationAttempt] = []
        last_evidence = None

        for local_attempt_index in range(self.max_corrections + 1):
            attempt_number = start_attempt_number + local_attempt_index
            request = PlannerGenerationContext(
                mission_input=mission_input,
                planner_choice=planner_choice,
                mission_snapshot=snapshot,
                environment_event=environment_event,
                attempt_number=attempt_number,
                correction_feedback=feedback,
            )
            problem = generate(request)
            if not isinstance(problem, MiniZincProblem) or not self._static_check(
                problem
            ):
                generation_attempts.append(
                    self._generation_attempt(
                        request,
                        problem,
                        TranslationAttemptOutcome.REJECTED,
                    )
                )
                feedback = PlannerCorrectionFeedback(PlannerCorrectionStage.STATIC)
                feedback_history.append(feedback)
                continue

            execution = self._planner.execute(problem.assets)
            if not isinstance(execution, PlannerExecutionResult):
                generation_attempts.append(
                    self._generation_attempt(
                        request,
                        problem,
                        TranslationAttemptOutcome.REJECTED,
                    )
                )
                feedback = PlannerCorrectionFeedback(
                    PlannerCorrectionStage.SOLUTION_CHECKER
                )
                feedback_history.append(feedback)
                continue
            last_evidence = execution.evidence
            if execution.outcome is PlanningOutcome.UNSOLVABLE:
                generation_attempts.append(
                    self._generation_attempt(
                        request,
                        problem,
                        TranslationAttemptOutcome.REJECTED,
                    )
                )
                return PlanningTranslationResult(
                    PlanningTranslationOutcome.UNSOLVABLE,
                    len(generation_attempts),
                    tuple(generation_attempts),
                    correction_feedback=tuple(feedback_history),
                    evidence=last_evidence,
                )
            if execution.outcome is PlanningOutcome.TIMEOUT:
                generation_attempts.append(
                    self._generation_attempt(
                        request,
                        problem,
                        TranslationAttemptOutcome.REJECTED,
                    )
                )
                return PlanningTranslationResult(
                    PlanningTranslationOutcome.TIMEOUT,
                    len(generation_attempts),
                    tuple(generation_attempts),
                    correction_feedback=tuple(feedback_history),
                    evidence=last_evidence,
                )
            normalized = self._normalize(
                mission_input,
                planner_choice,
                snapshot,
                environment_event,
                problem,
                execution,
                plan_revision,
            )
            if normalized is not None:
                generation_attempts.append(
                    self._generation_attempt(
                        request,
                        problem,
                        TranslationAttemptOutcome.ACCEPTED,
                    )
                )
                return PlanningTranslationResult(
                    PlanningTranslationOutcome.VERIFIED,
                    len(generation_attempts),
                    tuple(generation_attempts),
                    normalized_plan=normalized,
                    correction_feedback=tuple(feedback_history),
                    evidence=last_evidence,
                )
            generation_attempts.append(
                self._generation_attempt(
                    request,
                    problem,
                    TranslationAttemptOutcome.REJECTED,
                )
            )
            feedback = PlannerCorrectionFeedback(
                PlannerCorrectionStage.SOLUTION_CHECKER
            )
            feedback_history.append(feedback)

        return PlanningTranslationResult(
            PlanningTranslationOutcome.REPAIR_EXHAUSTED,
            len(generation_attempts),
            tuple(generation_attempts),
            correction_feedback=tuple(feedback_history),
            evidence=last_evidence,
        )

    def _generation_attempt(
        self,
        context: PlannerGenerationContext,
        problem: object,
        outcome: TranslationAttemptOutcome,
    ) -> PlannerGenerationAttempt:
        if isinstance(problem, MiniZincProblem):
            translator_id = problem.translator_id
            translator_version = problem.translator_version
            assets = problem.assets
        else:
            translator_id = "invalid-generator-output"
            translator_version = "0"
            assets = {}
        return create_generation_attempt_evidence(
            context,
            translator_id=translator_id,
            translator_version=translator_version,
            assets=assets,
            outcome=outcome,
            artifact_root=self.attempt_artifact_root,
        )

    def _static_check(self, problem: MiniZincProblem) -> bool:
        if set(problem.assets) != _REQUIRED_ASSETS or any(
            not content for content in problem.assets.values()
        ):
            return False
        return self._planner.check(problem.assets) is True

    @staticmethod
    def _normalize(
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
        problem: MiniZincProblem,
        execution: PlannerExecutionResult,
        plan_revision: int,
    ) -> NormalizedPlan | None:
        if (
            execution.outcome is not PlanningOutcome.SOLVED
            or execution.evidence is None
        ):
            return None
        assignments = {item.maneuver_id: item for item in execution.assignments}
        declared = {item.maneuver_id: item for item in problem.maneuvers}
        if len(assignments) != len(execution.assignments) or set(assignments) != set(
            declared
        ):
            return None
        maneuvers = tuple(
            ScheduledManeuver(
                maneuver_id=maneuver_id,
                intent=maneuver.intent,
                dependencies=maneuver.dependencies,
                start=assignments[maneuver_id].start,
                duration=assignments[maneuver_id].duration,
            )
            for maneuver_id, maneuver in declared.items()
            if assignments[maneuver_id].duration == maneuver.duration
            and assignments[maneuver_id].start + assignments[maneuver_id].duration
            <= problem.horizon
        )
        if len(maneuvers) != len(declared):
            return None
        scheduled = {item.maneuver_id: item for item in maneuvers}
        if any(
            scheduled[item.maneuver_id].start
            < scheduled[dependency].start + scheduled[dependency].duration
            for item in problem.maneuvers
            for dependency in item.dependencies
        ):
            return None
        solver_reference = verifiable_file_reference(execution.evidence.stdout_path)
        if solver_reference is None:
            return None
        artifact_paths = {path.name: path for path in execution.evidence.artifact_paths}
        generated_assets: dict[str, VerifiableReference] = {}
        for name, content in problem.assets.items():
            path = artifact_paths.get(name)
            reference = verifiable_file_reference(path) if path is not None else None
            if (
                reference is None
                or reference.sha256 != hashlib.sha256(content).hexdigest()
            ):
                return None
            generated_assets[name] = reference
        provenance = PlanProvenance(
            mission_id=mission_input.mission_id,
            source_authority=mission_input.source_authority,
            mission_intent=VerifiableReference(
                f"mission-input:{mission_input.mission_id}",
                planner_choice.mission_input_sha256,
            ),
            planning_decision=VerifiableReference(
                planner_choice.decision_id,
                hashlib.sha256(
                    planner_choice.to_canonical_json().encode("utf-8")
                ).hexdigest(),
            ),
            environment_data=VerifiableReference(
                environment_event.event_id,
                environment_data_sha256(environment_event),
            ),
            generated_assets=generated_assets,
            solver_evidence={
                "planner-result": solver_reference,
            },
        )
        return NormalizedPlan(
            plan_revision=plan_revision,
            mission_snapshot_id=(
                f"{mission_input.mission_id}:snapshot:{snapshot.version}"
            ),
            planner_choice=planner_choice.planner_choice,
            outcome=PlanningOutcome.SOLVED,
            maneuvers=maneuvers,
            provenance=provenance,
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
        environment_event: TransportEvent,
    ) -> None:
        if planner_choice.mission_id != mission_input.mission_id:
            raise ValueError("Planner Choice does not match Mission Input")
        mission_input_sha256 = hashlib.sha256(
            mission_input.to_canonical_json().encode("utf-8")
        ).hexdigest()
        if planner_choice.mission_input_sha256 != mission_input_sha256:
            raise ValueError("Planner Choice does not bind the supplied Mission Input")
        if (
            str(planner_choice.planner_choice.planning_profile) != "temporal"
            or planner_choice.planner_choice.planner_id != "minizinc"
        ):
            raise ValueError(
                "MiniZinc translation requires the MiniZinc Planner Choice"
            )
        validate_environment_data(mission_input.mission_id, snapshot, environment_event)


__all__ = ["MiniZincProblem", "MiniZincTranslation"]
