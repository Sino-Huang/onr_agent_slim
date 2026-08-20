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
        "outcome": {"enum": ["plan_ready", "planner_rejected"]},
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
    operational_log: Any = None
    planning_intent: Any = field(default=None, init=False)
    planner_choice: Any = field(default=None, init=False)
    planning_context_loaded: bool = field(default=False, init=False)
    minizinc_problem: Any = field(default=None, init=False)
    draft_references: tuple[str, ...] = field(default=(), init=False)
    translation: Any = field(default=None, init=False)
    current_attempt_number: int = field(default=0, init=False)

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
        self.artifact_root = Path(self.artifact_root).resolve()


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
    return choice.to_canonical_json()


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
        }
    )


@tool(parse_docstring=True)
def persist_planner_assets(
    attempt_number: int,
    model_mzn: str,
    data_dzn: str,
    horizon: int,
    maneuvers: list[TemporalManeuverCandidate],
    translator_id: str,
    translator_version: str,
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Persist one MiniZinc draft and its normalization template.

    Use this after reading creating-minizinc-problem-files and before calling
    planner_executor. Each call creates one attempt-specific immutable draft.

    Args:
        attempt_number: Positive generation-attempt sequence number.
        model_mzn: Complete model.mzn contents.
        data_dzn: Complete data.dzn contents.
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

    templates = tuple(_temporal_maneuver(item) for item in maneuvers)
    problem = MiniZincProblem(
        assets={
            "model.mzn": model_mzn.encode("utf-8"),
            "data.dzn": data_dzn.encode("utf-8"),
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
        return _canonical_json(
            {
                "attempt_id": attempt.attempt_id,
                "outcome": "verified",
                "planner_id": planner_id,
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
        ],
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
        if outcome is HyperWorkflowOutcome.PLAN_READY:
            if (
                context.translation is None
                or context.translation.outcome
                is not PlanningTranslationOutcome.VERIFIED
                or context.translation.normalized_plan is None
            ):
                raise ValueError("Hyper workflow success lacks a verified plan")
        elif (
            context.translation is None
            or context.translation.outcome is PlanningTranslationOutcome.VERIFIED
            or (
                context.translation.outcome
                is PlanningTranslationOutcome.REPAIR_EXHAUSTED
                and context.current_attempt_number < context.max_planner_attempts
            )
        ):
            raise ValueError("Hyper workflow rejection lacks planner evidence")
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
        if outcome is HyperWorkflowOutcome.PLAN_READY and any(
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
]
