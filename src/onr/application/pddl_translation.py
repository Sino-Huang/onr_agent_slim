"""Bounded PDDL generation, correction, planning, and VAL verification."""

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
    persist_static_check_diagnostics,
    validate_environment_data,
    verifiable_file_reference,
)
from onr.contracts.planning import (
    NormalizedPlan,
    PlannerStaticCheckResult,
    PlanningOutcome,
    PlanProvenance,
    SymbolicManeuver,
    SymbolicPlannerExecutionResult,
    SymbolicPlanStep,
    VerifiableReference,
)
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
    TranslationAttemptOutcome,
)
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
            raise TypeError("Fast Downward planner must expose check and execute")
        if not callable(getattr(validator, "validate", None)):
            raise TypeError("symbolic plan validator must expose validate")
        self._planner = planner
        self._validator = validator
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
    ) -> PlanningTranslationResult:
        self._validate_context(
            mission_input, planner_choice, snapshot, environment_event
        )
        generate = self._generator(generator)
        feedback: PlannerCorrectionFeedback | None = None
        feedback_history: list[PlannerCorrectionFeedback] = []
        generation_attempts: list[PlannerGenerationAttempt] = []
        last_evidence = None

        for attempt_number in range(1, self.max_corrections + 2):
            request = PlannerGenerationContext(
                mission_input=mission_input,
                planner_choice=planner_choice,
                mission_snapshot=snapshot,
                environment_event=environment_event,
                attempt_number=attempt_number,
                correction_feedback=feedback,
            )
            problem = generate(request)
            if not isinstance(problem, PDDLProblem):
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
            static_check = self._static_check(problem)
            if not static_check.accepted:
                attempt = self._generation_attempt(
                    request,
                    problem,
                    TranslationAttemptOutcome.REJECTED,
                )
                generation_attempts.append(attempt)
                diagnostic_references = persist_static_check_diagnostics(
                    attempt,
                    static_check,
                    prefix="fast-downward",
                )
                feedback = PlannerCorrectionFeedback(
                    PlannerCorrectionStage.STATIC,
                    static_check=static_check,
                    diagnostic_references=diagnostic_references,
                )
                feedback_history.append(feedback)
                continue

            execution = self._planner.execute(problem.assets)
            if not isinstance(execution, SymbolicPlannerExecutionResult):
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
                    attempt_number,
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
                    attempt_number,
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
                    attempt_number,
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
            self.max_corrections + 1,
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
        if isinstance(problem, PDDLProblem):
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

    def _static_check(self, problem: PDDLProblem) -> PlannerStaticCheckResult:
        if set(problem.assets) != _REQUIRED_ASSETS or any(
            not content for content in problem.assets.values()
        ):
            return PlannerStaticCheckResult(
                False,
                None,
                stderr=(
                    "Fast Downward static check requires non-empty "
                    "domain.pddl and problem.pddl."
                ),
            )
        result = self._planner.check(problem.assets)
        if not isinstance(result, PlannerStaticCheckResult):
            raise TypeError("Fast Downward planner check returned an invalid result")
        return result

    def _normalize(
        self,
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
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
        declared = {item.maneuver_id.lower(): item for item in problem.maneuvers}
        if len(declared) != len(problem.maneuvers) or len(
            execution.action_calls
        ) != len(declared):
            return None
        seen: set[str] = set()
        maneuvers: list[SymbolicPlanStep] = []
        for step_index, action_call in enumerate(execution.action_calls):
            action_name = action_call.action.lower()
            maneuver = declared.get(action_name)
            if (
                maneuver is None
                or action_name in seen
                or action_call.arguments
                or any(
                    dependency.lower() not in seen
                    for dependency in maneuver.dependencies
                )
            ):
                return None
            seen.add(action_name)
            maneuvers.append(
                SymbolicPlanStep(
                    step_index=step_index,
                    maneuver_id=maneuver.maneuver_id,
                    intent=maneuver.intent,
                    dependencies=maneuver.dependencies,
                    cost=maneuver.cost,
                )
            )
        if sum(item.cost for item in maneuvers) != execution.total_plan_cost:
            return None
        solver_reference = verifiable_file_reference(execution.evidence.stdout_path)
        artifact_paths = {path.name: path for path in execution.evidence.artifact_paths}
        accepted_plan_path = artifact_paths.get("sas_plan")
        accepted_plan_reference = (
            verifiable_file_reference(accepted_plan_path)
            if accepted_plan_path is not None
            else None
        )
        validator_reference = verifiable_file_reference(
            execution.evidence.artifact_directory / "validator.stdout"
        )
        if (
            solver_reference is None
            or accepted_plan_reference is None
            or validator_reference is None
        ):
            return None
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
                "accepted-plan": accepted_plan_reference,
                "planner-result": solver_reference,
                "validator-result": validator_reference,
            },
        )
        return NormalizedPlan(
            plan_revision=plan_revision,
            mission_snapshot_id=(
                f"{mission_input.mission_id}:snapshot:{snapshot.version}"
            ),
            planner_choice=planner_choice.planner_choice,
            outcome=PlanningOutcome.SOLVED,
            maneuvers=tuple(maneuvers),
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
            str(planner_choice.planner_choice.planning_profile) != "symbolic"
            or planner_choice.planner_choice.planner_id != "fast-downward"
        ):
            raise ValueError(
                "PDDL translation requires the Fast Downward Planner Choice"
            )
        validate_environment_data(mission_input.mission_id, snapshot, environment_event)


__all__ = ["PDDLProblem", "PDDLTranslation"]
