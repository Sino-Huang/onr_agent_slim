from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import onr.runtime.cli as runtime_cli
from onr.adapters.file_transport import FileTransport
from onr.application.bayesian_belief import belief_artifact_reference
from onr.contracts.context_coordination import (
    MissionSnapshot,
    create_source_fact_event,
)
from onr.contracts.fsm import FSMStatus, Statechart
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planning import (
    ManeuverIntent,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    ScheduledManeuver,
)
from onr.contracts.transport import CommandOutcome, TransportEvent
from onr.demo.fake_belief import create_fake_entity_risk_snapshot
from onr.ports.transport import Subscription
from onr.runtime.lease import RuntimeLeaseStore


def _mission_file(tmp_path: Path, **overrides: object) -> Path:
    value: dict[str, object] = {
        "mission_id": "mission:demo",
        "mission_text": "Survey the demo area without exposing this input.",
        "source_authority": "demo-operator",
    }
    value.update(overrides)
    path = tmp_path / "mission.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _normalized_plan() -> NormalizedPlan:
    return NormalizedPlan(
        mission_id="mission:demo",
        source_authority="demo-operator",
        plan_revision=3,
        mission_snapshot_id="mission:demo:snapshot:1",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            ScheduledManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent("survey"),
                dependencies=(),
                start=0,
                duration=1,
            ),
        ),
    )


def _role_prompt_files(tmp_path: Path) -> str:
    maneuver_prompt = "Temporary maneuver-control role prompt."
    prompt_root = tmp_path / "conf/system_prompt"
    path = prompt_root / "maneuver-control/SYSTEM.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(maneuver_prompt, encoding="utf-8")
    hyper_path = prompt_root / "hyper-agent/SYSTEM.md"
    hyper_path.parent.mkdir(parents=True, exist_ok=True)
    hyper_path.write_text("Temporary Hyper role prompt.", encoding="utf-8")
    return maneuver_prompt


def test_load_mission_file_is_exact_and_strict(tmp_path: Path) -> None:
    mission = runtime_cli.load_mission_file(_mission_file(tmp_path))
    assert mission == MissionInput(
        "mission:demo",
        "Survey the demo area without exposing this input.",
        "demo-operator",
    )

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be read as JSON"):
        runtime_cli.load_mission_file(invalid)

    with pytest.raises(ValueError, match="exactly the required fields"):
        runtime_cli.load_mission_file(_mission_file(tmp_path, unexpected="value"))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        runtime_cli.load_mission_file(_mission_file(tmp_path, mission_text="  "))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        runtime_cli.load_mission_file(_mission_file(tmp_path, source_authority=3))


def test_example_mission_requests_event_accounting_patrol() -> None:
    mission = runtime_cli.load_mission_file(Path("examples/mission.json"))

    assert mission.mission_text == (
        "Please patrol the environment and confirm that all the events mentioned in the event report are accounted for."
    )


def test_demo_environment_flag_is_explicitly_required(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        runtime_cli.main(
            [
                "--mission-file",
                str(_mission_file(tmp_path)),
            ]
        )
    assert exc.value.code == 2


def test_installed_cli_help_works_outside_checkout(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "onr.runtime.cli", "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0
    assert "--demo-environment" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


def test_demo_artifact_rollover_moves_prior_var_wholesale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior_files = {
        "transport/topics/events.json": "transport",
        "storage/operational-log/events.jsonl": "storage",
        "planner-artifacts/temporal/plan.txt": "planner",
        "environment/mission.json": "environment",
        "debug/llm/response.json": "debug",
    }
    for relative, content in prior_files.items():
        path = tmp_path / "var" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        runtime_cli, "_utc_archive_timestamp", lambda: "20260819T123456.123456Z"
    )

    destination = runtime_cli._rollover_demo_artifacts(
        repo_root=tmp_path,
        lease=RuntimeLeaseStore(tmp_path / "var/storage/runtime"),
    )

    expected = tmp_path / "data/past_debug_rounds/20260819T123456.123456Z/var"
    assert destination == expected
    assert not (tmp_path / "var").exists()
    assert {
        str(path.relative_to(expected)): path.read_text(encoding="utf-8")
        for path in expected.rglob("*")
        if path.is_file()
    } == prior_files


def test_demo_artifact_rollover_is_noop_without_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime_cli, "_utc_archive_timestamp", lambda: "20260819T123456.123456Z"
    )

    destination = runtime_cli._rollover_demo_artifacts(
        repo_root=tmp_path,
        lease=RuntimeLeaseStore(tmp_path / "var/storage/runtime"),
    )

    assert destination is None
    assert not (tmp_path / "var").exists()
    assert not (tmp_path / "data").exists()


def test_demo_artifact_rollover_refuses_an_active_lease_without_moving_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = tmp_path / "var/debug/prior.json"
    prior.parent.mkdir(parents=True)
    prior.write_text("prior", encoding="utf-8")
    lease_root = tmp_path / "var/storage/runtime"
    active_owner = RuntimeLeaseStore(lease_root)
    active_owner.start(session_id="active-demo")
    monkeypatch.setattr(
        runtime_cli, "_utc_archive_timestamp", lambda: "20260819T123456.123456Z"
    )

    try:
        with pytest.raises(RuntimeError, match="another runtime session is active"):
            runtime_cli._rollover_demo_artifacts(
                repo_root=tmp_path,
                lease=RuntimeLeaseStore(lease_root),
            )
    finally:
        active_owner.stop()

    assert prior.read_text(encoding="utf-8") == "prior"
    assert not (tmp_path / "data").exists()


def test_cli_composes_and_runs_offline_through_injected_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []
    models: dict[str, object] = {}
    plan = _normalized_plan()
    scene = TransportEvent(
        schema_version=1,
        event_id="environment-data:mission:demo:1",
        mission_id="mission:demo",
        sequence=0,
        event_kind="environment_data",
        payload={"graph": {"mission_id": "mission:demo", "entities": []}},
    )
    environment_snapshot = MissionSnapshot(
        mission_id="mission:demo",
        version=1,
        created_at="2026-08-20T00:00:00+00:00",
        environment_data=scene.event_id,
        source_revisions={"environment_data": 0},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    belief = create_fake_entity_risk_snapshot("mission:demo")
    belief_reference = belief_artifact_reference(
        belief.mission_id, belief.content_sha256
    )
    snapshot = MissionSnapshot(
        mission_id="mission:demo",
        version=2,
        created_at="2026-08-20T00:00:01+00:00",
        environment_data=scene.event_id,
        bayesian_belief_snapshot=belief_reference,
        source_revisions={
            "environment_data": 0,
            "bayesian_belief_snapshot": belief.belief_revision,
        },
        source_health={
            "environment_data": "healthy",
            "bayesian_belief_snapshot": "healthy",
        },
        source_freshness={
            "environment_data": True,
            "bayesian_belief_snapshot": True,
        },
    )
    plan_snapshot = MissionSnapshot(
        mission_id="mission:demo",
        version=3,
        created_at="2026-08-20T00:00:02+00:00",
        plan_revision=plan.plan_revision,
        plan_reference="normalized-plan:mission:demo:3",
        environment_data=scene.event_id,
        bayesian_belief_snapshot=belief_reference,
        source_revisions={
            "environment_data": 0,
            "bayesian_belief_snapshot": belief.belief_revision,
            "plan": plan.plan_revision,
        },
        source_health={
            "environment_data": "healthy",
            "bayesian_belief_snapshot": "healthy",
            "plan": "healthy",
        },
        source_freshness={
            "environment_data": True,
            "bayesian_belief_snapshot": True,
            "plan": True,
        },
    )
    statechart = Statechart.from_normalized_plan(plan)
    initial_status = FSMStatus(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        statechart_revision=plan.plan_revision,
        active_state=statechart.entry_state,
        active_state_context=statechart.context_for(statechart.entry_state),
    )

    class FakeHyperWorkflow:
        def run(self, context: object, **kwargs: object) -> object:
            calls.append(("hyper-run", context, kwargs))
            handoff = CommandOutcome(
                1,
                "hyper-handoff:mission:demo:3:1",
                "planning-run:mission:demo:1",
                "mission:demo",
                "completed",
                {
                    "mission_id": "mission:demo",
                    "request_id": "hyper-handoff:mission:demo:3:1",
                    "outcome": "no_change",
                    "summary": "No immediate effect is required.",
                },
            )
            return SimpleNamespace(
                outcome=HyperWorkflowOutcome.EXECUTION_READY,
                normalized_plan=plan,
                statechart=statechart,
                statechart_reference="/tmp/accepted-statechart.json",
                initial_fsm_status=initial_status,
                handoff_outcome=handoff,
                todos=({"content": "Run MiniZinc", "status": "completed"},),
            )

    class FakeContextCoordination:
        def __init__(self, transport: FileTransport) -> None:
            self.transport = transport
            self.subscription = Subscription(
                "context-coordination", "mission:demo", "normalized-plans"
            )
            self.input_topic = "normalized-plans"
            transport.subscriptions += (self.subscription,)
            self.snapshots = iter((environment_snapshot, snapshot, plan_snapshot))

        def publish_source_fact(
            self,
            source: str,
            revision: int,
            *,
            reference: str,
        ) -> object:
            event = create_source_fact_event(
                "mission:demo",
                source,
                revision,
                event_id="source-fact:mission:demo:belief:1",
                sequence=self.transport.next_event_sequence(
                    "normalized-plans", "mission:demo"
                ),
                reference=reference,
            )
            return self.transport.publish_event("normalized-plans", event)

        def run_once(self, consumer: object) -> MissionSnapshot:
            delivery = consumer.receive()  # type: ignore[attr-defined]
            assert delivery is not None
            delivery.ack()
            calls.append("heartbeat-snapshot")
            return next(self.snapshots)

    class FakeRuntime:
        def __init__(self) -> None:
            self.transport = FileTransport(tmp_path / "transport")
            self.config = SimpleNamespace(agent_name="drone-1")

        def verify_llm_reachability(self) -> None:
            calls.append("verify")

        def create_chat_model(
            self,
            *,
            mission_id: str | None = None,
            debug_scope: str = "runtime",
        ) -> object:
            model = object()
            models[debug_scope] = model
            calls.append(("model", debug_scope, mission_id, model))
            return model

        def create_maneuver_control(self, adapter: object, **kwargs: object) -> object:
            calls.append(("maneuver", adapter, kwargs))

            class HeartbeatControl:
                def handle_agent_message(self, message: object) -> object:
                    calls.append(("maneuver-heartbeat", message))
                    return None

            return HeartbeatControl()

        def create_communication_port(self) -> object:
            calls.append("communication-port")

            class Port:
                def register(self, recipient: str, handler: object) -> None:
                    calls.append(("communication-register", recipient, handler))

            return Port()

        def create_bayesian_belief_service(self, **kwargs: object) -> object:
            calls.append(("belief-service", kwargs))
            return "belief-service"

        def create_context_coordination(self, **kwargs: object) -> object:
            calls.append(("context", kwargs))
            return FakeContextCoordination(self.transport)

        def create_fsm_runner(self, **kwargs: object) -> object:
            calls.append(("fsm", kwargs))
            return "fsm-runner"

        def create_hyper_workflow(self, **kwargs: object) -> object:
            calls.append(("hyper-workflow", kwargs))
            return FakeHyperWorkflow()

        def create_hyper_workflow_context(
            self,
            mission: MissionInput,
            selected_snapshot: MissionSnapshot,
            selected_scene: TransportEvent,
            **kwargs: object,
        ) -> object:
            calls.append(
                (
                    "hyper-context",
                    mission,
                    selected_snapshot,
                    selected_scene,
                    kwargs,
                )
            )
            return "hyper-workflow-context"

        def run_mission(self, mission: MissionInput, **kwargs: object) -> object:
            calls.append(("run", mission, kwargs))
            assert kwargs["environment_step"]() == "demo-evidence"  # type: ignore[operator]
            return SimpleNamespace(
                plan=kwargs["plan"],
                command=SimpleNamespace(
                    command_id="command-demo", maneuver_id="maneuver-demo"
                ),
                final_status=SimpleNamespace(active_state="state-1", status="active"),
            )

    class FakeEnvironment:
        last_output_path = None

        def heartbeat(self) -> object:
            calls.append("environment-heartbeat")
            source_fact = create_source_fact_event(
                "mission:demo",
                "environment_data",
                0,
                event_id="source-fact:mission:demo:scene:1",
                sequence=0,
                reference=scene.event_id,
            )
            runtime.transport.publish_event("normalized-plans", source_fact)
            return SimpleNamespace(environment_event=scene, source_fact=source_fact)

        def run_once(self) -> str:
            calls.append("environment")
            return "demo-evidence"

    runtime = FakeRuntime()
    maneuver_prompt = _role_prompt_files(tmp_path)
    monkeypatch.setattr(
        runtime_cli,
        "_create_runtime",
        lambda **kwargs: calls.append(("runtime", kwargs)) or runtime,
    )
    monkeypatch.setattr(
        runtime_cli,
        "_create_demo_environment",
        lambda selected, mission_id, **kwargs: (
            calls.append(("demo-environment", selected, mission_id, kwargs))
            or FakeEnvironment()
        ),
    )
    result = runtime_cli.main(
        [
            "--mission-file",
            str(_mission_file(tmp_path)),
            "--repo-root",
            str(tmp_path),
            "--config-path",
            "runtime.yaml",
            "--planner-artifacts",
            "var/planner-artifacts",
            "--demo-environment",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0 and captured.err == ""
    assert json.loads(captured.out) == {
        "mission_id": "mission:demo",
        "plan_revision": 3,
        "outcome": "execution_ready",
        "statechart_reference": "/tmp/accepted-statechart.json",
        "entry_state": statechart.entry_state,
        "state_count": len(statechart.states),
        "transition_count": len(statechart.transitions),
        "maneuver_completion": {
            "mission_id": "mission:demo",
            "request_id": "hyper-handoff:mission:demo:3:1",
            "outcome": "no_change",
            "summary": "No immediate effect is required.",
        },
        "environment_file": None,
    }
    assert calls[0] == (
        "runtime",
        {"repo_root": tmp_path, "config_path": Path("runtime.yaml")},
    )
    model_calls = [
        item for item in calls if isinstance(item, tuple) and item[0] == "model"
    ]
    assert [(item[1], item[2]) for item in model_calls] == [
        ("hyper-agent", "mission:demo"),
        ("maneuver-control", "mission:demo"),
    ]
    assert len({id(item[3]) for item in model_calls}) == 2
    hyper_call = next(
        item
        for item in calls
        if isinstance(item, tuple) and item[0] == "hyper-workflow"
    )
    assert hyper_call[1]["model"] is models["hyper-agent"]
    assert hyper_call[1]["system_prompt"] == "Temporary Hyper role prompt."
    hyper_context_call = next(
        item for item in calls if isinstance(item, tuple) and item[0] == "hyper-context"
    )
    assert hyper_context_call[2] is snapshot
    assert hyper_context_call[3] is scene
    assert hyper_context_call[4]["artifact_root"] == (
        tmp_path / "var/planner-artifacts"
    )
    assert hyper_context_call[4]["belief_snapshot"] == belief
    assert hyper_context_call[4]["fsm_runner"] == "fsm-runner"
    assert hyper_context_call[4]["belief_service"] == "belief-service"
    hyper_run = next(
        item for item in calls if isinstance(item, tuple) and item[0] == "hyper-run"
    )
    assert hyper_run[2] == {
        "thread_id": "planning-run:mission:demo:1",
        "recursion_limit": 120,
    }
    maneuver_call = next(
        item for item in calls if isinstance(item, tuple) and item[0] == "maneuver"
    )
    skill_catalog = maneuver_call[2]["skill_catalog"]
    assert skill_catalog.root == tmp_path / "conf/skills"
    assert maneuver_call[2]["backend_root"] == tmp_path
    assert maneuver_call[2]["model"] is models["maneuver-control"]
    assert maneuver_call[2]["system_prompt"] == (
        f"You are agent drone-1. {maneuver_prompt}"
    )
    registration = next(
        item
        for item in calls
        if isinstance(item, tuple) and item[0] == "communication-register"
    )
    assert registration[1] == "maneuver-control"
    assert "environment" not in calls
    assert calls.index("environment-heartbeat") < calls.index("heartbeat-snapshot")


def test_cli_failure_is_nonzero_actionable_and_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mission_path = _mission_file(tmp_path)

    def fail_runtime(**kwargs: object) -> object:
        _ = kwargs
        raise RuntimeError(
            "Survey the demo area without exposing this input. api_key=secret"
        )

    monkeypatch.setattr(runtime_cli, "_create_runtime", fail_runtime)
    result = runtime_cli.main(
        [
            "--mission-file",
            str(mission_path),
            "--demo-environment",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1 and captured.out == ""
    assert "runtime configuration" in captured.err
    assert "RuntimeError" in captured.err
    assert "Survey the demo area" not in captured.err
    assert "api_key" not in captured.err and "secret" not in captured.err


def test_cli_reports_system_prompt_loading_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = SimpleNamespace(
        transport=FileTransport(tmp_path / "transport"),
        verify_llm_reachability=lambda: None,
    )
    monkeypatch.setattr(runtime_cli, "_create_runtime", lambda **kwargs: runtime)

    result = runtime_cli.main(
        [
            "--mission-file",
            str(_mission_file(tmp_path)),
            "--repo-root",
            str(tmp_path),
            "--demo-environment",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1 and captured.out == ""
    assert "system prompt loading" in captured.err
    assert "ValueError" in captured.err
