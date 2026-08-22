"""One Deep Agent workflow for environment-backed planning and correction."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from typing import Any, Literal, cast

from langchain.agents.middleware import TodoListMiddleware, wrap_model_call
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import BaseMessage, HumanMessage

from onr.agents.hyper_agent import (
    _create_deep_agent,
    _parse_planning_intent_response,
)
from onr.contracts.bayesian_belief import BayesianBeliefSnapshot
from onr.contracts.communication import AgentMessage
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, Statechart, TransitionCandidate
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.maneuver_control import (
    ManeuverHeartbeatCompletion,
    ManeuverInvocation,
)
from onr.contracts.planner_translation import validate_environment_data
from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlannerPlan,
    PlannerStaticCheckResult,
    PlanningOutcome,
    SymbolicPlannerExecutionResult,
)
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.planning_intent import PlanningIntent
from onr.contracts.transport import CommandOutcome, TransportEvent

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


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _model_environment_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _model_environment_payload(item)
            for key, item in value.items()
            if key != "environment_file"
        }
    if isinstance(value, (list, tuple)):
        return [_model_environment_payload(item) for item in value]
    return value


def _prerequisite_missing(*, required_tool: str, retry_tool: str) -> str:
    return f"Cannot run {retry_tool} yet. Call {required_tool} first."


def _planner_asset_locations(
    context: HyperWorkflowContext, planner_id: str
) -> dict[str, str]:
    workspace = cast(str, context.planner_workspace_location).rstrip("/")
    filenames = (
        ("model.mzn", "data.dzn")
        if planner_id == "minizinc"
        else ("domain.pddl", "problem.pddl")
    )
    return {name: f"{workspace}/001/{name}" for name in filenames}


def _host_path(context: HyperWorkflowContext, location: str) -> Path:
    if context.backend_root is not None and location.startswith("/"):
        path = context.backend_root / location.removeprefix("/")
    else:
        path = Path(location)
    return path.resolve()


def _sandbox_path(context: HyperWorkflowContext, path: Path) -> str:
    resolved = path.resolve()
    if context.backend_root is None:
        return str(resolved)
    try:
        relative = resolved.relative_to(context.backend_root)
    except ValueError:
        return str(resolved)
    return "/" + relative.as_posix()


def _resolve_planner_files(
    context: HyperWorkflowContext,
    planner_choice: str,
    locations: list[str],
) -> tuple[dict[str, bytes], dict[str, str], dict[str, Path]]:
    choice = context.planner_choice
    if choice is None or choice.planner_choice.planner_id != planner_choice:
        raise ValueError("planner choice does not match the recorded Planner Choice")
    expected = _planner_asset_locations(context, planner_choice)
    if len(locations) != 2 or len(set(locations)) != 2:
        raise ValueError("planner submission requires exactly two distinct file paths")
    if set(locations) != set(expected.values()):
        raise ValueError(
            "planner file paths do not match the recorded planner workspace"
        )
    sandbox_by_name = {Path(location).name: location for location in locations}
    host_by_name = {
        name: _host_path(context, location)
        for name, location in sandbox_by_name.items()
    }
    if set(host_by_name) != set(expected) or any(
        not path.is_file() for path in host_by_name.values()
    ):
        raise ValueError("planner submission files do not exist at the expected paths")
    assets = {name: path.read_bytes() for name, path in host_by_name.items()}
    return assets, sandbox_by_name, host_by_name


def _remap_planner_paths(
    text: str,
    sandbox_by_name: Mapping[str, str],
    host_by_name: Mapping[str, Path],
) -> str:
    remapped = text
    for name, sandbox in sandbox_by_name.items():
        remapped = remapped.replace(str(host_by_name[name]), sandbox)
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:)?(?:[/\\][^\s:'\"()<>]+)*[/\\]?{re.escape(name)}"
        )
        remapped = pattern.sub(sandbox, remapped)
    return remapped


def _tool_result(
    *,
    success: bool,
    stdout: str,
    stderr: str,
    instruction: str,
) -> str:
    return (
        f"status: {'success' if success else 'failed'}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}\n"
        f"instruction: {instruction}"
    )


def _snapshot_submission(
    context: HyperWorkflowContext, assets: Mapping[str, bytes]
) -> None:
    directory = (
        context.artifact_root
        / "planner-attempts"
        / f"{context.current_attempt_number:03d}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    for name, contents in assets.items():
        (directory / name).write_bytes(contents)


def _planner_files_exist(context: HyperWorkflowContext) -> bool:
    choice = context.planner_choice
    if choice is None or choice.planner_choice.planner_id is None:
        return False
    try:
        return all(
            _host_path(context, location).is_file()
            for location in _planner_asset_locations(
                context, choice.planner_choice.planner_id
            ).values()
        )
    except OSError:
        return False


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
    minizinc_planner: Any
    fast_downward_planner: Any
    val_validator: Any
    belief_snapshot: Any = None
    max_planner_attempts: int = 1
    max_statechart_attempts: int = 3
    state_machine_factory: Any = None
    operational_log: Any = None
    backend_root: Path | None = None
    planner_workspace_location: str | None = None
    planning_intent: Any = field(default=None, init=False)
    planner_choice: Any = field(default=None, init=False)
    submitted_planner_choice: str | None = field(default=None, init=False)
    submitted_file_locations: tuple[str, ...] = field(default=(), init=False)
    submitted_assets: Any = field(default=None, init=False)
    submitted_sandbox_paths: Any = field(default=None, init=False)
    submitted_host_paths: Any = field(default=None, init=False)
    static_check_result: Any = field(default=None, init=False)
    static_accepted: bool = field(default=False, init=False)
    submission_result: str | None = field(default=None, init=False)
    planner_plan: Any = field(default=None, init=False)
    planner_execution_outcome: Any = field(default=None, init=False)
    current_attempt_number: int = field(default=0, init=False)
    executed_attempt_number: int = field(default=0, init=False)
    statechart: Any = field(default=None, init=False)
    statechart_reference: str | None = field(default=None, init=False)
    initial_fsm_status: Any = field(default=None, init=False)
    preview_fsm_status: Any = field(default=None, init=False)
    handoff_outcome: Any = field(default=None, init=False)
    handoff_attempt: int = field(default=0, init=False)
    handoff_correlation_id: str | None = field(default=None, init=False)
    current_statechart_attempt: int = field(default=0, init=False)
    fsm_runner: Any = None
    environment_authority: Any = None
    belief_service: Any = None
    communication_port: Any = None

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
        if not callable(getattr(self.minizinc_planner, "check", None)) or not callable(
            getattr(self.minizinc_planner, "execute", None)
        ):
            raise TypeError(
                "Hyper workflow MiniZinc planner must expose check and execute"
            )
        if not callable(getattr(self.fast_downward_planner, "execute", None)):
            raise TypeError("Hyper workflow Fast Downward planner must expose execute")
        if not callable(getattr(self.val_validator, "check", None)) or not callable(
            getattr(self.val_validator, "validate", None)
        ):
            raise TypeError(
                "Hyper workflow VAL verifier must expose check and validate"
            )
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
            raise ValueError(
                "Hyper workflow Statechart attempt budget must be positive"
            )
        if self.state_machine_factory is not None and not callable(
            getattr(self.state_machine_factory, "build", None)
        ):
            raise TypeError("Hyper workflow Statechart factory must expose build")
        if self.fsm_runner is not None and (
            not callable(getattr(self.fsm_runner, "activate", None))
            or not callable(getattr(self.fsm_runner, "status", None))
        ):
            raise TypeError("Hyper workflow FSM Runner must expose activate and status")
        if self.communication_port is not None and not callable(
            getattr(self.communication_port, "request", None)
        ):
            raise TypeError("Hyper workflow communication port must expose request")
        self.artifact_root = Path(self.artifact_root).resolve()
        if self.backend_root is not None:
            self.backend_root = Path(self.backend_root).resolve()
        if self.planner_workspace_location is None:
            self.planner_workspace_location = str(
                (self.artifact_root / "workspace").resolve()
            )

    @property
    def handoff_required(self) -> bool:
        return self.fsm_runner is not None or self.communication_port is not None


_PHASE_CONTROLLED_TOOLS = frozenset(
    {
        "record_planning_intent",
        "write_file",
        "edit_file",
        "submit_planner_attempt",
        "planner_executor",
        "submit_statechart_draft",
        "handoff_execution",
        "HyperWorkflowResultCandidate",
    }
)


def _allowed_workflow_tools(context: HyperWorkflowContext) -> frozenset[str]:
    if context.planning_intent is None or context.planner_choice is None:
        return frozenset({"record_planning_intent"})
    if context.static_accepted and (
        context.current_attempt_number > context.executed_attempt_number
    ):
        return frozenset({"planner_executor"})
    if context.planner_plan is None:
        if context.current_attempt_number >= context.max_planner_attempts:
            return frozenset({"HyperWorkflowResultCandidate"})
        allowed = {"write_file", "edit_file"}
        if _planner_files_exist(context):
            allowed.add("submit_planner_attempt")
        return frozenset(allowed)
    if context.statechart is not None and context.handoff_required:
        if context.handoff_outcome is None:
            return frozenset({"handoff_execution"})
        return frozenset({"HyperWorkflowResultCandidate"})
    if context.statechart is not None:
        return frozenset({"HyperWorkflowResultCandidate"})
    if context.current_statechart_attempt < context.max_statechart_attempts:
        return frozenset({"submit_statechart_draft"})
    return frozenset({"HyperWorkflowResultCandidate"})


def _request_tool_name(value: object) -> str | None:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(value, Mapping):
        function = value.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return cast(str, function["name"])
    return None


@wrap_model_call
def _gate_workflow_tools(request: Any, handler: Callable[[Any], Any]) -> Any:
    runtime = request.runtime
    context = runtime.context if runtime is not None else None
    if not isinstance(context, HyperWorkflowContext):
        raise TypeError("Hyper workflow model call requires HyperWorkflowContext")
    allowed = _allowed_workflow_tools(context)
    tools = [
        item
        for item in request.tools
        if (name := _request_tool_name(item)) not in _PHASE_CONTROLLED_TOOLS
        or name in allowed
    ]
    response_format = (
        request.response_format if "HyperWorkflowResultCandidate" in allowed else None
    )
    return handler(request.override(tools=tools, response_format=response_format))


@dataclass(frozen=True, slots=True)
class HyperWorkflowRunResult:
    """Validated evidence returned by one workflow-level Deep Agent run."""

    outcome: HyperWorkflowOutcome
    todos: tuple[Mapping[str, str], ...]
    messages: tuple[BaseMessage, ...]
    planning_intent: PlanningIntent | None
    planner_choice: PlannerChoiceRecord | None
    planner_plan: PlannerPlan | None
    statechart: Statechart | None
    statechart_reference: str | None
    initial_fsm_status: FSMStatus | None
    handoff_outcome: CommandOutcome | None


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
    objective: str,
    planning_profile: Literal["temporal", "symbolic"],
    planner_id: Literal["minizinc", "fast-downward"],
    rationale: str,
    details: dict[str, Any],
    reflection: str,
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Validate and record the workflow's derived PlanningIntent and Planner Choice.

    Use this after reading the mission-parsing skill. Planner choice is recorded in
    the same call so the workflow can continue instead of ending at structured output.

    Args:
        objective: Concise planner-facing Mission objective.
        planning_profile: Temporal scheduling or symbolic reachability profile.
        planner_id: Configured planner ID for the selected profile.
        rationale: Concise public planner-choice rationale.
        details: JSON-safe planner-selection facts derived from Mission Intent.
        reflection: Concise public summary of observed evidence and the immediate
            next action. Do not include private reasoning.

    Returns:
        Acceptance with planner-native paths and exact file-generation evidence.
    """

    context = _context(runtime)
    candidate = {
        "mission_id": context.mission_input.mission_id,
        "source_authority": context.mission_input.source_authority,
        "objective": objective,
        "planner_choice": {
            "planning_profile": planning_profile,
            "planner_id": planner_id,
        },
        "rationale": rationale,
        "details": details,
    }
    try:
        intent = _parse_planning_intent_response(
            {"structured_response": candidate}, context.mission_input
        )
        choice = PlannerChoiceRecord.from_planning_intent(intent)
    except (TypeError, ValueError) as exc:
        return f"Planning intent rejected: {type(exc).__name__}: {exc}"
    try:
        validate_environment_data(
            context.mission_input.mission_id,
            context.mission_snapshot,
            context.environment_event,
        )
    except ValueError as exc:
        return f"Planning intent rejected: {exc}"
    if context.planning_intent is not None or context.planner_choice is not None:
        if context.planning_intent != intent or context.planner_choice != choice:
            raise ValueError("recorded PlanningIntent conflicts with this workflow")
        accepted = "Planning intent was already accepted."
    else:
        context.planning_intent = intent
        context.planner_choice = choice
        _emit(
            context,
            "planning-intent",
            "completed",
            details={
                "planner_id": choice.planner_choice.planner_id,
                "planning_profile": choice.planner_choice.planning_profile,
            },
        )
        _emit(
            context,
            "planner-choice",
            "completed",
            details={
                "planner_id": choice.planner_choice.planner_id,
                "planning_profile": choice.planner_choice.planning_profile,
                "rationale": choice.rationale,
            },
        )
        accepted = "Planning intent accepted."
    selected_planner = choice.planner_choice.planner_id
    if selected_planner is None:
        raise ValueError("recorded Planner Choice has no executable planner")
    locations = _planner_asset_locations(context, selected_planner)
    environment = _model_environment_payload(
        context.environment_event.to_dict()["payload"]
    )
    marginals = (
        [item.to_dict() for item in context.belief_snapshot.marginals]
        if context.belief_snapshot is not None
        else None
    )
    planner_label = "MiniZinc" if selected_planner == "minizinc" else "PDDL"
    file_lines = "\n".join(f"{name}: {path}" for name, path in locations.items())
    return (
        f"{accepted} Generate {planner_label} files at:\n"
        f"{file_lines}\n"
        f"Environment data:\n{_canonical_json(environment)}\n"
        f"Belief marginals:\n{_canonical_json(marginals)}"
    )


def _normalize_provider_tool_value(value: object) -> object:
    if isinstance(value, str):
        return value.replace('<|"|>', "")
    if isinstance(value, Mapping):
        return {
            _normalize_provider_tool_value(key): _normalize_provider_tool_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_provider_tool_value(item) for item in value]
    return value


@tool(parse_docstring=True)
def submit_planner_attempt(
    planner_choice: Literal["minizinc", "fast-downward"],
    planner_model_file_locations: list[str],
    reflection: str,
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Snapshot and externally verify one planner-native file set.

    Args:
        planner_choice: Selected external planner.
        planner_model_file_locations: Exact two sandbox paths returned after planner
            choice was recorded.
        reflection: Concise public summary of observed evidence and the immediate
            next action. Do not include private reasoning.

    Returns:
        Exact remapped verifier streams and the next workflow instruction.
    """

    context = _context(runtime)
    choice = context.planner_choice
    if context.planning_intent is None or choice is None:
        return _prerequisite_missing(
            required_tool="record_planning_intent",
            retry_tool="submit_planner_attempt",
        )
    attempt_number = context.current_attempt_number + 1
    if attempt_number > context.max_planner_attempts:
        raise ValueError("planner asset attempt is outside the workflow retry sequence")
    assets, sandbox_by_name, host_by_name = _resolve_planner_files(
        context, planner_choice, planner_model_file_locations
    )
    context.current_attempt_number = attempt_number
    _snapshot_submission(context, assets)
    context.submitted_planner_choice = planner_choice
    context.submitted_file_locations = tuple(planner_model_file_locations)
    context.submitted_assets = assets
    context.submitted_sandbox_paths = sandbox_by_name
    context.submitted_host_paths = host_by_name
    context.submission_result = None
    verifier = (
        context.minizinc_planner
        if planner_choice == "minizinc"
        else context.val_validator
    )
    static_check = verifier.check(assets)
    if not isinstance(static_check, PlannerStaticCheckResult):
        raise TypeError("planner verifier returned an invalid result")
    context.static_check_result = static_check
    context.static_accepted = static_check.accepted
    context.planner_plan = None
    context.planner_execution_outcome = None
    stdout = _remap_planner_paths(static_check.stdout, sandbox_by_name, host_by_name)
    stderr = _remap_planner_paths(static_check.stderr, sandbox_by_name, host_by_name)
    if static_check.accepted:
        instruction = (
            "Call planner_executor with the same planner_choice and "
            "planner_model_file_locations."
        )
    else:
        paths = ", ".join(planner_model_file_locations)
        instruction = (
            f"Call edit_file on the same submitted files ({paths}), then resubmit "
            "them with the same planner choice and paths."
        )
    _emit(
        context,
        "planner-assets",
        "accepted" if static_check.accepted else "rejected",
        details={
            "generated_assets": ",".join(sorted(assets)),
            "planner_id": planner_choice,
            "sequence": attempt_number,
        },
    )
    context.submission_result = _tool_result(
        success=static_check.accepted,
        stdout=stdout,
        stderr=stderr,
        instruction=instruction,
    )
    return context.submission_result


def _execution_streams(
    result: PlannerExecutionResult | SymbolicPlannerExecutionResult,
) -> tuple[str, str]:
    stdout = result.stdout
    stderr = result.stderr
    evidence = result.evidence
    if evidence is not None:
        if not stdout and evidence.stdout_path.is_file():
            stdout = evidence.stdout_path.read_text(encoding="utf-8")
        if not stderr and evidence.stderr_path.is_file():
            stderr = evidence.stderr_path.read_text(encoding="utf-8")
    return stdout, stderr


def _planner_failure_instruction(context: HyperWorkflowContext) -> str:
    paths = ", ".join(context.submitted_file_locations)
    return (
        "Call write_todos: move 'Generate planner files' back to in_progress; "
        "move planner submission, planner execution, and every later stage back "
        "to pending. Then call edit_file on the same planner files "
        f"({paths}) and resubmit them."
    )


def _persist_minizinc_plan(context: HyperWorkflowContext, plan_text: str) -> Path:
    directory = (
        context.artifact_root
        / "planner-plans"
        / f"{context.current_attempt_number:03d}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "minizinc.plan"
    path.write_text(plan_text, encoding="utf-8")
    return path.resolve()


def _fast_downward_plan(evidence: PlannerExecutionEvidence | None) -> Path | None:
    if evidence is None:
        return None
    return next(
        (
            path.resolve()
            for path in evidence.artifact_paths
            if path.name == "sas_plan" and path.is_file()
        ),
        None,
    )


@tool(parse_docstring=True)
def planner_executor(
    planner_choice: Literal["minizinc", "fast-downward"],
    planner_model_file_locations: list[str],
    reflection: str,
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Execute an externally verified planner-native file set.

    Args:
        planner_choice: Selected external planner.
        planner_model_file_locations: Exact two sandbox paths used for submission.
        reflection: Concise public summary of observed evidence and the immediate
            next action. Do not include private reasoning.

    Returns:
        Exact planner-native plan text or exact remapped failure streams.
    """

    context = _context(runtime)
    choice = context.planner_choice
    if context.planning_intent is None or choice is None:
        return _prerequisite_missing(
            required_tool="record_planning_intent",
            retry_tool="planner_executor",
        )
    assets, sandbox_by_name, host_by_name = _resolve_planner_files(
        context, planner_choice, planner_model_file_locations
    )
    if not context.static_accepted or context.static_check_result is None:
        return _prerequisite_missing(
            required_tool="submit_planner_attempt",
            retry_tool="planner_executor",
        )
    if (
        planner_choice != context.submitted_planner_choice
        or tuple(planner_model_file_locations) != context.submitted_file_locations
        or assets != context.submitted_assets
    ):
        raise ValueError("planner execution files do not match the accepted submission")
    if planner_choice == "minizinc":
        result = context.minizinc_planner.execute(context.submitted_assets)
        if not isinstance(result, PlannerExecutionResult):
            raise TypeError("MiniZinc planner returned an invalid result")
    else:
        result = context.fast_downward_planner.execute(context.submitted_assets)
        if not isinstance(result, SymbolicPlannerExecutionResult):
            raise TypeError("Fast Downward planner returned an invalid result")
    context.executed_attempt_number = context.current_attempt_number
    context.static_accepted = False
    context.planner_execution_outcome = result.outcome
    stdout, stderr = _execution_streams(result)
    plan_path: Path | None = None
    plan_text: str | None = None
    if result.outcome is PlanningOutcome.SOLVED:
        if planner_choice == "minizinc":
            plan_text = stdout
            plan_path = _persist_minizinc_plan(context, plan_text)
        else:
            plan_path = _fast_downward_plan(result.evidence)
            if plan_path is not None and context.val_validator.validate(
                result.evidence
            ):
                plan_text = plan_path.read_text(encoding="utf-8")
            else:
                validator_directory = (
                    result.evidence.artifact_directory
                    if result.evidence is not None
                    else None
                )
                if validator_directory is not None:
                    validator_stdout = validator_directory / "validator.stdout"
                    validator_stderr = validator_directory / "validator.stderr"
                    stdout = (
                        validator_stdout.read_text(encoding="utf-8")
                        if validator_stdout.is_file()
                        else ""
                    )
                    stderr = (
                        validator_stderr.read_text(encoding="utf-8")
                        if validator_stderr.is_file()
                        else ""
                    )
                plan_path = None
    _emit(
        context,
        "planner-execution",
        (
            "verified"
            if plan_path is not None
            else "rejected"
            if result.outcome is PlanningOutcome.SOLVED
            else str(result.outcome)
        ),
        details={
            "planner_id": planner_choice,
            "plan_revision": (context.mission_snapshot.plan_revision or 0) + 1,
        },
    )
    if plan_path is not None and plan_text is not None:
        reference = _sandbox_path(context, plan_path)
        context.planner_plan = PlannerPlan(
            mission_id=context.mission_input.mission_id,
            source_authority=context.mission_input.source_authority,
            plan_revision=(context.mission_snapshot.plan_revision or 0) + 1,
            mission_snapshot_id=(
                f"{context.mission_input.mission_id}:snapshot:"
                f"{context.mission_snapshot.version}"
            ),
            planner_choice=choice.planner_choice,
            outcome=PlanningOutcome.SOLVED,
            planner_native_plan_artifact_reference=reference,
        )
        return (
            "status: success\n"
            f"plan:\n{plan_text}\n"
            f"planner_native_plan_artifact_reference: {reference}\n"
            "instruction: Proceed to Statechart generation from this "
            "planner-native plan."
        )
    context.planner_plan = None
    return _tool_result(
        success=False,
        stdout=_remap_planner_paths(stdout, sandbox_by_name, host_by_name),
        stderr=_remap_planner_paths(stderr, sandbox_by_name, host_by_name),
        instruction=_planner_failure_instruction(context),
    )


def _statechart_rejection(
    context: HyperWorkflowContext,
    *,
    attempt_number: int,
    stage: str,
    diagnostic: str,
) -> str:
    retries_remaining = context.max_statechart_attempts - attempt_number
    directory = context.artifact_root / "statechart-attempts" / f"{attempt_number:03d}"
    (directory / "statechart-error.txt").write_text(diagnostic, encoding="utf-8")
    _emit(
        context,
        "statechart-generation",
        "rejected",
        details={"attempt_number": attempt_number, "stage": stage},
    )
    return (
        f"Statechart validation failed:\n{diagnostic}\n"
        f"{retries_remaining} Statechart attempts remain."
    )


@tool(parse_docstring=True)
def submit_statechart_draft(
    statechart: dict[str, Any],
    reflection: str,
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Persist and validate one semantic Statechart topology draft.

    The tool binds model-authored topology to the accepted PlannerPlan revision, builds
    a live python-statemachine instance, and returns exact bounded repair feedback.

    Args:
        statechart: Topology containing exactly entry_state, terminal_states,
            states, state_context, and transitions. Each transition contains
            event, source, target, and conditions. Physical actions are omitted.
        reflection: Concise public summary of observed evidence and the immediate
            next action. Do not include private reasoning.

    Returns:
        Concise acceptance or exact bounded repair feedback.
    """

    context = _context(runtime)
    plan = context.planner_plan
    if plan is None:
        return _prerequisite_missing(
            required_tool="planner_executor",
            retry_tool="submit_statechart_draft",
        )
    if context.state_machine_factory is None:
        raise RuntimeError("Hyper workflow has no Statechart machine factory")
    attempt_number = context.current_statechart_attempt + 1
    if attempt_number > context.max_statechart_attempts:
        raise ValueError("Statechart attempt is outside the workflow retry sequence")

    statechart = cast(dict[str, Any], _normalize_provider_tool_value(statechart))

    directory = context.artifact_root / "statechart-attempts" / f"{attempt_number:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "statechart.json"
    topology_document = _canonical_json(statechart)
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
            "entry_state": statechart["entry_state"],
            "terminal_states": statechart["terminal_states"],
            "states": statechart["states"],
            "state_context": statechart["state_context"],
            "transitions": transitions,
            "timers": {},
            "trusted": False,
        }
        chart = Statechart.from_dict(bound)
    except (TypeError, ValueError, KeyError) as exc:
        return _statechart_rejection(
            context,
            attempt_number=attempt_number,
            stage="schema",
            diagnostic=f"{type(exc).__name__}: {exc}",
        )

    try:
        machine = context.state_machine_factory.build(chart)
        if machine.current_state != chart.entry_state:
            raise RuntimeError("FSM engine entry state does not match Statechart")
    except Exception as exc:  # noqa: BLE001 - external engine diagnostics are feedback.
        return _statechart_rejection(
            context,
            attempt_number=attempt_number,
            stage="machine_build",
            diagnostic=f"{type(exc).__name__}: {exc}",
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
    accepted_path.write_text(document, encoding="utf-8")
    context.statechart = chart
    context.statechart_reference = str(accepted_path.resolve())
    context.initial_fsm_status = status
    context.preview_fsm_status = status
    if context.handoff_required:
        context.initial_fsm_status = None
    _emit(
        context,
        "statechart-generation",
        "verified",
        details={
            "attempt_number": attempt_number,
            "state_count": len(chart.states),
            "transition_count": len(chart.transitions),
        },
    )
    return "Statechart validation passed. Hand off execution next."


def _run_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: list[object] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except Exception as exc:  # noqa: BLE001 - preserve arbitrary awaitable failure.
            failure.append(exc)

    thread = Thread(target=target, name="hyper-handoff-fsm-adapter")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


def _live_environment(context: HyperWorkflowContext) -> Mapping[str, object]:
    authority = context.environment_authority
    if authority is not None:
        getter = getattr(authority, "current_environment_data", None)
        if callable(getter):
            value = getter()
            if not isinstance(value, Mapping):
                raise TypeError("environment authority returned invalid current data")
            return value
        event = getattr(authority, "latest_environment_event", None)
        payload = getattr(event, "payload", None)
        if isinstance(payload, Mapping):
            return payload
    return context.environment_event.payload


@tool(parse_docstring=True)
def handoff_execution(
    reflection: str,
    runtime: ToolRuntime[HyperWorkflowContext],
) -> str:
    """Activate the verified Statechart and synchronously invoke Maneuver Control.

    Args:
        reflection: Concise public summary of verified execution evidence and handoff.

    Returns:
        Canonical JSON containing the correlated Maneuver completion or retry evidence.
    """

    context = _context(runtime)
    plan = context.planner_plan
    if (
        plan is None
        or context.statechart is None
        or context.statechart_reference is None
    ):
        return _prerequisite_missing(
            required_tool="submit_statechart_draft",
            retry_tool="handoff_execution",
        )
    if context.fsm_runner is None or context.communication_port is None:
        raise RuntimeError(
            "Hyper execution handoff requires FSM Runner and communication port"
        )
    context.handoff_attempt += 1
    live_status = _run_sync(context.fsm_runner.activate(context.statechart))
    if not isinstance(live_status, FSMStatus):
        raise TypeError("FSM Runner activation did not return FSMStatus")
    refreshed = _run_sync(context.fsm_runner.status())
    if not isinstance(refreshed, FSMStatus):
        raise TypeError("FSM Runner status did not return FSMStatus")
    belief = cast(BayesianBeliefSnapshot | None, context.belief_snapshot)
    if context.belief_service is not None:
        loader = getattr(context.belief_service, "load_current_snapshot", None)
        if callable(loader):
            current_belief = loader()
            if current_belief is not None and not isinstance(
                current_belief, BayesianBeliefSnapshot
            ):
                raise TypeError("belief service returned invalid current snapshot")
            if current_belief is not None:
                belief = current_belief
    available = getattr(context.communication_port, "available_recipients", None)
    recipients = (
        cast(tuple[str, ...], tuple(cast(Any, available("maneuver-control"))))
        if callable(available)
        else ("hyper-agent",)
    )
    correlation_id = context.handoff_correlation_id or (
        f"execution-handoff:{plan.mission_id}:{plan.plan_revision}"
    )
    request_id = (
        f"hyper-handoff:{plan.mission_id}:{plan.plan_revision}:"
        f"{context.handoff_attempt}"
    )
    invocation = ManeuverInvocation(
        request_id=request_id,
        correlation_id=correlation_id,
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        statechart_reference=context.statechart_reference,
        fsm_status=refreshed,
        environment_data=_live_environment(context),
        belief_snapshot=belief,
        available_recipients=recipients,
        planning_snapshot=context.mission_snapshot,
    )
    message = AgentMessage(
        message_id=request_id,
        correlation_id=correlation_id,
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        sender="hyper-agent",
        recipient="maneuver-control",
        kind="invoke",
        payload=invocation.to_dict(),
    )
    outcome = context.communication_port.request(message)
    if (
        not isinstance(outcome, CommandOutcome)
        or outcome.command_id != request_id
        or outcome.correlation_id != correlation_id
        or outcome.mission_id != plan.mission_id
        or str(outcome.status) != "completed"
    ):
        reason = "Maneuver Control handoff did not complete successfully"
        if isinstance(outcome, CommandOutcome):
            error = outcome.payload.get("error")
            if isinstance(error, str) and error.strip():
                reason = error
        return f"Execution handoff failed: {reason}."
    completion = ManeuverHeartbeatCompletion.from_dict(outcome.payload)
    if completion.mission_id != plan.mission_id or completion.request_id != request_id:
        raise ValueError("Maneuver handoff completion identity does not match")
    context.initial_fsm_status = refreshed
    context.handoff_outcome = outcome
    _emit(
        context,
        "maneuver-handoff",
        "completed",
        details={
            "correlation_id": correlation_id,
            "plan_revision": plan.plan_revision,
            "request_id": request_id,
        },
    )
    return "Execution handoff completed."


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
        middleware=[
            TodoListMiddleware(),
            _gate_workflow_tools,
        ],
        tools=[
            record_planning_intent,
            submit_planner_attempt,
            planner_executor,
            submit_statechart_draft,
            handoff_execution,
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
        context.handoff_correlation_id = thread_id
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
                context.planner_plan is None
                or context.statechart is None
                or context.statechart_reference is None
                or context.initial_fsm_status is None
                or (context.handoff_required and context.handoff_outcome is None)
            ):
                raise ValueError(
                    "Hyper workflow success lacks verified plan and Statechart evidence"
                )
        elif outcome is HyperWorkflowOutcome.PLANNER_REJECTED:
            if (
                context.planner_plan is not None
                or (
                    context.planner_execution_outcome is None
                    and (
                        context.static_check_result is None
                        or context.static_check_result.accepted
                    )
                )
                or context.current_attempt_number < context.max_planner_attempts
            ):
                raise ValueError("Hyper workflow rejection lacks planner evidence")
        elif (
            context.planner_plan is None
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
            planner_plan=context.planner_plan,
            statechart=context.statechart,
            statechart_reference=context.statechart_reference,
            initial_fsm_status=context.initial_fsm_status,
            handoff_outcome=context.handoff_outcome,
        )


__all__ = [
    "HYPER_WORKFLOW_RESULT_SCHEMA",
    "DeepAgentsHyperWorkflow",
    "HyperWorkflowContext",
    "HyperWorkflowRunResult",
    "create_hyper_workflow_agent",
    "handoff_execution",
    "planner_executor",
    "record_planning_intent",
    "submit_planner_attempt",
    "submit_statechart_draft",
]
