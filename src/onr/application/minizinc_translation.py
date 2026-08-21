"""Bounded MiniZinc generation, correction, execution, and normalization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planner_translation import (
    PlannerCorrectionFeedback,
    PlannerCorrectionStage,
    PlannerGenerationContext,
    PlanningTranslationOutcome,
    PlanningTranslationResult,
    create_generation_attempt_evidence,
    persist_static_check_diagnostics,
    validate_environment_data,
)
from onr.contracts.planning import (
    ManeuverIntent,
    NormalizedPlan,
    PlannerExecutionResult,
    PlannerStaticCheckResult,
    PlanningOutcome,
    ScheduledManeuver,
    TemporalManeuver,
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
            raise TypeError("MiniZinc assets must be a mapping")
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
            if not isinstance(problem, MiniZincProblem):
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
            static_check = self.check_problem(problem)
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
                    prefix="minizinc",
                )
                feedback = PlannerCorrectionFeedback(
                    PlannerCorrectionStage.STATIC,
                    static_check=static_check,
                    diagnostic_references=diagnostic_references,
                )
                feedback_history.append(feedback)
                continue

            result = self._execute_attempt(
                mission_input,
                planner_choice,
                snapshot,
                environment_event,
                problem,
                request,
                plan_revision=plan_revision,
                prior_generation_attempts=tuple(generation_attempts),
                correction_feedback=tuple(feedback_history),
            )
            if result.outcome is not PlanningTranslationOutcome.REPAIR_EXHAUSTED:
                return result
            generation_attempts = list(result.generation_attempts)
            feedback_history = list(result.correction_feedback)
            feedback = feedback_history[-1]
            last_evidence = result.evidence

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

    def check_problem(self, problem: MiniZincProblem) -> PlannerStaticCheckResult:
        """Statically check one exact MiniZinc problem without executing it."""

        if not isinstance(problem, MiniZincProblem):
            raise TypeError("MiniZinc static check requires a MiniZincProblem")
        if set(problem.assets) != _REQUIRED_ASSETS or any(
            not content for content in problem.assets.values()
        ):
            return PlannerStaticCheckResult(
                False,
                None,
                stderr="MiniZinc static check requires non-empty model.mzn and data.dzn.",
            )
        result = self._planner.check(problem.assets)
        if not isinstance(result, PlannerStaticCheckResult):
            raise TypeError("MiniZinc planner check returned an invalid result")
        return result

    def execute_prechecked(
        self,
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
        problem: MiniZincProblem,
        *,
        plan_revision: int,
        attempt_number: int,
        prior_generation_attempts: tuple[PlannerGenerationAttempt, ...] = (),
        correction_feedback: tuple[PlannerCorrectionFeedback, ...] = (),
    ) -> PlanningTranslationResult:
        """Execute one statically accepted problem exactly once."""

        self._validate_context(
            mission_input, planner_choice, snapshot, environment_event
        )
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            raise ValueError("generation attempt number must be positive")
        if len(prior_generation_attempts) != len(correction_feedback):
            raise ValueError("prior MiniZinc attempts require matching feedback")
        if any(
            item.outcome is not TranslationAttemptOutcome.REJECTED
            for item in prior_generation_attempts
        ):
            raise ValueError("prior MiniZinc attempts must be rejected")
        request = PlannerGenerationContext(
            mission_input=mission_input,
            planner_choice=planner_choice,
            mission_snapshot=snapshot,
            environment_event=environment_event,
            attempt_number=attempt_number,
            correction_feedback=(
                correction_feedback[-1] if correction_feedback else None
            ),
        )
        return self._execute_attempt(
            mission_input,
            planner_choice,
            snapshot,
            environment_event,
            problem,
            request,
            plan_revision=plan_revision,
            prior_generation_attempts=prior_generation_attempts,
            correction_feedback=correction_feedback,
        )

    def _execute_attempt(
        self,
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
        problem: MiniZincProblem,
        request: PlannerGenerationContext,
        *,
        plan_revision: int,
        prior_generation_attempts: tuple[PlannerGenerationAttempt, ...],
        correction_feedback: tuple[PlannerCorrectionFeedback, ...],
    ) -> PlanningTranslationResult:
        attempts = list(prior_generation_attempts)
        feedback = list(correction_feedback)
        execution = self._planner.execute(problem.assets)
        if not isinstance(execution, PlannerExecutionResult):
            attempts.append(
                self._generation_attempt(
                    request, problem, TranslationAttemptOutcome.REJECTED
                )
            )
            feedback.append(
                PlannerCorrectionFeedback(PlannerCorrectionStage.EXECUTION)
            )
            return PlanningTranslationResult(
                PlanningTranslationOutcome.REPAIR_EXHAUSTED,
                len(attempts),
                tuple(attempts),
                correction_feedback=tuple(feedback),
            )
        evidence = execution.evidence
        if execution.outcome in {PlanningOutcome.UNSOLVABLE, PlanningOutcome.TIMEOUT}:
            attempts.append(
                self._generation_attempt(
                    request, problem, TranslationAttemptOutcome.REJECTED
                )
            )
            outcome = (
                PlanningTranslationOutcome.UNSOLVABLE
                if execution.outcome is PlanningOutcome.UNSOLVABLE
                else PlanningTranslationOutcome.TIMEOUT
            )
            return PlanningTranslationResult(
                outcome,
                len(attempts),
                tuple(attempts),
                correction_feedback=tuple(feedback),
                evidence=evidence,
            )
        if execution.outcome in {PlanningOutcome.ERROR, PlanningOutcome.INCOMPLETE}:
            attempts.append(
                self._generation_attempt(
                    request, problem, TranslationAttemptOutcome.REJECTED
                )
            )
            feedback.append(
                PlannerCorrectionFeedback(
                    PlannerCorrectionStage.EXECUTION,
                    execution_result=execution,
                )
            )
            return PlanningTranslationResult(
                PlanningTranslationOutcome.REPAIR_EXHAUSTED,
                len(attempts),
                tuple(attempts),
                correction_feedback=tuple(feedback),
                evidence=evidence,
            )
        normalized, checker_diagnostic = self._normalize(
            mission_input,
            planner_choice,
            snapshot,
            environment_event,
            problem,
            execution,
            plan_revision,
        )
        if normalized is not None:
            attempts.append(
                self._generation_attempt(
                    request, problem, TranslationAttemptOutcome.ACCEPTED
                )
            )
            return PlanningTranslationResult(
                PlanningTranslationOutcome.VERIFIED,
                len(attempts),
                tuple(attempts),
                normalized_plan=normalized,
                correction_feedback=tuple(feedback),
                evidence=evidence,
            )
        attempts.append(
            self._generation_attempt(
                request, problem, TranslationAttemptOutcome.REJECTED
            )
        )
        feedback.append(
            PlannerCorrectionFeedback(
                PlannerCorrectionStage.SOLUTION_CHECKER,
                checker_diagnostic=checker_diagnostic,
            )
        )
        return PlanningTranslationResult(
            PlanningTranslationOutcome.REPAIR_EXHAUSTED,
            len(attempts),
            tuple(attempts),
            correction_feedback=tuple(feedback),
            evidence=evidence,
        )

    @staticmethod
    def _normalize(
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
        problem: MiniZincProblem,
        execution: PlannerExecutionResult,
        plan_revision: int,
    ) -> tuple[NormalizedPlan | None, str | None]:
        if (
            execution.outcome is not PlanningOutcome.SOLVED
            or execution.evidence is None
        ):
            return None, "Planner execution evidence is missing."
        assignments = {item.maneuver_id: item for item in execution.assignments}
        declared = {item.maneuver_id: item for item in problem.maneuvers}
        if len(assignments) != len(execution.assignments) or set(assignments) != set(
            declared
        ):
            return (
                None,
                "Planner assignments do not match generated maneuver identifiers.",
            )
        maneuvers_list = []
        for maneuver_id, maneuver in declared.items():
            assignment = assignments[maneuver_id]
            if assignment.duration != maneuver.duration:
                return (
                    None,
                    (
                        f"Planner assignment duration for '{maneuver_id}' does not "
                        "match the generated maneuver."
                    ),
                )
            if assignment.start + assignment.duration > problem.horizon:
                return (
                    None,
                    f"Planner assignment '{maneuver_id}' exceeds the planning horizon.",
                )
            template_names = {item.name for item in maneuver.intent.parameters}
            if template_names.intersection(item.name for item in assignment.parameters):
                return (
                    None,
                    (
                        f"Planner assignment parameters for '{maneuver_id}' duplicate "
                        "generated template parameters."
                    ),
                )
            try:
                intent = ManeuverIntent(
                    maneuver.intent.action,
                    maneuver.intent.parameters + assignment.parameters,
                )
                scheduled_maneuver = ScheduledManeuver(
                    maneuver_id=maneuver_id,
                    intent=intent,
                    dependencies=maneuver.dependencies,
                    start=assignment.start,
                    duration=assignment.duration,
                )
            except ValueError:
                return None, f"Planner assignment '{maneuver_id}' cannot be normalized."
            maneuvers_list.append(scheduled_maneuver)
        maneuvers = tuple(maneuvers_list)

        scheduled = {item.maneuver_id: item for item in maneuvers}
        for item in problem.maneuvers:
            for dependency in item.dependencies:
                if (
                    scheduled[item.maneuver_id].start
                    < scheduled[dependency].start + scheduled[dependency].duration
                ):
                    return (
                        None,
                        (
                            f"Planner assignment '{item.maneuver_id}' starts before "
                            f"dependency '{dependency}' completes."
                        ),
                    )
        if not execution.evidence.stdout_path.is_file():
            return None, "Planner solver stdout evidence is missing or unreadable."
        artifact_paths = {path.name: path for path in execution.evidence.artifact_paths}
        for name, content in problem.assets.items():
            path = artifact_paths.get(name)
            try:
                persisted = path.read_bytes() if path is not None else None
            except OSError:
                persisted = None
            if persisted != content:
                return (
                    None,
                    (
                        f"Planner execution evidence for '{name}' does not match the "
                        "submitted asset."
                    ),
                )
        return (
            NormalizedPlan(
                mission_id=mission_input.mission_id,
                source_authority=mission_input.source_authority,
                plan_revision=plan_revision,
                mission_snapshot_id=(
                    f"{mission_input.mission_id}:snapshot:{snapshot.version}"
                ),
                planner_choice=planner_choice.planner_choice,
                outcome=PlanningOutcome.SOLVED,
                maneuvers=maneuvers,
            ),
            None,
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
        if (
            str(planner_choice.planner_choice.planning_profile) != "temporal"
            or planner_choice.planner_choice.planner_id != "minizinc"
        ):
            raise ValueError(
                "MiniZinc translation requires the MiniZinc Planner Choice"
            )
        validate_environment_data(mission_input.mission_id, snapshot, environment_event)


__all__ = ["MiniZincProblem", "MiniZincTranslation"]
