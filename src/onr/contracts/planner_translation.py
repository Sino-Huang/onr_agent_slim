"""Public contracts for bounded planner-asset correction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import (
    NormalizedPlan,
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlannerStaticCheckResult,
)
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
    TranslationAttemptOutcome,
)
from onr.contracts.transport import TransportEvent


def validate_environment_data(
    mission_id: str,
    snapshot: MissionSnapshot,
    environment_event: TransportEvent,
) -> None:
    """Validate snapshot identity, revision, reference, health, and freshness."""

    source = "environment_data"
    source_reference = environment_event.payload.get(
        "source_environment_event_id", environment_event.event_id
    )
    if (
        snapshot.mission_id != mission_id
        or environment_event.mission_id != mission_id
        or environment_event.event_kind != "environment_data"
        or not isinstance(source_reference, str)
        or not source_reference.strip()
        or snapshot.source_references[source] != source_reference
        or snapshot.source_revisions[source] is None
        or snapshot.source_health[source] != "healthy"
        or not snapshot.source_freshness[source]
    ):
        raise ValueError("planning requires snapshot-authorized environment data")


class PlannerCorrectionStage(StrEnum):
    """Code-owned validation stage that rejected generated planner assets."""

    STATIC = "static"
    EXECUTION = "execution"
    SOLUTION_CHECKER = "solution_checker"


_CORRECTION_MESSAGES = {
    PlannerCorrectionStage.STATIC: "Generated planner assets failed static validation.",
    PlannerCorrectionStage.EXECUTION: (
        "Planner execution failed without diagnostic output."
    ),
    PlannerCorrectionStage.SOLUTION_CHECKER: (
        "Planner output failed independent solution validation."
    ),
}


@dataclass(frozen=True, slots=True)
class PlannerCorrectionFeedback:
    """Planner correction returned to a planner-asset generator."""

    stage: PlannerCorrectionStage | str
    static_check: PlannerStaticCheckResult | None = None
    execution_result: PlannerExecutionResult | None = None
    checker_diagnostic: str | None = None
    diagnostic_references: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        stage = PlannerCorrectionStage(self.stage)
        if self.static_check is not None and stage is not PlannerCorrectionStage.STATIC:
            raise ValueError("planner check diagnostics require static correction")
        if (
            self.execution_result is not None
            and stage is not PlannerCorrectionStage.EXECUTION
        ):
            raise ValueError("planner execution diagnostics require execution correction")
        if self.checker_diagnostic is not None and (
            stage is not PlannerCorrectionStage.SOLUTION_CHECKER
            or not isinstance(self.checker_diagnostic, str)
            or not self.checker_diagnostic.strip()
        ):
            raise ValueError(
                "solution-checker diagnostics require non-empty checker correction"
            )
        object.__setattr__(self, "stage", stage)
        object.__setattr__(
            self,
            "diagnostic_references",
            MappingProxyType(dict(self.diagnostic_references)),
        )

    @property
    def message(self) -> str:
        if self.static_check is not None:
            return self.static_check.error_message
        if self.execution_result is not None:
            return (
                self.execution_result.stderr.strip()
                or self.execution_result.stdout.strip()
                or _CORRECTION_MESSAGES[PlannerCorrectionStage.EXECUTION]
            )
        if self.checker_diagnostic is not None:
            return self.checker_diagnostic
        return _CORRECTION_MESSAGES[PlannerCorrectionStage(self.stage)]


@dataclass(frozen=True, slots=True)
class PlannerGenerationContext:
    """Authoritative inputs and optional safe feedback for one generation attempt."""

    mission_input: MissionInput
    planner_choice: PlannerChoiceRecord
    mission_snapshot: MissionSnapshot
    environment_event: TransportEvent
    attempt_number: int
    correction_feedback: PlannerCorrectionFeedback | None = None

    def __post_init__(self) -> None:
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise ValueError("generation attempt number must be positive")


def create_generation_attempt_evidence(
    context: PlannerGenerationContext,
    *,
    translator_id: str,
    translator_version: str,
    assets: Mapping[str, bytes],
    outcome: TranslationAttemptOutcome | str,
    artifact_root: Path,
) -> PlannerGenerationAttempt:
    """Persist one generated asset set as immutable public evidence."""

    attempt_id = (
        f"{context.planner_choice.decision_id}:generation:{context.attempt_number}"
    )
    directory = (
        Path(artifact_root).resolve()
        / quote(context.planner_choice.decision_id, safe="._-")
        / f"{context.attempt_number:03d}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    references: dict[str, str] = {}
    for name, content in sorted(assets.items()):
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(content, bytes)
        ):
            raise ValueError("generation-attempt assets must map names to bytes")
        path = directory / quote(name, safe="._-")
        path.write_bytes(content)
        references[name] = str(path.resolve())
    choice = context.planner_choice
    return PlannerGenerationAttempt(
        attempt_id=attempt_id,
        decision_id=choice.decision_id,
        mission_id=choice.mission_id,
        planner_choice=choice.planner_choice,
        rationale=choice.rationale,
        mission_snapshot_id=(
            f"{context.mission_input.mission_id}:snapshot:"
            f"{context.mission_snapshot.version}"
        ),
        translator_id=translator_id,
        translator_version=translator_version,
        outcome=outcome,
        asset_references=references,
    )


def persist_static_check_diagnostics(
    attempt: PlannerGenerationAttempt,
    result: PlannerStaticCheckResult,
    *,
    prefix: str,
) -> Mapping[str, str]:
    """Persist exact checker streams beside one immutable generation attempt."""

    if not attempt.asset_references:
        return MappingProxyType({})
    directory = Path(next(iter(attempt.asset_references.values()))).resolve().parent
    references = {}
    for stream, contents in (("stdout", result.stdout), ("stderr", result.stderr)):
        path = directory / f"{prefix}-check.{stream}"
        path.write_text(contents, encoding="utf-8")
        references[stream] = str(path.resolve())
    return MappingProxyType(references)


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
    generation_attempts: tuple[PlannerGenerationAttempt, ...]
    normalized_plan: NormalizedPlan | None = None
    correction_feedback: tuple[PlannerCorrectionFeedback, ...] = ()
    evidence: PlannerExecutionEvidence | None = None

    def __post_init__(self) -> None:
        outcome = PlanningTranslationOutcome(self.outcome)
        if isinstance(self.attempt_count, bool) or self.attempt_count < 1:
            raise ValueError("translation attempt count must be positive")
        attempts = tuple(self.generation_attempts)
        if len(attempts) != self.attempt_count or not all(
            isinstance(item, PlannerGenerationAttempt) for item in attempts
        ):
            raise ValueError(
                "translation requires one generation evidence record per attempt"
            )
        accepted = [
            item
            for item in attempts
            if item.outcome is TranslationAttemptOutcome.ACCEPTED
        ]
        if outcome is PlanningTranslationOutcome.VERIFIED:
            if self.normalized_plan is None:
                raise ValueError("verified translation requires a Normalized Plan")
            if accepted != [attempts[-1]]:
                raise ValueError(
                    "verified translation requires only its final attempt to be accepted"
                )
        else:
            if self.normalized_plan is not None:
                raise ValueError(
                    "only verified translation may contain a Normalized Plan"
                )
            if accepted:
                raise ValueError(
                    "non-verified translation cannot accept generation attempts"
                )
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "generation_attempts", attempts)
        object.__setattr__(self, "correction_feedback", tuple(self.correction_feedback))


__all__ = [
    "PlannerCorrectionFeedback",
    "PlannerCorrectionStage",
    "PlannerGenerationContext",
    "PlanningTranslationOutcome",
    "PlanningTranslationResult",
    "create_generation_attempt_evidence",
    "persist_static_check_diagnostics",
    "validate_environment_data",
]
