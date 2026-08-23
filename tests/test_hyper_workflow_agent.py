from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import ToolRuntime

from onr.adapters.operational_log import InProcessOperationalLog
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.agents.hyper_workflow import (
    HyperWorkflowContext,
    _allowed_workflow_tools,
    _gate_workflow_tools,
    create_hyper_workflow_agent,
    handoff_execution,
    initialize_event_data_materialization,
    materialize_event_information_data,
    planner_executor,
    record_planning_intent,
    submit_planner_attempt,
    submit_statechart_draft,
)
from onr.contracts.communication import AgentMessage
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlannerStaticCheckResult,
    PlanningOutcome,
    SymbolicPlannerExecutionResult,
)
from onr.contracts.transport import CommandOutcome, TransportEvent


class _MiniZinc:
    def __init__(
        self,
        *,
        check: PlannerStaticCheckResult | None = None,
        execute: PlannerExecutionResult | None = None,
    ) -> None:
        self.check_result = check or PlannerStaticCheckResult(True, 0)
        self.execute_result = execute or PlannerExecutionResult(
            PlanningOutcome.SOLVED,
            stdout='{"type":"status","status":"SATISFIED"}\n',
        )
        self.checked: list[dict[str, bytes]] = []
        self.executed: list[tuple[dict[str, bytes], str]] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked.append(dict(assets))
        return self.check_result

    def execute(
        self, assets: Mapping[str, bytes], solver: str
    ) -> PlannerExecutionResult:
        self.executed.append((dict(assets), solver))
        return self.execute_result


class _FastDownward:
    def __init__(
        self,
        root: Path,
        *,
        outcome: PlanningOutcome = PlanningOutcome.SOLVED,
        stdout: str = "fd stdout\n",
        stderr: str = "",
    ) -> None:
        self.root = root
        self.outcome = outcome
        self.stdout = stdout
        self.stderr = stderr
        self.executed: list[dict[str, bytes]] = []

    def execute(self, assets: Mapping[str, bytes]) -> SymbolicPlannerExecutionResult:
        self.executed.append(dict(assets))
        directory = self.root / f"run-{len(self.executed)}"
        directory.mkdir(parents=True)
        for name, contents in assets.items():
            (directory / name).write_bytes(contents)
        if self.outcome is PlanningOutcome.SOLVED:
            (directory / "sas_plan").write_text(
                "(survey drone-1 site-a)\n; cost = 1 (unit cost)\n",
                encoding="utf-8",
            )
        (directory / "solver.stdout").write_text(self.stdout, encoding="utf-8")
        (directory / "solver.stderr").write_text(self.stderr, encoding="utf-8")
        evidence = PlannerExecutionEvidence(
            directory,
            tuple(
                path
                for path in directory.iterdir()
                if path.name not in {"solver.stdout", "solver.stderr"}
            ),
            directory / "solver.stdout",
            directory / "solver.stderr",
        )
        return SymbolicPlannerExecutionResult(
            self.outcome,
            evidence=evidence,
            return_code=0 if self.outcome is PlanningOutcome.SOLVED else 1,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class _VAL:
    def __init__(
        self,
        *,
        check: PlannerStaticCheckResult | None = None,
        accepts_plan: bool = True,
    ) -> None:
        self.check_result = check or PlannerStaticCheckResult(True, 0)
        self.accepts_plan = accepts_plan
        self.checked: list[dict[str, bytes]] = []
        self.validated: list[PlannerExecutionEvidence] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked.append(dict(assets))
        return self.check_result

    def validate(self, evidence: PlannerExecutionEvidence) -> bool:
        self.validated.append(evidence)
        (evidence.artifact_directory / "validator.stdout").write_text(
            "Plan valid\n" if self.accepts_plan else "Plan failed\n",
            encoding="utf-8",
        )
        (evidence.artifact_directory / "validator.stderr").write_text(
            "" if self.accepts_plan else "VAL rejected exact sas_plan\n",
            encoding="utf-8",
        )
        return self.accepts_plan


class _FSMRunner:
    def __init__(self) -> None:
        self.chart: Any = None

    async def activate(self, chart: object) -> FSMStatus:
        self.chart = chart
        return await self.status()

    async def status(self) -> FSMStatus:
        chart = self.chart
        return FSMStatus(
            mission_id=chart.mission_id,
            plan_revision=chart.plan_revision,
            statechart_revision=chart.plan_revision,
            active_state=chart.entry_state,
            active_state_context=chart.context_for(chart.entry_state),
            transition_candidates=(),
            status="initialized",
        )


class _Communication:
    def __init__(self) -> None:
        self.message: AgentMessage | None = None

    def available_recipients(self, sender: str) -> tuple[str, ...]:
        return ("maneuver-control",)

    def request(self, message: AgentMessage) -> CommandOutcome:
        self.message = message
        return CommandOutcome(
            1,
            message.message_id,
            message.correlation_id,
            message.mission_id,
            "completed",
            {
                "mission_id": message.mission_id,
                "request_id": message.message_id,
                "outcome": "no_change",
                "summary": "FSM is active.",
            },
        )


def _runtime(context: HyperWorkflowContext) -> ToolRuntime[HyperWorkflowContext]:
    return ToolRuntime(
        state={"messages": []},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="test",
        store=None,
    )


def _context(
    tmp_path: Path,
    *,
    minizinc: _MiniZinc | None = None,
    downward: _FastDownward | None = None,
    val: _VAL | None = None,
    handoff: bool = False,
) -> HyperWorkflowContext:
    mission = MissionInput("mission-1", "Survey and return", "mission-control")
    event = TransportEvent(
        1,
        "environment:1",
        mission.mission_id,
        0,
        "environment_data",
        {"drone": {"id": "drone-1", "site": "site-a"}},
    )
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=1,
        created_at="2026-08-22T00:00:00+10:00",
        environment_data=event.event_id,
        source_revisions={"environment_data": 1},
        source_references={"environment_data": event.event_id},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    backend = tmp_path / "backend"
    artifacts = backend / "artifacts"
    environment_file = backend / "var/environment/mission-1/environment.json"
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text(
        json.dumps(event.to_dict()["payload"]),
        encoding="utf-8",
    )
    communication = _Communication() if handoff else None
    return HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=event,
        environment_file=environment_file,
        artifact_root=artifacts,
        backend_root=backend,
        planner_workspace_location="/artifacts/workspace",
        minizinc_planner=minizinc or _MiniZinc(),
        fast_downward_planner=downward or _FastDownward(artifacts / "fd"),
        val_validator=val or _VAL(),
        max_planner_attempts=2,
        max_statechart_attempts=2,
        state_machine_factory=PythonStateMachineFactory(),
        fsm_runner=_FSMRunner() if handoff else None,
        communication_port=communication,
    )


def _record(context: HyperWorkflowContext, planner: str) -> str:
    symbolic = planner == "fast-downward"
    return cast(Any, record_planning_intent).func(
        objective="Survey and return",
        planning_profile="symbolic" if symbolic else "temporal",
        planner_id=planner,
        rationale="Reachability only" if symbolic else "Timing affects feasibility",
        details={"mission_pattern": "survey-return"},
        reflection="Recording planner choice.",
        runtime=_runtime(context),
    )


def _paths(context: HyperWorkflowContext, planner: str) -> list[str]:
    names = (
        ("model.mzn", "data.dzn")
        if planner == "minizinc"
        else ("domain.pddl", "problem.pddl")
    )
    return [f"/artifacts/workspace/001/{name}" for name in names]


def _write(context: HyperWorkflowContext, planner: str) -> list[str]:
    paths = _paths(context, planner)
    for location in paths:
        host = cast(Path, context.backend_root) / location.removeprefix("/")
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_text(f"contents for {host.name}\n", encoding="utf-8")
    return paths


def _submit(context: HyperWorkflowContext, planner: str, paths: list[str]) -> str:
    return cast(Any, submit_planner_attempt).func(
        planner_choice=planner,
        planner_model_file_locations=paths,
        reflection="Submitting exact planner files.",
        runtime=_runtime(context),
    )


def _execute(context: HyperWorkflowContext, planner: str, paths: list[str]) -> str:
    return cast(Any, planner_executor).func(
        planner_choice=planner,
        planner_model_file_locations=paths,
        minizinc_solver="coin-bc" if planner == "minizinc" else None,
        reflection="Executing exact accepted planner files.",
        runtime=_runtime(context),
    )


def test_planner_submission_accepts_equivalent_backend_relative_paths(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    expected = _write(context, "minizinc")
    relative = [location.removeprefix("/") for location in expected]

    result = _submit(context, "minizinc", relative)

    assert result.startswith("status: success\n")
    assert cast(_MiniZinc, context.minizinc_planner).checked[-1] == {
        "model.mzn": b"contents for model.mzn\n",
        "data.dzn": b"contents for data.dzn\n",
    }


def _write_minizinc_model(context: HyperWorkflowContext) -> Path:
    location = _paths(context, "minizinc")[0]
    host = cast(Path, context.backend_root) / location.removeprefix("/")
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text("int: event_count;\n", encoding="utf-8")
    return host


def _initialize_events(
    context: HyperWorkflowContext,
    count: int,
    fields: list[dict[str, str]],
    *,
    restart: bool = False,
) -> dict[str, object]:
    result = cast(Any, initialize_event_data_materialization).func(
        total_event_count=count,
        fields=fields,
        restart=restart,
        reflection="Initializing event arrays.",
        runtime=_runtime(context),
    )
    return cast(dict[str, object], json.loads(result))


def _materialize_events(
    context: HyperWorkflowContext,
    events: list[dict[str, object]],
    mapping: dict[str, list[str | int]],
) -> dict[str, object]:
    result = cast(Any, materialize_event_information_data).func(
        events=events,
        mapping=mapping,
        reflection="Materializing the next event batch.",
        runtime=_runtime(context),
    )
    return cast(dict[str, object], json.loads(result))


def _statechart() -> dict[str, object]:
    return {
        "entry_state": "surveying",
        "terminal_states": ["complete"],
        "states": ["surveying", "complete"],
        "state_context": {"surveying": {}, "complete": {}},
        "transitions": [
            {
                "event": "survey-complete",
                "source": "surveying",
                "target": "complete",
                "context": {"arbitrary": {"nested": [1, "two"]}},
            }
        ],
    }


def _write_statechart(context: HyperWorkflowContext, value: object) -> str:
    location = cast(str, context.statechart_file_location)
    host = cast(Path, context.backend_root) / location.removeprefix("/")
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(json.dumps(value), encoding="utf-8")
    generator = host.with_name("generate_statechart.py")
    generator.write_text("# mission-specific generator\n", encoding="utf-8")
    return location


def test_tool_interfaces_are_identical_and_planner_neutral(monkeypatch: Any) -> None:
    import onr.agents.hyper_workflow as module

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        module, "_create_deep_agent", lambda **kw: captured.update(kw) or object()
    )
    create_hyper_workflow_agent(model=object(), system_prompt="test", mission_id="m")
    assert type(cast(list[Any], captured["middleware"])[0]) is TodoListMiddleware
    expected = {"planner_choice", "planner_model_file_locations", "reflection"}
    assert set(cast(Any, submit_planner_attempt).args) == expected
    assert set(cast(Any, planner_executor).args) == expected | {"minizinc_solver"}
    assert set(cast(Any, initialize_event_data_materialization).args) == {
        "total_event_count",
        "fields",
        "restart",
        "reflection",
    }
    assert set(cast(Any, materialize_event_information_data).args) == {
        "events",
        "mapping",
        "reflection",
    }


def test_terminal_workflow_gate_exposes_only_structured_response(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    context.current_attempt_number = context.max_planner_attempts
    response_format = object()
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        tools=[
            SimpleNamespace(name="read_file"),
            SimpleNamespace(name="write_todos"),
            SimpleNamespace(name="submit_planner_attempt"),
        ],
        response_format=response_format,
        state={"todos": []},
    )
    request.override = lambda **changes: changes

    overridden = cast(Any, _gate_workflow_tools).wrap_model_call(
        request, lambda value: value
    )

    assert overridden["tools"] == []
    assert overridden["response_format"] is response_format


def test_success_gate_requires_final_todo_update_before_structured_response(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    context.planning_intent = object()
    context.planner_choice = object()
    context.planner_plan = object()
    context.statechart = object()
    response_format = object()
    write_todos = SimpleNamespace(name="write_todos")
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        tools=[write_todos, SimpleNamespace(name="read_file")],
        response_format=response_format,
        state={"todos": [{"content": "step", "status": "in_progress"}]},
    )
    request.override = lambda **changes: changes

    update = cast(Any, _gate_workflow_tools).wrap_model_call(
        request, lambda value: value
    )
    assert update == {"tools": [write_todos], "response_format": None}

    request.state = {
        "todos": [
            {"content": f"step-{index}", "status": "completed"} for index in range(8)
        ]
    }
    terminal = cast(Any, _gate_workflow_tools).wrap_model_call(
        request, lambda value: value
    )
    assert terminal == {"tools": [], "response_format": response_format}


def test_handoff_tool_recovers_when_verification_workflow_requires_no_handoff(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    context.planner_plan = object()
    context.statechart = object()
    context.statechart_reference = "accepted-statechart.json"

    result = cast(Any, handoff_execution).func(
        reflection="Statechart verification passed.",
        runtime=_runtime(context),
    )

    assert "owned by Context Coordination" in result
    assert "return execution_ready" in result


_EVENT_FIELDS = [
    {"target": "event_time_s", "dzn_type": "float", "normalization": "identity"},
    {"target": "event_entity", "dzn_type": "int", "normalization": "first_seen_index"},
    {"target": "event_active", "dzn_type": "bool", "normalization": "identity"},
    {"target": "event_label", "dzn_type": "string", "normalization": "identity"},
    {"target": "event_order", "dzn_type": "int", "normalization": "identity"},
]


def test_event_materialization_tracks_progress_changes_mapping_and_writes_aligned_dzn(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    assert _allowed_workflow_tools(context) == {
        "write_file",
        "edit_file",
        "initialize_event_data_materialization",
    }
    _write(context, "minizinc")
    assert "submit_planner_attempt" in _allowed_workflow_tools(context)

    initialized = _initialize_events(context, 3, _EVENT_FIELDS)
    assert initialized == {
        "status": "continue",
        "accepted_count": 0,
        "remaining_count": 3,
        "next_event_number": 1,
        "next_batch": {
            "start_event_number": 1,
            "end_event_number": 3,
            "event_count": 3,
        },
        "instruction": (
            "Use execute for one jq slice containing only event numbers 1 through "
            "3, then call materialize_event_information_data with that output as "
            "your very next tool call. An execute result does not accept a batch. "
            "Wait for the materialization result before reading any later slice."
        ),
    }
    assert "materialize_event_information_data" in _allowed_workflow_tools(context)
    assert "submit_planner_attempt" not in _allowed_workflow_tools(context)
    with pytest.raises(ValueError, match="materialization is incomplete"):
        _submit(context, "minizinc", _paths(context, "minizinc"))

    first_raw = {
        "time": 1,
        "entity": "ship-a",
        "meta": {"active": True},
        "kind": 'course "change"',
        "seq": 7,
    }
    first = _materialize_events(
        context,
        [
            {"event_number": 1, "event": first_raw},
            {"event_number": 2, "event": dict(first_raw)},
        ],
        {
            "event_time_s": ["time"],
            "event_entity": ["entity"],
            "event_active": ["meta", "active"],
            "event_label": ["kind"],
            "event_order": ["seq"],
            "unused": ["ignored"],
        },
    )
    assert first["accepted_count"] == 2
    assert first["remaining_count"] == 1
    assert first["next_event_number"] == 3
    assert first["warnings"] == ["Ignored undeclared mapping targets: unused"]

    completed = _materialize_events(
        context,
        [
            {
                "event_number": 3,
                "event": {
                    "payload": {
                        "at": 2.5,
                        "who": "ship-b",
                        "flags": [False],
                        "label": "rendezvous",
                        "order": 8,
                    }
                },
            }
        ],
        {
            "event_time_s": ["payload", "at"],
            "event_entity": ["payload", "who"],
            "event_active": ["payload", "flags", 0],
            "event_label": ["payload", "label"],
            "event_order": ["payload", "order"],
        },
    )
    assert completed["status"] == "complete"
    assert completed["data_file_path"] == "/artifacts/workspace/001/data.dzn"
    assert completed["entity_index_maps"] == {
        "event_entity": {"ship-a": 1, "ship-b": 2}
    }
    assert "event_count = 3" not in json.dumps(completed)
    data_path = cast(Path, context.backend_root) / "artifacts/workspace/001/data.dzn"
    assert data_path.read_text(encoding="utf-8") == (
        "event_count = 3;\n"
        "event_time_s = [1.0, 1.0, 2.5];\n"
        "event_entity = [1, 1, 2];\n"
        "event_active = [true, true, false];\n"
        'event_label = ["course \\"change\\"", "course \\"change\\"", "rendezvous"];\n'
        "event_order = [7, 7, 8];\n"
    )
    assert "submit_planner_attempt" in _allowed_workflow_tools(context)


def test_event_materialization_rejects_bad_batches_transactionally(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    _write_minizinc_model(context)
    fields = [_EVENT_FIELDS[0]]
    _initialize_events(context, 30, fields)
    mapping: dict[str, list[str | int]] = {"event_time_s": ["time"]}

    invalid_batches = [
        [
            {"event_number": number, "event": {"time": number}}
            for number in range(1, 27)
        ],
        [
            {"event_number": 1, "event": {"time": 1}},
            {"event_number": 1, "event": {"time": 1}},
        ],
    ]
    for batch in invalid_batches:
        with pytest.raises(ValueError):
            _materialize_events(context, batch, mapping)
        assert cast(Any, context.event_data_materialization).accepted_count == 0

    rejected = _materialize_events(
        context, [{"event_number": 2, "event": {"time": 2}}], mapping
    )
    assert rejected["status"] == "rejected"
    assert rejected["error"] == "event numbers must be contiguous and begin at 1"
    assert rejected["next_batch"] == {
        "start_event_number": 1,
        "end_event_number": 25,
        "event_count": 25,
    }
    assert cast(Any, context.event_data_materialization).accepted_count == 0

    for bad_mapping, event in (
        ({}, {"event_number": 1, "event": {"time": 1}}),
        ({"event_time_s": ["missing"]}, {"event_number": 1, "event": {"time": 1}}),
        ({"event_time_s": ["time"]}, {"event_number": 1, "event": {"time": "one"}}),
        ({"event_time_s": ["time", 0.5]}, {"event_number": 1, "event": {"time": [1]}}),
    ):
        with pytest.raises((TypeError, ValueError)):
            _materialize_events(context, [event], cast(Any, bad_mapping))
        assert cast(Any, context.event_data_materialization).accepted_count == 0

    accepted = _materialize_events(
        context, [{"event_number": 1, "event": {"time": 1}}], mapping
    )
    assert accepted["accepted_count"] == 1
    repeated = _materialize_events(
        context, [{"event_number": 1, "event": {"time": 1}}], mapping
    )
    assert repeated["status"] == "rejected"
    assert repeated["error"] == "event numbers must be contiguous and begin at 2"
    assert cast(Any, context.event_data_materialization).accepted_count == 1


def test_event_materialization_progress_directs_only_the_next_batch(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    _write_minizinc_model(context)
    fields = [_EVENT_FIELDS[0]]

    initialized = _initialize_events(context, 30, fields)

    assert initialized["next_batch"] == {
        "start_event_number": 1,
        "end_event_number": 25,
        "event_count": 25,
    }
    assert initialized["instruction"] == (
        "Use execute for one jq slice containing only event numbers 1 through 25, "
        "then call materialize_event_information_data with that output as your very "
        "next tool call. An execute result does not accept a batch. Wait for the "
        "materialization result before reading any later slice."
    )

    progress = _materialize_events(
        context,
        [
            {"event_number": number, "event": {"time": number}}
            for number in range(1, 26)
        ],
        {"event_time_s": ["time"]},
    )

    assert progress["next_batch"] == {
        "start_event_number": 26,
        "end_event_number": 30,
        "event_count": 5,
    }
    assert "only event numbers 26 through 30" in cast(str, progress["instruction"])
    assert "very next tool call" in cast(str, progress["instruction"])


def test_event_materialization_restart_discards_rows_file_and_definitions(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    model = _write_minizinc_model(context)
    model.unlink()
    rejected = cast(Any, initialize_event_data_materialization).func(
        total_event_count=1,
        fields=[_EVENT_FIELDS[0]],
        restart=False,
        reflection="Initializing event arrays.",
        runtime=_runtime(context),
    )
    assert "model.mzn must exist first" in rejected
    assert context.event_data_materialization is None
    _write_minizinc_model(context)
    _initialize_events(context, 1, [_EVENT_FIELDS[0]])
    completed = _materialize_events(
        context,
        [{"event_number": 1, "event": {"time": 1}}],
        {"event_time_s": ["time"]},
    )
    assert completed["status"] == "complete"
    data_path = cast(Path, context.backend_root) / "artifacts/workspace/001/data.dzn"
    assert data_path.is_file()
    with pytest.raises(ValueError, match="cannot change without restart"):
        _initialize_events(context, 2, [_EVENT_FIELDS[4]])

    restarted = _initialize_events(context, 2, [_EVENT_FIELDS[4]], restart=True)
    assert restarted["accepted_count"] == 0
    assert restarted["remaining_count"] == 2
    assert not data_path.exists()
    assert "submit_planner_attempt" not in _allowed_workflow_tools(context)


def test_event_materialization_atomic_write_failure_does_not_accept_batch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import onr.agents.hyper_workflow as module

    context = _context(tmp_path)
    _record(context, "minizinc")
    _write_minizinc_model(context)
    _initialize_events(context, 1, [_EVENT_FIELDS[0]])
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        _materialize_events(
            context,
            [{"event_number": 1, "event": {"time": 1}}],
            {"event_time_s": ["time"]},
        )
    assert cast(Any, context.event_data_materialization).accepted_count == 0
    assert not (
        cast(Path, context.backend_root) / "artifacts/workspace/001/data.dzn"
    ).exists()
    assert not list(
        (cast(Path, context.backend_root) / "artifacts/workspace/001").glob(
            ".data.dzn.tmp-*"
        )
    )


@pytest.mark.parametrize(
    ("planner", "expected_names"),
    [
        ("minizinc", ("model.mzn", "data.dzn")),
        ("fast-downward", ("domain.pddl", "problem.pddl")),
    ],
)
def test_recorded_choice_returns_matching_native_paths_and_reaches_verifier(
    tmp_path: Path, planner: str, expected_names: tuple[str, str]
) -> None:
    context = _context(tmp_path)
    result = _record(context, planner)
    assert all(f"/artifacts/workspace/001/{name}" in result for name in expected_names)
    assert "Environment file: var/environment/mission-1/environment.json" in result
    assert "jq '.static_info | length'" in result
    assert '"static_info":' not in result
    assert '"scene_graph":' not in result
    assert "drone-1" not in result
    paths = _write(context, planner)
    submitted = _submit(context, planner, paths)
    assert submitted.startswith("status: success")
    verifier = (
        context.minizinc_planner if planner == "minizinc" else context.val_validator
    )
    assert cast(Any, verifier).checked
    assert _allowed_workflow_tools(context) == {"planner_executor"}


def test_root_relative_environment_path_works_with_file_read_and_jq(
    tmp_path: Path,
) -> None:
    from deepagents.backends import LocalShellBackend

    context = _context(tmp_path)
    backend = LocalShellBackend(
        root_dir=context.backend_root,
        virtual_mode=True,
        inherit_env=False,
        env={"PATH": "/usr/bin:/bin"},
    )

    read_result = backend.read(context.environment_file_location)
    assert read_result.error is None
    assert (
        json.loads(cast(dict[str, str], read_result.file_data)["content"])
        == (context.environment_event.to_dict()["payload"])
    )
    jq_result = backend.execute(f"jq 'keys' {context.environment_file_location}")
    assert jq_result.exit_code == 0
    assert json.loads(jq_result.output) == ["drone"]


def test_environment_file_validation_rejects_missing_outside_stale_and_mismatch(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)

    def rebuild(environment_file: Path, *, backend_root: Path | None = None) -> None:
        HyperWorkflowContext(
            mission_input=context.mission_input,
            mission_snapshot=context.mission_snapshot,
            environment_event=context.environment_event,
            environment_file=environment_file,
            artifact_root=context.artifact_root,
            backend_root=context.backend_root if backend_root is None else backend_root,
            planner_workspace_location=context.planner_workspace_location,
            minizinc_planner=context.minizinc_planner,
            fast_downward_planner=context.fast_downward_planner,
            val_validator=context.val_validator,
        )

    missing = cast(Path, context.backend_root) / "var/environment/missing.json"
    with pytest.raises(ValueError, match="does not exist"):
        rebuild(missing)

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(context.environment_event.to_dict()["payload"]))
    with pytest.raises(ValueError, match="outside the backend root"):
        rebuild(outside)

    stale = cast(Path, context.backend_root) / "var/environment/stale.json"
    stale.write_text(json.dumps({"drone": {"id": "retired-drone"}}))
    with pytest.raises(ValueError, match="may be stale"):
        rebuild(stale)

    mismatched = cast(Path, context.backend_root) / "var/environment/mismatch.json"
    mismatched.write_text(json.dumps({"drone": {"id": "drone-2"}}))
    with pytest.raises(ValueError, match="does not match"):
        rebuild(mismatched)


@pytest.mark.parametrize(
    "locations",
    [
        [],
        ["/artifacts/workspace/001/model.mzn"],
        ["/artifacts/workspace/001/model.mzn"] * 2,
        ["/foreign/model.mzn", "/foreign/data.dzn"],
        [
            "/artifacts/workspace/001/domain.pddl",
            "/artifacts/workspace/001/problem.pddl",
        ],
    ],
)
def test_submission_rejects_missing_duplicate_foreign_and_wrong_planner_paths(
    tmp_path: Path, locations: list[str]
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    _write(context, "minizinc")
    with pytest.raises(ValueError):
        _submit(context, "minizinc", locations)


def test_static_failure_preserves_streams_remaps_paths_and_repairs_same_files(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _record(context, "minizinc")
    paths = _write(context, "minizinc")
    host = cast(Path, context.backend_root) / paths[0].removeprefix("/")
    temporary = context.artifact_root / "check-abc" / "data.dzn"
    context.minizinc_planner.check_result = PlannerStaticCheckResult(
        False,
        1,
        stdout=f"checking {host}\n",
        stderr=f"syntax error at {temporary}:7\n",
    )
    result = _submit(context, "minizinc", paths)
    assert f"checking {paths[0]}\n" in result
    assert f"syntax error at {paths[1]}:7\n" in result
    assert "edit_file on the same submitted files" in result
    assert "next" not in result.casefold()
    assert all(
        term not in result.casefold()
        for term in ("sha", "maneuver", "cost", "diagnostic_reference")
    )
    assert (context.artifact_root / "planner-attempts/001/model.mzn").is_file()


def test_minizinc_success_persists_and_returns_exact_native_output(
    tmp_path: Path,
) -> None:
    native = (
        '{"type":"solution","output":{"default":"route"}}\n'
        '{"type":"status","status":"SATISFIED"}\n'
    )
    context = _context(
        tmp_path,
        minizinc=_MiniZinc(
            execute=PlannerExecutionResult(PlanningOutcome.SOLVED, stdout=native)
        ),
    )
    _record(context, "minizinc")
    paths = _write(context, "minizinc")
    _submit(context, "minizinc", paths)
    result = _execute(context, "minizinc", paths)
    assert result.startswith("status: success\n")
    assert f"plan:\n{native}" in result
    assert context.planner_plan is not None
    reference = context.planner_plan.planner_native_plan_artifact_reference
    assert reference.startswith("/artifacts/planner-plans/")
    assert (
        cast(Path, context.backend_root) / reference.removeprefix("/")
    ).read_text() == native
    assert cast(_MiniZinc, context.minizinc_planner).executed[-1][1] == "coin-bc"
    assert (
        "statechart_generator_file_location: "
        "/artifacts/workspace/001/generate_statechart.py"
        in result
    )
    assert (
        "statechart_file_location: /artifacts/workspace/001/statechart.json" in result
    )


def test_planner_executor_requires_solver_only_for_minizinc(tmp_path: Path) -> None:
    minizinc_context = _context(tmp_path / "minizinc")
    _record(minizinc_context, "minizinc")
    minizinc_paths = _write(minizinc_context, "minizinc")
    _submit(minizinc_context, "minizinc", minizinc_paths)
    with pytest.raises(ValueError, match="requires minizinc_solver"):
        cast(Any, planner_executor).func(
            planner_choice="minizinc",
            planner_model_file_locations=minizinc_paths,
            minizinc_solver=None,
            reflection="Missing solver.",
            runtime=_runtime(minizinc_context),
        )

    downward_context = _context(tmp_path / "downward")
    _record(downward_context, "fast-downward")
    downward_paths = _write(downward_context, "fast-downward")
    _submit(downward_context, "fast-downward", downward_paths)
    with pytest.raises(ValueError, match="requires minizinc_solver to be null"):
        cast(Any, planner_executor).func(
            planner_choice="fast-downward",
            planner_model_file_locations=downward_paths,
            minizinc_solver="coin-bc",
            reflection="Wrong solver.",
            runtime=_runtime(downward_context),
        )


def test_planner_execution_log_records_selected_minizinc_solver(tmp_path: Path) -> None:
    context = _context(tmp_path)
    operational_log = InProcessOperationalLog()
    context.operational_log = operational_log
    _record(context, "minizinc")
    paths = _write(context, "minizinc")
    _submit(context, "minizinc", paths)

    _execute(context, "minizinc", paths)

    execution = next(
        record
        for record in operational_log.replay("mission-1")
        if record.event_kind == "planner-execution"
    )
    assert execution.details["minizinc_solver"] == "coin-bc"


def test_fast_downward_success_requires_val_and_returns_exact_sas_plan(
    tmp_path: Path,
) -> None:
    val = _VAL()
    context = _context(tmp_path, val=val)
    _record(context, "fast-downward")
    paths = _write(context, "fast-downward")
    _submit(context, "fast-downward", paths)
    result = _execute(context, "fast-downward", paths)
    plan = "(survey drone-1 site-a)\n; cost = 1 (unit cost)\n"
    assert f"plan:\n{plan}" in result
    assert len(val.validated) == 1
    assert {path.name for path in val.validated[0].artifact_paths} >= {
        "domain.pddl",
        "problem.pddl",
        "sas_plan",
    }


@pytest.mark.parametrize(
    "outcome",
    [PlanningOutcome.ERROR, PlanningOutcome.TIMEOUT, PlanningOutcome.UNSOLVABLE],
)
def test_execution_failure_returns_exact_streams_and_todo_rollback(
    tmp_path: Path, outcome: PlanningOutcome
) -> None:
    execution = PlannerExecutionResult(
        outcome,
        stdout="planner stdout\n",
        stderr="planner stderr\n",
    )
    context = _context(tmp_path, minizinc=_MiniZinc(execute=execution))
    _record(context, "minizinc")
    paths = _write(context, "minizinc")
    _submit(context, "minizinc", paths)
    result = _execute(context, "minizinc", paths)
    assert result.startswith("status: failed")
    assert "planner stdout\n" in result and "planner stderr\n" in result
    assert "write_todos" in result
    assert "'Generate planner files' back to in_progress" in result
    assert "every later stage back to pending" in result
    assert "edit_file on the same planner files" in result


def test_val_rejection_returns_val_streams_and_no_planner_plan(tmp_path: Path) -> None:
    context = _context(tmp_path, val=_VAL(accepts_plan=False))
    _record(context, "fast-downward")
    paths = _write(context, "fast-downward")
    _submit(context, "fast-downward", paths)
    result = _execute(context, "fast-downward", paths)
    assert result.startswith("status: failed")
    assert "Plan failed\n" in result
    assert "VAL rejected exact sas_plan\n" in result
    assert context.planner_plan is None


def test_statechart_binds_planner_plan_without_direct_agent_handoff(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, handoff=True)
    _record(context, "fast-downward")
    paths = _write(context, "fast-downward")
    _submit(context, "fast-downward", paths)
    _execute(context, "fast-downward", paths)
    location = _write_statechart(context, _statechart())
    accepted = cast(Any, submit_statechart_draft).func(
        statechart_file_location=location,
        reflection="Binding FSM semantics.",
        runtime=_runtime(context),
    )
    feedback = json.loads(accepted)
    assert feedback["status"] == "accepted"
    assert feedback["graph_counts"] == {"states": 2, "transitions": 1}
    assert context.statechart.plan_revision == context.planner_plan.plan_revision
    accepted_document = context.statechart.to_dict()
    assert accepted_document["schema_version"] == 2
    assert "planner_native_plan_artifact_reference" not in accepted_document
    assert "maneuver_id" not in accepted_document["transitions"][0]
    assert Path(cast(str, context.statechart_reference)).is_file()
    assert (context.artifact_root / "statechart-attempts/001/statechart.json").is_file()
    assert (
        cast(Path, context.backend_root)
        / "artifacts/workspace/001/generate_statechart.py"
    ).is_file()
    assert "owned by Context Coordination" in cast(Any, handoff_execution).func(
        reflection="Returning accepted execution artifacts.",
        runtime=_runtime(context),
    )
    communication = cast(_Communication, context.communication_port)
    assert communication.message is None


def test_statechart_submission_accepts_only_returned_path_and_repairs_same_files(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    context.max_statechart_attempts = 3
    _record(context, "minizinc")
    paths = _write(context, "minizinc")
    _submit(context, "minizinc", paths)
    _execute(context, "minizinc", paths)
    location = _write_statechart(context, {**_statechart(), "extra": True})

    wrong_path = cast(Any, submit_statechart_draft).func(
        statechart_file_location=str(
            cast(Path, context.backend_root) / location.removeprefix("/")
        ),
        reflection="Submitting an alternate representation.",
        runtime=_runtime(context),
    )
    wrong = json.loads(wrong_path)
    assert wrong["stage"] == "workspace_path"
    assert wrong["draft_path"] != location

    rejected_text = cast(Any, submit_statechart_draft).func(
        statechart_file_location=location,
        reflection="Submitting the authored draft.",
        runtime=_runtime(context),
    )
    rejected = json.loads(rejected_text)
    assert rejected["status"] == "rejected"
    assert rejected["stage"] == "schema"
    assert rejected["draft_path"] == location
    assert "same returned workspace paths" in rejected["required_next_action"]
    assert (context.artifact_root / "statechart-attempts/002/statechart.json").is_file()

    _write_statechart(context, _statechart())
    repaired_text = cast(Any, submit_statechart_draft).func(
        statechart_file_location=location,
        reflection="Resubmitting the repaired files at the same paths.",
        runtime=_runtime(context),
    )
    repaired = json.loads(repaired_text)
    assert repaired["status"] == "accepted"
    assert repaired["attempt_number"] == 3
    assert (context.artifact_root / "statechart-attempts/003/statechart.json").is_file()
