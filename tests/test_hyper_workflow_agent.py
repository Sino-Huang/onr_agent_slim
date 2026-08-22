from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import ToolRuntime

from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.agents.hyper_workflow import (
    HyperWorkflowContext,
    _allowed_workflow_tools,
    create_hyper_workflow_agent,
    handoff_execution,
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
        self.executed: list[dict[str, bytes]] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked.append(dict(assets))
        return self.check_result

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        self.executed.append(dict(assets))
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
    communication = _Communication() if handoff else None
    return HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=event,
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
        reflection="Executing exact accepted planner files.",
        runtime=_runtime(context),
    )


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
                "conditions": [],
            }
        ],
    }


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
    assert set(cast(Any, planner_executor).args) == expected


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
    paths = _write(context, planner)
    submitted = _submit(context, planner, paths)
    assert submitted.startswith("status: success")
    verifier = (
        context.minizinc_planner if planner == "minizinc" else context.val_validator
    )
    assert cast(Any, verifier).checked
    assert _allowed_workflow_tools(context) == {"planner_executor"}


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
    native = '{"type":"solution","output":{"default":"route"}}\n{"type":"status","status":"SATISFIED"}\n'
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


def test_statechart_binds_planner_plan_and_handoff_contains_no_plan(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, handoff=True)
    _record(context, "fast-downward")
    paths = _write(context, "fast-downward")
    _submit(context, "fast-downward", paths)
    _execute(context, "fast-downward", paths)
    accepted = cast(Any, submit_statechart_draft).func(
        statechart=_statechart(),
        reflection="Binding FSM semantics.",
        runtime=_runtime(context),
    )
    assert accepted.startswith("Statechart validation passed")
    assert context.statechart.plan_revision == context.planner_plan.plan_revision
    assert (
        cast(Any, handoff_execution).func(
            reflection="Activating FSM.", runtime=_runtime(context)
        )
        == "Execution handoff completed."
    )
    communication = cast(_Communication, context.communication_port)
    payload = cast(AgentMessage, communication.message).payload
    assert "normalized_plan" not in payload
    assert "planner_plan" not in payload
    assert set(payload) == {
        "request_id",
        "correlation_id",
        "mission_id",
        "plan_revision",
        "statechart_reference",
        "fsm_status",
        "environment_data",
        "belief_snapshot",
        "available_recipients",
        "planning_snapshot",
    }
