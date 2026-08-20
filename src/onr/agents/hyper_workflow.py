"""One Deep Agent workflow for environment-backed planning and correction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from onr.agents.hyper_agent import (
    _create_deep_agent,
    _parse_planning_intent_response,
)
from onr.application.minizinc_translation import MiniZincProblem, MiniZincTranslation
from onr.contracts.bayesian_belief import BayesianBeliefSnapshot
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, Statechart, TransitionCandidate
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planner_translation import (
    PlanningTranslationOutcome,
    PlanningTranslationResult,
    validate_environment_data,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    NormalizedPlan,
    PlannerChoice,
    TemporalManeuver,
)
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.planning_intent import PlanningIntent
from onr.contracts.transport import TransportEvent

HYPER_WORKFLOW_RESULT_SCHEMA: dict[str, Any] = {
    "title": "HyperWorkflowResultCandidate",
    "type": "object",
    "properties": {
        "mission_id": {"type": "string"},
        "outcome": {
            "enum": [
                "execution_ready",
                "planner_rejected",
                "statechart_rejected",
            ]
        },
    },
    "required": ["mission_id", "outcome"],
    "additionalProperties": False,
}


class TemporalManeuverCandidate(BaseModel):
    """Strict model-facing normalization template for one temporal maneuver."""

    model_config = ConfigDict(extra="forbid")

    maneuver_id: str = Field(
        description="Portable maneuver identifier used in solver output."
    )
    action: str = Field(description="Abstract maneuver action for Maneuver Control.")
    parameters: dict[str, str | int | float | bool | None] = Field(
        description="JSON-scalar action parameters keyed by semantic name."
    )
    dependencies: list[str] = Field(
        description="Maneuver IDs that must finish before this maneuver starts."
    )
    duration: int = Field(description="Positive integer maneuver duration.")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _prerequisite_missing(
    *, missing: tuple[str, ...], required_tool: str, retry_tool: str
) -> str:
    return _canonical_json(
        {
            "message": f"Call {required_tool}, then retry {retry_tool}.",
            "missing": list(missing),
            "required_tool": required_tool,
            "retry_tool": retry_tool,
            "status": "prerequisite_missing",
        }
    )


def _planner_asset_locations(
    context: HyperWorkflowContext, attempt_number: int
) -> dict[str, str]:
    workspace = cast(str, context.planner_workspace_location).rstrip("/")
    attempt = f"{attempt_number:03d}"
    return {
        "model_file_location": f"{workspace}/{attempt}/model.mzn",
        "data_file_location": f"{workspace}/{attempt}/data.dzn",
    }


def _planner_asset_path(
    context: HyperWorkflowContext,
    location: str,
    *,
    attempt_number: int,
    name: str,
) -> Path:
    if not isinstance(location, str) or not location.strip():
        raise ValueError("planner asset file location must be non-empty")
    expected_location = _planner_asset_locations(context, attempt_number)[name]
    if location != expected_location:
        raise ValueError("planner asset file location does not match the attempt workspace")
    if context.backend_root is not None and location.startswith("/"):
        path = context.backend_root / location.removeprefix("/")
    else:
        path = Path(location)
    path = path.resolve()
    expected_path = (
        context.artifact_root
        / "workspace"
        / f"{attempt_number:03d}"
        / ("model.mzn" if name == "model_file_location" else "data.dzn")
    ).resolve()
    if path != expected_path or not path.is_file():
        raise ValueError("planner asset file does not exist at the expected location")
    return path


@dataclass(slots=True)
class HyperWorkflowContext:
    """Per-run dependencies and evidence available only through workflow tools."""

    # ToolRuntime is itself validated and dumped by Pydantic before tool dispatch.
    # Keep injected domain/service objects opaque there; __post_init__ owns their
    # concrete interface validation and the model never sees these fields.
    mission_input: Any
    mission_snapshot: Any
    environment_event: Any
    artifact_root: Path
    minizinc_translation: Any
    belief_snapshot: Any = None
    max_planner_attempts: int = 1
    max_statechart_attempts: int = 3
    state_machine_factory: Any = None
    operational_log: Any = None
    backend_root: Path | None = None
    planner_workspace_location: str | None = None
    planning_intent: Any = field(default=None, init=False)
    planner_choice: Any = field(default=None, init=False)
    planning_context_loaded: bool = field(default=False, init=False)
    minizinc_problem: Any = field(default=None, init=False)
    draft_references: tuple[str, ...] = field(default=(), init=False)
    translation: Any = field(default=None, init=False)
    current_attempt_number: int = field(default=0, init=False)
    statechart: Any = field(default=None, init=False)
    statechart_reference: str | None = field(default=None, init=False)
    initial_fsm_status: Any = field(default=None, init=False)
    current_statechart_attempt: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.mission_input, MissionInput):
            raise TypeError("Hyper workflow requires a MissionInput")
        if not isinstance(self.mission_snapshot, MissionSnapshot):
            raise TypeError("Hyper workflow requires a MissionSnapshot")
        if not isinstance(self.environment_event, TransportEvent):
            raise TypeError("Hyper workflow requires an environment event")
        if self.belief_snapshot is not None and not isinstance(
            self.belief_snapshot, BayesianBeliefSnapshot
        ):
            raise TypeError(
                "Hyper workflow belief evidence must be a BayesianBeliefSnapshot"
            )
        if not isinstance(self.minizinc_translation, MiniZincTranslation):
            raise TypeError("Hyper workflow requires MiniZinc translation")
        if self.operational_log is not None and not callable(
            getattr(self.operational_log, "emit", None)
        ):
            raise TypeError("Hyper workflow operational log must expose emit")
        if (
            isinstance(self.max_planner_attempts, bool)
            or not isinstance(self.max_planner_attempts, int)
            or self.max_planner_attempts < 1
        ):
            raise ValueError("Hyper workflow planner attempt budget must be positive")
        if (
            isinstance(self.max_statechart_attempts, bool)
            or not isinstance(self.max_statechart_attempts, int)
            or self.max_statechart_attempts < 1
        ):
            raise ValueError("Hyper workflow Statechart attempt budget must be positive")
        if self.state_machine_factory is not None and not callable(
            getattr(self.state_machine_factory, "build", None)
        ):
            raise TypeError("Hyper workflow Statechart factory must expose build")
        self.artifact_root = Path(self.artifact_root).resolve()
        if self.backend_root is not None:
            self.backend_root = Path(self.backend_root).resolve()
        if self.planner_workspace_location is None:
            self.planner_workspace_location = str(
                (self.artifact_root / "workspace").resolve()
            )


@dataclass(frozen=True, slots=True)
class HyperWorkflowRunResult:
    """Validated evidence returned by one workflow-level Deep Agent run."""

    outcome: HyperWorkflowOutcome
    todos: tuple[Mapping[str, str], ...]
    messages: tuple[BaseMessage, ...]
    planning_intent: PlanningIntent | None
    planner_choice: PlannerChoiceRecord | None
    translation: PlanningTranslationResult | None
    normalized_plan: NormalizedPlan | None
    statechart: Statechart | None
    statechart_reference: str | None
    initial_fsm_status: FSMStatus | None


def _context(runtime: ToolRuntime[HyperWorkflowContext]) -> HyperWorkflowContext:
    context = runtime.context
    if not isinstance(context, HyperWorkflowContext):
        raise TypeError("Hyper workflow tool requires HyperWorkflowContext")
    return context


def _emit(
    context: HyperWorkflowContext,
    event_kind: str,
    outcome: str,
    *,
    details: Mapping[str, object] | None = None,
) -> None:
    if context.operational_log is not None:
        context.operational_log.emit(
            context.mission_input.mission_id,
            "hyper-agent",
            event_kind,
            outcome,
            details=details,
        )


@tool(parse_docstring=True)
def record_planning_intent(
    mission_id: str,
    source_authority: str,
    objective: str,
    planning_profile: Literal["temporal", "symbolic"],
    planner_id: Literal["minizinc", "fast-downward"],
    rationale: str,
    details: dict[str, Any],
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Validate and record the workflow's derived PlanningIntent and Planner Choice.

    Use this after reading the mission-parsing skill. Planner choice is recorded in
    the same call so the workflow can continue instead of ending at structured output.

    Args:
        mission_id: Exact MissionInput mission ID.
        source_authority: Exact MissionInput source authority.
        objective: Concise planner-facing Mission objective.
        planning_profile: Temporal scheduling or symbolic reachability profile.
        planner_id: Configured planner ID for the selected profile.
        rationale: Concise public planner-choice rationale.
        details: JSON-safe planner-selection facts derived from Mission Intent.

    Returns:
        Canonical PlannerChoiceRecord JSON bound to the accepted PlanningIntent.
    """

    context = _context(runtime)
    candidate = {
        "mission_id": mission_id,
        "source_authority": source_authority,
        "objective": objective,
        "planner_choice": {
            "planning_profile": planning_profile,
            "planner_id": planner_id,
        },
        "rationale": rationale,
        "details": details,
    }
    intent = _parse_planning_intent_response(
        {"structured_response": candidate}, context.mission_input
    )
    choice = PlannerChoiceRecord.from_planning_intent(intent)
    if context.planning_intent is not None or context.planner_choice is not None:
        if context.planning_intent != intent or context.planner_choice != choice:
            raise ValueError("recorded PlanningIntent conflicts with this workflow")
        return _canonical_json(
            {
                "next_tool": "load_planning_context",
                "planner_choice_record": choice.to_dict(),
                "status": "already_recorded",
            }
        )
    context.planning_intent = intent
    context.planner_choice = choice
    context.planning_context_loaded = False
    _emit(
        context,
        "planning-intent",
        "completed",
        details={
            "decision_id": choice.decision_id,
            "planner_id": choice.planner_choice.planner_id,
            "planning_profile": choice.planner_choice.planning_profile,
            "planning_intent_sha256": choice.planning_intent_sha256,
        },
    )
    _emit(
        context,
        "planner-choice",
        "completed",
        details={
            "decision_id": choice.decision_id,
            "planner_id": choice.planner_choice.planner_id,
            "planning_profile": choice.planner_choice.planning_profile,
            "rationale": choice.rationale,
        },
    )
    return _canonical_json(
        {
            "next_tool": "load_planning_context",
            "planner_choice_record": choice.to_dict(),
            "status": "accepted",
        }
    )


@tool
def load_planning_context(
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Load the complete snapshot-authorized context for planner generation.

    Returns canonical JSON containing PlanningIntent, Planner Choice, MissionSnapshot,
    the complete flexible environment-data payload, and the snapshot-authorized
    BayesianBeliefSnapshot when available, or an actionable missing-prerequisite
    result.
    """

    context = _context(runtime)
    intent = context.planning_intent
    choice = context.planner_choice
    if intent is None or choice is None:
        return _prerequisite_missing(
            missing=("planning_intent", "planner_choice"),
            required_tool="record_planning_intent",
            retry_tool="load_planning_context",
        )
    validate_environment_data(
        context.mission_input.mission_id,
        context.mission_snapshot,
        context.environment_event,
    )
    context.planning_context_loaded = True
    _emit(
        context,
        "planning-context",
        "completed",
        details={
            "snapshot_id": (
                f"{context.mission_input.mission_id}:snapshot:"
                f"{context.mission_snapshot.version}"
            ),
            "environment_data_reference": context.environment_event.event_id,
            "revision": context.mission_snapshot.version,
        },
    )
    return _canonical_json(
        {
            "status": "ready",
            "planning_intent": intent.to_dict(),
            "planner_choice": choice.to_dict(),
            "mission_snapshot": context.mission_snapshot.to_dict(),
            "environment_data": context.environment_event.to_dict()["payload"],
            "belief_snapshot": (
                context.belief_snapshot.to_dict()
                if context.belief_snapshot is not None
                else None
            ),
            "planner_asset_locations": _planner_asset_locations(
                context, context.current_attempt_number + 1
            ),
        }
    )


@tool(parse_docstring=True)
def persist_planner_assets(
    attempt_number: int,
    model_file_location: str,
    data_file_location: str,
    horizon: int,
    maneuvers: list[TemporalManeuverCandidate],
    translator_id: str,
    translator_version: str,
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Freeze agent-written MiniZinc files and their normalization template.

    Use this after reading creating-minizinc-problem-files and before calling
    planner_executor. Each call creates one attempt-specific immutable draft.

    Args:
        attempt_number: Positive generation-attempt sequence number.
        model_file_location: Exact model.mzn location returned by planning context.
        data_file_location: Exact data.dzn location returned by planning context.
        horizon: Positive scheduling horizon represented by the problem.
        maneuvers: Maneuver templates used to check solver assignments.
        translator_id: Public identity of the asset generator.
        translator_version: Public version of the asset generator.

    Returns:
        Canonical JSON containing the two persisted asset references, or an
        actionable missing-prerequisite result.
    """

    context = _context(runtime)
    choice = context.planner_choice
    if context.planning_intent is None or choice is None:
        return _prerequisite_missing(
            missing=("planning_intent", "planner_choice"),
            required_tool="record_planning_intent",
            retry_tool="persist_planner_assets",
        )
    if choice.planner_choice != PlannerChoice("temporal", "minizinc"):
        raise ValueError("MiniZinc assets require the recorded MiniZinc Planner Choice")
    if not context.planning_context_loaded:
        return _prerequisite_missing(
            missing=("planning_context",),
            required_tool="load_planning_context",
            retry_tool="persist_planner_assets",
        )
    if isinstance(attempt_number, bool) or attempt_number < 1:
        raise ValueError("planner asset attempt number must be positive")
    if (
        attempt_number > context.max_planner_attempts
        or attempt_number > context.current_attempt_number + 1
        or attempt_number < context.current_attempt_number
    ):
        raise ValueError("planner asset attempt is outside the workflow retry sequence")

    model_path = _planner_asset_path(
        context,
        model_file_location,
        attempt_number=attempt_number,
        name="model_file_location",
    )
    data_path = _planner_asset_path(
        context,
        data_file_location,
        attempt_number=attempt_number,
        name="data_file_location",
    )
    templates = tuple(_temporal_maneuver(item) for item in maneuvers)
    problem = MiniZincProblem(
        assets={
            "model.mzn": model_path.read_bytes(),
            "data.dzn": data_path.read_bytes(),
        },
        maneuvers=templates,
        horizon=horizon,
        translator_id=translator_id,
        translator_version=translator_version,
    )
    if (
        attempt_number == context.current_attempt_number
        and context.minizinc_problem != problem
    ):
        raise ValueError("planner draft attempt identity conflicts")
    directory = context.artifact_root / "drafts" / f"{attempt_number:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    references = []
    for name, contents in problem.assets.items():
        path = directory / name
        if path.exists() and path.read_bytes() != contents:
            raise ValueError("planner draft attempt identity conflicts")
        if not path.exists():
            path.write_bytes(contents)
        references.append(str(path.resolve()))
    context.minizinc_problem = problem
    context.draft_references = tuple(sorted(references))
    context.current_attempt_number = attempt_number
    _emit(
        context,
        "planner-assets",
        "completed",
        details={
            "generated_assets": "model.mzn,data.dzn",
            "planner_id": "minizinc",
            "sequence": attempt_number,
            "translator_id": translator_id,
            "translator_version": translator_version,
        },
    )
    return _canonical_json(
        {
            "attempt_number": attempt_number,
            "asset_references": list(context.draft_references),
            "asset_sha256": {
                name: hashlib.sha256(contents).hexdigest()
                for name, contents in sorted(problem.assets.items())
            },
            "planner_id": "minizinc",
        }
    )


def _temporal_maneuver(value: TemporalManeuverCandidate) -> TemporalManeuver:
    return TemporalManeuver(
        maneuver_id=value.maneuver_id,
        intent=ManeuverIntent(
            value.action,
            tuple(
                ManeuverParameter(name, item) for name, item in value.parameters.items()
            ),
        ),
        dependencies=tuple(value.dependencies),
        duration=value.duration,
    )


@tool(parse_docstring=True)
def planner_executor(
    planner_id: Literal["minizinc"],
    asset_references: list[str],
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Run the selected planner translator on one exact persisted asset draft.

    This tool owns static validation, external solver execution, independent
    checking, evidence persistence, and NormalizedPlan construction. A rejection
    returns only the code-owned sanitized correction stage and message.

    Args:
        planner_id: Recorded planner identifier for this draft.
        asset_references: Exact model.mzn and data.dzn paths returned by persistence.

    Returns:
        Canonical JSON with a verified result, sanitized rejection, or actionable
        missing-prerequisite result.
    """

    context = _context(runtime)
    choice = context.planner_choice
    problem = context.minizinc_problem
    if context.planning_intent is None or choice is None:
        return _prerequisite_missing(
            missing=("planning_intent", "planner_choice"),
            required_tool="record_planning_intent",
            retry_tool="planner_executor",
        )
    if planner_id != "minizinc" or choice.planner_choice != PlannerChoice(
        "temporal", "minizinc"
    ):
        raise ValueError("planner executor has no matching MiniZinc draft")
    if not context.planning_context_loaded:
        return _prerequisite_missing(
            missing=("planning_context",),
            required_tool="load_planning_context",
            retry_tool="planner_executor",
        )
    if problem is None:
        return _prerequisite_missing(
            missing=("minizinc_problem", "asset_references"),
            required_tool="persist_planner_assets",
            retry_tool="planner_executor",
        )
    supplied = tuple(sorted(str(Path(item).resolve()) for item in asset_references))
    if supplied != context.draft_references:
        raise ValueError("planner executor asset references do not match the draft")
    for reference in supplied:
        path = Path(reference)
        expected = problem.assets.get(path.name)
        if expected is None or path.read_bytes() != expected:
            raise ValueError(
                "planner executor draft content does not match persistence"
            )

    translation = context.minizinc_translation.plan(
        context.mission_input,
        choice,
        context.mission_snapshot,
        context.environment_event,
        lambda request: problem,
        plan_revision=(context.mission_snapshot.plan_revision or 0) + 1,
        start_attempt_number=context.current_attempt_number,
    )
    context.translation = translation
    attempt = translation.generation_attempts[-1]
    _emit(
        context,
        "planner-execution",
        str(translation.outcome),
        details={
            "attempt_id": attempt.attempt_id,
            "planner_id": planner_id,
            "plan_revision": (
                translation.normalized_plan.plan_revision
                if translation.normalized_plan is not None
                else (context.mission_snapshot.plan_revision or 0) + 1
            ),
        },
    )
    if translation.outcome is PlanningTranslationOutcome.VERIFIED:
        normalized_plan = translation.normalized_plan
        if normalized_plan is None:
            raise RuntimeError("verified planner translation lacks a Normalized Plan")
        return _canonical_json(
            {
                "attempt_id": attempt.attempt_id,
                "outcome": "verified",
                "planner_id": planner_id,
                "normalized_plan": normalized_plan.to_dict(),
            }
        )
    if translation.outcome is PlanningTranslationOutcome.REPAIR_EXHAUSTED:
        feedback = translation.correction_feedback[-1]
        retries_remaining = (
            context.max_planner_attempts - context.current_attempt_number
        )
        return _canonical_json(
            {
                "attempt_id": attempt.attempt_id,
                "attempt_outcome": "rejected",
                "correction_message": feedback.message,
                "correction_stage": str(feedback.stage),
                "outcome": (
                    "repair_exhausted" if retries_remaining == 0 else "rejected"
                ),
                "planner_id": planner_id,
                "retries_remaining": retries_remaining,
            }
        )
    return _canonical_json(
        {
            "attempt_id": attempt.attempt_id,
            "outcome": str(translation.outcome),
            "planner_id": planner_id,
        }
    )


def _statechart_rejection(
    context: HyperWorkflowContext,
    *,
    attempt_number: int,
    stage: str,
) -> str:
    messages = {
        "schema": "Generated Statechart data failed contract validation.",
        "machine_build": "Generated Statechart data could not instantiate the FSM engine.",
    }
    retries_remaining = context.max_statechart_attempts - attempt_number
    _emit(
        context,
        "statechart-generation",
        "rejected",
        details={"attempt_number": attempt_number, "stage": stage},
    )
    return _canonical_json(
        {
            "attempt_number": attempt_number,
            "correction_message": messages[stage],
            "correction_stage": stage,
            "outcome": (
                "repair_exhausted" if retries_remaining == 0 else "rejected"
            ),
            "retries_remaining": retries_remaining,
        }
    )


@tool(parse_docstring=True)
def submit_statechart_draft(
    attempt_number: int,
    statechart: dict[str, Any],
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Persist and validate one semantic Statechart topology draft.

    The tool binds model-authored topology to the verified NormalizedPlan, builds
    a live python-statemachine instance, and returns bounded sanitized feedback.

    Args:
        attempt_number: Positive sequential Statechart generation attempt.
        statechart: Topology containing exactly entry_state, terminal_states,
            states, state_context, and transitions. Each transition contains
            event, source, target, and conditions. Physical actions are omitted.

    Returns:
        Canonical JSON with accepted Statechart evidence or repair feedback.
    """

    context = _context(runtime)
    translation = context.translation
    plan = translation.normalized_plan if translation is not None else None
    if (
        translation is None
        or translation.outcome is not PlanningTranslationOutcome.VERIFIED
        or plan is None
    ):
        return _prerequisite_missing(
            missing=("verified_normalized_plan",),
            required_tool="planner_executor",
            retry_tool="submit_statechart_draft",
        )
    if context.state_machine_factory is None:
        raise RuntimeError("Hyper workflow has no Statechart machine factory")
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int):
        raise TypeError("Statechart attempt number must be an integer")
    if (
        attempt_number < 1
        or attempt_number > context.max_statechart_attempts
        or attempt_number > context.current_statechart_attempt + 1
        or attempt_number < context.current_statechart_attempt
    ):
        raise ValueError("Statechart attempt is outside the workflow retry sequence")

    directory = context.artifact_root / "statechart-attempts" / f"{attempt_number:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "statechart.json"
    topology_document = _canonical_json(statechart)
    if path.exists() and path.read_text(encoding="utf-8") != topology_document:
        raise ValueError("Statechart draft attempt identity conflicts")
    if not path.exists():
        path.write_text(topology_document, encoding="utf-8")
    context.current_statechart_attempt = attempt_number

    try:
        expected = {
            "entry_state",
            "terminal_states",
            "states",
            "state_context",
            "transitions",
        }
        if set(statechart) != expected:
            raise ValueError("Statechart topology contains unknown or missing fields")
        raw_transitions = statechart["transitions"]
        if not isinstance(raw_transitions, list):
            raise TypeError("Statechart transitions must be an array")
        transitions = []
        for raw in raw_transitions:
            if not isinstance(raw, Mapping) or set(raw) != {
                "event",
                "source",
                "target",
                "conditions",
            }:
                raise ValueError("Statechart transition topology is invalid")
            transitions.append(
                {
                    "event": raw["event"],
                    "source": raw["source"],
                    "target": raw["target"],
                    "maneuver_id": None,
                    "requires_lifecycle_fact": False,
                    "requires_decision": True,
                    "conditions": raw["conditions"],
                }
            )
        bound = {
            "schema_version": 1,
            "mission_id": plan.mission_id,
            "plan_revision": plan.plan_revision,
            "mission_snapshot_id": plan.mission_snapshot_id,
            "planning_profile": str(plan.planner_choice.planning_profile),
            "normalized_plan_sha256": hashlib.sha256(
                plan.to_canonical_json().encode("utf-8")
            ).hexdigest(),
            "entry_state": statechart["entry_state"],
            "terminal_states": statechart["terminal_states"],
            "states": statechart["states"],
            "state_context": statechart["state_context"],
            "transitions": transitions,
            "timers": {},
            "trusted": False,
        }
        chart = Statechart.from_dict(bound)
    except (TypeError, ValueError, KeyError):
        return _statechart_rejection(
            context, attempt_number=attempt_number, stage="schema"
        )

    try:
        machine = context.state_machine_factory.build(chart)
        if machine.current_state != chart.entry_state:
            raise RuntimeError("FSM engine entry state does not match Statechart")
    except Exception:  # noqa: BLE001 - external engine errors become safe feedback.
        return _statechart_rejection(
            context, attempt_number=attempt_number, stage="machine_build"
        )

    candidates = tuple(
        TransitionCandidate(
            event=item.event,
            source=item.source,
            target=item.target,
            requires_decision=True,
            conditions=item.conditions,
            source_state_context=chart.context_for(item.source),
            target_state_context=chart.context_for(item.target),
        )
        for item in chart.transitions
        if item.source == chart.entry_state
    )
    status = FSMStatus(
        mission_id=chart.mission_id,
        plan_revision=chart.plan_revision,
        statechart_revision=chart.statechart_revision,
        active_state=chart.entry_state,
        active_state_context=chart.context_for(chart.entry_state),
        transition_candidates=candidates,
        status="initialized",
    )
    accepted_path = directory / "accepted-statechart.json"
    document = chart.to_canonical_json()
    if not accepted_path.exists():
        accepted_path.write_text(document, encoding="utf-8")
    context.statechart = chart
    context.statechart_reference = str(accepted_path.resolve())
    context.initial_fsm_status = status
    _emit(
        context,
        "statechart-generation",
        "verified",
        details={
            "attempt_number": attempt_number,
            "statechart_sha256": chart.statechart_sha256,
            "state_count": len(chart.states),
            "transition_count": len(chart.transitions),
        },
    )
    return _canonical_json(
        {
            "attempt_number": attempt_number,
            "entry_state": chart.entry_state,
            "outcome": "verified",
            "state_count": len(chart.states),
            "statechart_reference": context.statechart_reference,
            "statechart_sha256": chart.statechart_sha256,
            "transition_count": len(chart.transitions),
        }
    )


def create_hyper_workflow_agent(
    *,
    model: Any,
    system_prompt: str,
    mission_id: str,
    memory_store: object | None = None,
    skill_catalog: object | None = None,
    skill_version: str | None = None,
    backend_root: Path | None = None,
    checkpointer: object | None = None,
    planner_workspace_location: str | None = None,
) -> object:
    """Create one Deep Agent that owns planning workflow state and tools."""

    return _create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        response_format=HYPER_WORKFLOW_RESULT_SCHEMA,
        mission_id=mission_id,
        role="hyper-agent",
        memory_store=memory_store,
        skill_catalog=skill_catalog,
        skill_version=skill_version,
        backend_root=backend_root,
        middleware=[TodoListMiddleware()],
        tools=[
            record_planning_intent,
            load_planning_context,
            persist_planner_assets,
            planner_executor,
            submit_statechart_draft,
        ],
        writable_paths=(
            [planner_workspace_location]
            if planner_workspace_location is not None
            else None
        ),
        context_schema=HyperWorkflowContext,
        checkpointer=checkpointer,
    )


class DeepAgentsHyperWorkflow:
    """Invoke and validate one workflow-level Hyper Deep Agent thread."""

    def __init__(self, agent: object) -> None:
        if not callable(getattr(agent, "invoke", None)):
            raise TypeError("Hyper workflow agent must expose invoke")
        self.agent = agent

    def run(
        self,
        context: HyperWorkflowContext,
        *,
        thread_id: str,
        recursion_limit: int,
        max_empty_responses: int = 8,
    ) -> HyperWorkflowRunResult:
        if not isinstance(context, HyperWorkflowContext):
            raise TypeError("Hyper workflow requires HyperWorkflowContext")
        _emit(
            context,
            "workflow",
            "started",
            details={"operation": "hyper_workflow", "correlation_id": thread_id},
        )
        try:
            result = self._run(
                context,
                thread_id=thread_id,
                recursion_limit=recursion_limit,
                max_empty_responses=max_empty_responses,
            )
        except Exception as exc:
            _emit(
                context,
                "workflow",
                "failed",
                details={
                    "error_type": type(exc).__name__,
                    "operation": "hyper_workflow",
                },
            )
            raise
        _emit(
            context,
            "workflow",
            "completed",
            details={
                "operation": "hyper_workflow",
                "status": str(result.outcome),
            },
        )
        return result

    def _run(
        self,
        context: HyperWorkflowContext,
        *,
        thread_id: str,
        recursion_limit: int,
        max_empty_responses: int = 8,
    ) -> HyperWorkflowRunResult:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("Hyper workflow thread ID must be non-empty")
        if (
            isinstance(recursion_limit, bool)
            or not isinstance(recursion_limit, int)
            or recursion_limit < 1
        ):
            raise ValueError("Hyper workflow recursion limit must be positive")
        if (
            isinstance(max_empty_responses, bool)
            or not isinstance(max_empty_responses, int)
            or max_empty_responses < 0
        ):
            raise ValueError(
                "Hyper workflow empty-response retries must be non-negative"
            )
        config: dict[str, object] = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion_limit,
        }
        callback = getattr(self.agent, "_onr_debug_callback", None)
        if callback is not None:
            config["callbacks"] = [callback]
        invoke = cast(Callable[..., object], cast(Any, self.agent).invoke)
        response: object = None
        candidate: object = None
        messages = [HumanMessage(content=context.mission_input.to_canonical_json())]
        for recovery_attempt in range(max_empty_responses + 1):
            response = invoke(
                {"messages": messages},
                config=config,
                context=context,
            )
            if not isinstance(response, Mapping):
                raise TypeError("Hyper workflow returned invalid agent state")
            candidate = response.get("structured_response")
            if candidate is not None:
                break
            if recovery_attempt == max_empty_responses:
                raise ValueError("Hyper workflow returned no structured output")
            messages = [
                HumanMessage(
                    content=_canonical_json(
                        {
                            "workflow_control": "continue",
                            "reason": "previous model response contained neither a tool call nor the final workflow result",
                        }
                    )
                )
            ]
        if not isinstance(response, Mapping):
            raise TypeError("Hyper workflow returned invalid agent state")
        model_dump = getattr(candidate, "model_dump", None)
        if callable(model_dump):
            candidate = model_dump()
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "mission_id",
            "outcome",
        }:
            raise ValueError("Hyper workflow returned invalid structured output")
        if candidate["mission_id"] != context.mission_input.mission_id:
            raise ValueError("Hyper workflow result Mission ID does not match")
        outcome = HyperWorkflowOutcome(cast(str, candidate["outcome"]))
        if outcome is HyperWorkflowOutcome.EXECUTION_READY:
            if (
                context.translation is None
                or context.translation.outcome
                is not PlanningTranslationOutcome.VERIFIED
                or context.translation.normalized_plan is None
                or context.statechart is None
                or context.statechart_reference is None
                or context.initial_fsm_status is None
            ):
                raise ValueError(
                    "Hyper workflow success lacks verified plan and Statechart evidence"
                )
        elif outcome is HyperWorkflowOutcome.PLANNER_REJECTED:
            if (
                context.translation is None
                or context.translation.outcome is PlanningTranslationOutcome.VERIFIED
                or (
                    context.translation.outcome
                    is PlanningTranslationOutcome.REPAIR_EXHAUSTED
                    and context.current_attempt_number < context.max_planner_attempts
                )
            ):
                raise ValueError("Hyper workflow rejection lacks planner evidence")
        elif (
            context.translation is None
            or context.translation.outcome is not PlanningTranslationOutcome.VERIFIED
            or context.current_statechart_attempt < context.max_statechart_attempts
            or context.statechart is not None
        ):
            raise ValueError("Hyper workflow rejection lacks Statechart evidence")
        raw_todos = response.get("todos", ())
        raw_messages = response.get("messages", ())
        if not isinstance(raw_todos, (list, tuple)) or not isinstance(
            raw_messages, (list, tuple)
        ):
            raise TypeError("Hyper workflow state is missing todos or messages")
        todos = tuple(
            {"content": str(item["content"]), "status": str(item["status"])}
            for item in raw_todos
            if isinstance(item, Mapping)
        )
        if len(todos) != len(raw_todos):
            raise ValueError("Hyper workflow todo state is invalid")
        if outcome is HyperWorkflowOutcome.EXECUTION_READY and any(
            todo["status"] != "completed" for todo in todos
        ):
            raise ValueError("Hyper workflow success requires completed todos")
        return HyperWorkflowRunResult(
            outcome=outcome,
            todos=todos,
            messages=tuple(cast(list[BaseMessage], raw_messages)),
            planning_intent=context.planning_intent,
            planner_choice=context.planner_choice,
            translation=context.translation,
            normalized_plan=(
                context.translation.normalized_plan
                if context.translation is not None
                else None
            ),
            statechart=context.statechart,
            statechart_reference=context.statechart_reference,
            initial_fsm_status=context.initial_fsm_status,
        )


__all__ = [
    "HYPER_WORKFLOW_RESULT_SCHEMA",
    "DeepAgentsHyperWorkflow",
    "HyperWorkflowContext",
    "HyperWorkflowRunResult",
    "TemporalManeuverCandidate",
    "create_hyper_workflow_agent",
    "load_planning_context",
    "persist_planner_assets",
    "planner_executor",
    "record_planning_intent",
    "submit_statechart_draft",
]
