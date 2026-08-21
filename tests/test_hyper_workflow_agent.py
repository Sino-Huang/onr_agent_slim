from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import ToolRuntime

from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.agents.hyper_workflow import (
    HyperWorkflowContext,
    TemporalManeuverCandidate,
    _allowed_workflow_tools,
    create_hyper_workflow_agent,
    handoff_execution,
    planner_executor,
    record_planning_intent,
    submit_planner_attempt,
    submit_statechart_draft,
)
from onr.application.bayesian_belief import belief_artifact_reference
from onr.application.minizinc_translation import MiniZincTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlannerStaticCheckResult,
    PlanningOutcome,
    TemporalAssignment,
)
from onr.contracts.transport import CommandOutcome, TransportEvent
from onr.demo.fake_belief import create_fake_entity_risk_snapshot

_BANNED = (
    "sha256",
    "planner_choice_record",
    "decision_id",
    "created_at",
    "mission_snapshot",
)


class _Planner:
    def __init__(
        self,
        evidence: PlannerExecutionEvidence,
        *,
        static_diagnostic: str | None = None,
        execution_diagnostic: str | None = None,
    ) -> None:
        self.evidence = evidence
        self.static_diagnostic = static_diagnostic
        self.execution_diagnostic = execution_diagnostic
        self.checked: list[dict[str, bytes]] = []
        self.executed: list[dict[str, bytes]] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked.append(dict(assets))
        if self.static_diagnostic is not None:
            diagnostic = self.static_diagnostic
            self.static_diagnostic = None
            return PlannerStaticCheckResult(False, 1, stderr=diagnostic)
        return PlannerStaticCheckResult(True, 0)

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        self.executed.append(dict(assets))
        for path in self.evidence.artifact_paths:
            path.write_bytes(assets[path.name])
        if self.execution_diagnostic is not None:
            return PlannerExecutionResult(
                PlanningOutcome.ERROR,
                evidence=self.evidence,
                return_code=7,
                stderr=self.execution_diagnostic,
            )
        return PlannerExecutionResult(
            PlanningOutcome.SOLVED,
            (
                TemporalAssignment(
                    "observe-ship-1",
                    0,
                    1,
                ),
            ),
            self.evidence,
        )


class _FSMRunner:
    def __init__(self) -> None:
        self.current = None

    async def activate(self, chart: object) -> object:
        self.current = chart
        return self._status()

    async def status(self) -> object:
        return self._status()

    def _status(self) -> object:
        chart = cast(Any, self.current)
        from onr.contracts.fsm import FSMStatus

        return FSMStatus(
            mission_id=chart.mission_id,
            plan_revision=chart.plan_revision,
            statechart_revision=chart.plan_revision,
            active_state=chart.entry_state,
            active_state_context=chart.context_for(chart.entry_state),
            transition_candidates=(),
            status="initialized",
        )


class _CommunicationPort:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def available_recipients(self, sender: str) -> tuple[str, ...]:
        _ = sender
        return ("maneuver-control",)

    def request(self, message: Any) -> CommandOutcome:
        if self.fail:
            return CommandOutcome(
                1,
                message.message_id,
                message.correlation_id,
                message.mission_id,
                "failed",
                {"error": "Maneuver Control is unavailable"},
            )
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
                "summary": "No immediate effect is required.",
            },
        )


def _runtime(context: HyperWorkflowContext) -> ToolRuntime[HyperWorkflowContext]:
    return ToolRuntime(
        state={"messages": []},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="test-tool-call",
        store=None,
    )


def _evidence(tmp_path: Path) -> PlannerExecutionEvidence:
    directory = tmp_path / "solver"
    directory.mkdir(parents=True)
    model = directory / "model.mzn"
    data = directory / "data.dzn"
    stdout = directory / "solver.stdout"
    stderr = directory / "solver.stderr"
    for path in (model, data, stdout, stderr):
        path.write_text("", encoding="utf-8")
    return PlannerExecutionEvidence(directory, (model, data), stdout, stderr)


def _context(
    tmp_path: Path,
    planner: _Planner,
    *,
    belief: bool = False,
    handoff: bool = False,
) -> HyperWorkflowContext:
    mission = MissionInput(
        "mission-hyper",
        "Observe the risky ship at the reported time with field of view coverage.",
        "mission-control",
    )
    scene = TransportEvent(
        schema_version=1,
        event_id="environment:mission-hyper:1",
        mission_id=mission.mission_id,
        sequence=0,
        event_kind="environment_data",
        payload={
            "environment_file": "/host/private/environment.json",
            "entities": [
                {"id": "ship-1", "x": 12.5, "y": 4.25},
                {"id": "drone-1", "x": 0.0, "y": 0.0, "fov_radius": 30},
            ],
            "events": [{"entity_id": "ship-1", "time": 0.5}],
        },
    )
    belief_snapshot = create_fake_entity_risk_snapshot(mission.mission_id) if belief else None
    revisions: dict[str, int] = {"environment_data": 1}
    references: dict[str, str] = {"environment_data": scene.event_id}
    health = {"environment_data": "healthy"}
    freshness = {"environment_data": True}
    if belief_snapshot is not None:
        revisions["bayesian_belief_snapshot"] = belief_snapshot.belief_revision
        references["bayesian_belief_snapshot"] = belief_artifact_reference(
            belief_snapshot.mission_id, belief_snapshot.content_sha256
        )
        health["bayesian_belief_snapshot"] = "healthy"
        freshness["bayesian_belief_snapshot"] = True
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=1,
        created_at="2026-08-20T00:00:00+00:00",
        environment_data=scene.event_id,
        bayesian_belief_snapshot=references.get("bayesian_belief_snapshot"),
        source_revisions=revisions,
        source_references=references,
        source_health=health,
        source_freshness=freshness,
    )
    artifacts = tmp_path / "artifacts"
    return HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=scene,
        belief_snapshot=belief_snapshot,
        artifact_root=artifacts,
        minizinc_translation=MiniZincTranslation(
            planner, artifacts / "generation-attempts", max_corrections=0
        ),
        max_planner_attempts=2,
        max_statechart_attempts=2,
        state_machine_factory=PythonStateMachineFactory(),
        fsm_runner=_FSMRunner() if handoff else None,
        communication_port=_CommunicationPort() if handoff else None,
    )


def _record(context: HyperWorkflowContext) -> str:
    return cast(Any, record_planning_intent).func(
        objective="Observe the risky ship at its reported time",
        planning_profile="temporal",
        planner_id="minizinc",
        rationale="Travel, event time, and field of view make this temporal.",
        details={"fov_rule": "observe within supplied radius"},
        reflection="Recording the temporal planning interpretation.",
        runtime=_runtime(context),
    )


def _write_problem(context: HyperWorkflowContext, attempt: int = 1) -> None:
    directory = context.artifact_root / "workspace" / f"{attempt:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.mzn").write_text("solve satisfy;\n", encoding="utf-8")
    (directory / "data.dzn").write_text("horizon = 2;\n", encoding="utf-8")


def _submit(context: HyperWorkflowContext) -> str:
    return cast(Any, submit_planner_attempt).func(
        horizon=2,
        maneuvers=[
            TemporalManeuverCandidate(
                maneuver_id="observe-ship-1",
                action="navigate_and_observe",
                parameters={},
                dependencies=[],
                duration=1,
            )
        ],
        reflection="Submitting the generated MiniZinc files.",
        runtime=_runtime(context),
    )


def _execute(context: HyperWorkflowContext) -> str:
    return cast(Any, planner_executor).func(
        reflection="Executing the accepted MiniZinc problem.",
        runtime=_runtime(context),
    )


def _valid_statechart() -> dict[str, object]:
    return {
        "entry_state": "waiting",
        "terminal_states": ["complete"],
        "states": ["waiting", "complete"],
        "state_context": {"waiting": {}, "complete": {}},
        "transitions": [
            {
                "event": "finish",
                "source": "waiting",
                "target": "complete",
                "conditions": [
                    {
                        "kind": "environment_time_at_or_after",
                        "time_tick": 0,
                        "time_scale": 1,
                    }
                ],
            }
        ],
    }


def test_tool_registration_drops_context_loader_and_simplifies_inputs(
    monkeypatch: Any,
) -> None:
    import onr.agents.hyper_workflow as workflow_module

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        workflow_module,
        "_create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    create_hyper_workflow_agent(
        model=object(), system_prompt="test", mission_id="mission-tools"
    )

    names = [cast(Any, item).name for item in cast(list[object], captured["tools"])]
    middleware = cast(list[object], captured["middleware"])
    assert type(middleware[0]) is TodoListMiddleware
    assert names == [
        "record_planning_intent",
        "submit_planner_attempt",
        "planner_executor",
        "submit_statechart_draft",
        "handoff_execution",
    ]
    assert not hasattr(workflow_module, "load_planning_context")
    assert "mission_id" not in cast(Any, record_planning_intent).args
    assert "source_authority" not in cast(Any, record_planning_intent).args
    assert set(cast(Any, planner_executor).args) == {"reflection"}
    assert "attempt_number" not in cast(Any, submit_statechart_draft).args
    assert "model_file_location" not in cast(Any, submit_planner_attempt).args
    assert "translator_id" not in cast(Any, submit_planner_attempt).args


def test_record_intent_immediately_opens_file_generation_with_exact_values(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, _Planner(_evidence(tmp_path)), belief=True)
    result = _record(context)

    assert result.startswith("Planning intent accepted.")
    assert str(context.artifact_root / "workspace" / "001" / "model.mzn") in result
    assert str(context.artifact_root / "workspace" / "001" / "data.dzn") in result
    environment = context.environment_event.to_dict()["payload"]
    environment.pop("environment_file")
    assert json.dumps(environment, sort_keys=True, separators=(",", ":")) in result
    expected_marginals = [item.to_dict() for item in context.belief_snapshot.marginals]
    assert json.dumps(expected_marginals, sort_keys=True, separators=(",", ":")) in result
    assert _allowed_workflow_tools(context) == {"write_file", "edit_file"}
    assert all(term not in result.casefold() for term in _BANNED)
    assert "environment_file" not in result


def test_static_results_are_concise_and_preserve_exact_repair_diagnostic(
    tmp_path: Path,
) -> None:
    diagnostic = "EXACT MiniZinc parser diagnostic at data.dzn:7"
    context = _context(
        tmp_path, _Planner(_evidence(tmp_path), static_diagnostic=diagnostic)
    )
    _record(context)
    _write_problem(context)

    rejected = _submit(context)
    assert diagnostic in rejected
    assert "1 planner attempts remain" in rejected
    assert str(context.artifact_root / "workspace" / "002" / "model.mzn") in rejected
    assert all(term not in rejected.casefold() for term in _BANNED)

    accepted = _submit(context)
    assert accepted == "Static verification passed. Execute MiniZinc next."


def test_execution_success_returns_every_verified_maneuver_field(tmp_path: Path) -> None:
    context = _context(tmp_path, _Planner(_evidence(tmp_path)))
    _record(context)
    _write_problem(context)
    assert _submit(context) == "Static verification passed. Execute MiniZinc next."

    result = _execute(context)

    assert result.startswith("MiniZinc execution and solution verification passed.")
    maneuvers = json.loads(result.split("\n", 1)[1])
    assert maneuvers == [
        {
            "dependencies": [],
            "duration": 1,
            "intent": {"action": "navigate_and_observe", "parameters": {}},
            "maneuver_id": "observe-ship-1",
            "start": 0,
        }
    ]
    assert all(term not in result.casefold() for term in _BANNED)


def test_execution_rejection_preserves_exact_diagnostic_and_repair_paths(
    tmp_path: Path,
) -> None:
    diagnostic = "EXACT planner stderr diagnostic"
    context = _context(
        tmp_path, _Planner(_evidence(tmp_path), execution_diagnostic=diagnostic)
    )
    _record(context)
    _write_problem(context)
    _submit(context)

    result = _execute(context)

    assert diagnostic in result
    assert "1 planner attempts remain" in result
    assert str(context.artifact_root / "workspace" / "002" / "data.dzn") in result


def test_statechart_tool_assigns_attempts_and_returns_exact_validation_error(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, _Planner(_evidence(tmp_path)))
    _record(context)
    _write_problem(context)
    _submit(context)
    _execute(context)

    rejected = cast(Any, submit_statechart_draft).func(
        statechart={"states": []},
        reflection="Submitting the Statechart topology.",
        runtime=_runtime(context),
    )
    assert "ValueError: Statechart topology contains unknown or missing fields" in rejected
    assert "1 Statechart attempts remain" in rejected

    accepted = cast(Any, submit_statechart_draft).func(
        statechart=_valid_statechart(),
        reflection="Repairing the Statechart topology.",
        runtime=_runtime(context),
    )
    assert accepted == "Statechart validation passed. Hand off execution next."
    assert context.current_statechart_attempt == 2


def test_handoff_tool_returns_only_completion_or_actionable_error(tmp_path: Path) -> None:
    context = _context(tmp_path, _Planner(_evidence(tmp_path)), handoff=True)
    _record(context)
    _write_problem(context)
    _submit(context)
    _execute(context)
    cast(Any, submit_statechart_draft).func(
        statechart=_valid_statechart(),
        reflection="Submitting the verified Statechart.",
        runtime=_runtime(context),
    )

    completed = cast(Any, handoff_execution).func(
        reflection="Handing verified execution to Maneuver Control.",
        runtime=_runtime(context),
    )
    assert completed == "Execution handoff completed."

    context.handoff_outcome = None
    context.communication_port = _CommunicationPort(fail=True)
    failed = cast(Any, handoff_execution).func(
        reflection="Retrying the execution handoff.",
        runtime=_runtime(context),
    )
    assert failed == "Execution handoff failed: Maneuver Control is unavailable."
