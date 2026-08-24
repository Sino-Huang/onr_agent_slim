from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import onr.runtime.cli as runtime_cli
from onr.adapters.file_transport import FileTransport
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import PlannerChoice, PlannerPlan, PlanningOutcome
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


def _planner_plan() -> PlannerPlan:
    return PlannerPlan(
        mission_id="mission:demo",
        source_authority="demo-operator",
        plan_revision=3,
        mission_snapshot_id="mission:demo:snapshot:1",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        planner_native_plan_artifact_reference="/tmp/minizinc.plan",
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
    supervisor_path = prompt_root / "hyper-supervisor/SYSTEM.md"
    supervisor_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_path.write_text("Temporary Hyper supervisor prompt.", encoding="utf-8")
    return maneuver_prompt


def test_closed_loop_routes_workflow_and_supervisor_prompts_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = {
        "hyper-agent": "Planning workflow prompt.",
        "hyper-supervisor": "Supervisory heartbeat prompt.",
        "maneuver-control": "Maneuver prompt.",
    }
    workflow_prompts: list[str] = []
    supervisor_prompts: list[str] = []
    planning_snapshot = MissionSnapshot(
        "mission:demo",
        1,
        "2026-08-23T00:00:00+10:00",
        plan_revision=1,
        plan_reference="planner-plan.json",
        source_revisions={"environment_data": 1},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    planning_view = SimpleNamespace(
        environment_event=object(),
        environment_file=tmp_path / "environment.json",
    )
    closed_loop_result = object()

    class FakeTransport:
        def open_consumer(self, subscription: object) -> nullcontext[object]:
            _ = subscription
            return nullcontext(object())

    class FakeEnvironment:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs
            self.event_report: dict[str, object] = {}

        def heartbeat(self) -> object:
            return planning_view

    class PlanningContext:
        subscription = object()

        def drain_to_latest(self, consumer: object) -> MissionSnapshot:
            _ = consumer
            return planning_snapshot

    class ClosedLoopContext:
        def __init__(self, replan_workflow: object) -> None:
            self.replan_workflow = replan_workflow

        def run(self, active: object) -> object:
            _ = active
            replan = self.replan_workflow
            assert callable(replan)
            replan(object(), 2, planning_snapshot, planning_view)
            return closed_loop_result

        def handle_agent_message(self, message: object) -> None:
            _ = message

    class Communication:
        def register(self, role: str, handler: object) -> None:
            _ = role, handler

    belief_service = SimpleNamespace(load_current_snapshot=lambda: object())
    supervisor = SimpleNamespace(handle_agent_message=lambda message: message)

    class FakeRuntime:
        transport = FakeTransport()
        config = SimpleNamespace(
            agent_name="test-agent",
            heartbeats=SimpleNamespace(maneuver_seconds=5, hyper_seconds=10),
            transport=SimpleNamespace(root=tmp_path / "transport"),
        )

        def create_context_coordination(self, **kwargs: object) -> object:
            if "environment" not in kwargs:
                return PlanningContext()
            return ClosedLoopContext(kwargs["replan_workflow"])

        def create_bayesian_belief_service(self, **kwargs: object) -> object:
            _ = kwargs
            return belief_service

        def create_chat_model(self, **kwargs: object) -> object:
            return kwargs["debug_scope"]

        def create_hyper_supervisor(self, **kwargs: object) -> object:
            supervisor_prompts.append(str(kwargs["system_prompt"]))
            return supervisor

        def create_communication_port(self, **kwargs: object) -> Communication:
            _ = kwargs
            return Communication()

        def create_fsm_runner(self, **kwargs: object) -> object:
            _ = kwargs
            return object()

        def create_maneuver_control(self, *args: object, **kwargs: object) -> object:
            _ = args, kwargs
            return object()

    def run_revision(*args: object, **kwargs: object) -> object:
        _ = args
        workflow_prompts.append(str(kwargs["system_prompt"]))
        return object()

    monkeypatch.setattr(runtime_cli, "FileTransport", FakeTransport)
    monkeypatch.setattr(runtime_cli, "FakeEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        runtime_cli,
        "load_system_prompt",
        lambda prompt_root, role: prompts[role],
    )
    monkeypatch.setattr(runtime_cli, "seed_event_risk_beliefs", lambda *args: object())
    monkeypatch.setattr(runtime_cli, "_run_hyper_revision", run_revision)

    result = runtime_cli.run_closed_loop_demo(
        FakeRuntime(),  # type: ignore[arg-type]
        MissionInput("mission:demo", "Patrol the area.", "operator"),
        repo_root=tmp_path,
        planner_artifacts=tmp_path / "planner-artifacts",
        recursion_limit=120,
        simulation_limit_seconds=30,
    )

    assert result is closed_loop_result
    assert workflow_prompts == [prompts["hyper-agent"], prompts["hyper-agent"]]
    assert supervisor_prompts == [prompts["hyper-supervisor"]]


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
        "Please patrol the environment and confirm that all the events mentioned "
        "in the event report are accounted for."
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


@pytest.mark.parametrize("planner_override", [None, "var/override-artifacts"])
def test_cli_composes_and_runs_closed_loop_through_injected_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    planner_override: str | None,
) -> None:
    from onr.application.context_coordination import ClosedLoopRunResult
    from onr.contracts.hyper_agent import HyperHeartbeatDecision

    calls: list[object] = []

    class FakeRuntime:
        def __init__(self) -> None:
            self.transport = FileTransport(tmp_path / "transport")
            self.config = SimpleNamespace(
                storage=SimpleNamespace(
                    planner_artifacts=tmp_path / "configured-planner-artifacts"
                )
            )

        def verify_llm_reachability(self) -> None:
            calls.append("verify")

        def runtime_session(self):  # type: ignore[no-untyped-def]
            return nullcontext()

    runtime = FakeRuntime()
    _role_prompt_files(tmp_path)
    expected = ClosedLoopRunResult(
        mission_id="mission:demo",
        simulated_duration_seconds=15.0,
        tick_count=30,
        maneuver_heartbeat_count=4,
        hyper_heartbeat_count=1,
        physical_actions=("navigate",),
        feedback_count=2,
        perception_count=2,
        belief_revisions=(20, 21),
        hyper_outcomes=(
            HyperHeartbeatDecision(
                "mission:demo",
                1,
                "no_change",
                "The active plan remains executable.",
                ("periodic:10",),
                (),
            ),
        ),
        plan_revisions=(1,),
        final_fsm_state="complete",
        terminal=True,
    )
    monkeypatch.setattr(
        runtime_cli,
        "_create_runtime",
        lambda **kwargs: calls.append(("runtime", kwargs)) or runtime,
    )
    monkeypatch.setattr(
        runtime_cli,
        "run_closed_loop_demo",
        lambda selected_runtime, mission, **kwargs: (
            calls.append(("closed-loop", selected_runtime, mission, kwargs)) or expected
        ),
    )

    arguments = [
        "--mission-file",
        str(_mission_file(tmp_path)),
        "--repo-root",
        str(tmp_path),
        "--config-path",
        "runtime.yaml",
        "--simulation-limit-seconds",
        "30",
        "--demo-environment",
    ]
    if planner_override is not None:
        arguments.extend(("--planner-artifacts", planner_override))

    result = runtime_cli.main(arguments)

    captured = capsys.readouterr()
    assert result == 0 and captured.err == ""
    assert json.loads(captured.out) == expected.to_dict()
    assert calls[0] == (
        "runtime",
        {"repo_root": tmp_path, "config_path": Path("runtime.yaml")},
    )
    assert calls[1] == "verify"
    closed_loop = calls[2]
    assert closed_loop[0] == "closed-loop"
    assert closed_loop[1] is runtime
    assert closed_loop[2] == MissionInput(
        "mission:demo",
        "Survey the demo area without exposing this input.",
        "demo-operator",
    )
    expected_artifacts = (
        tmp_path / planner_override
        if planner_override is not None
        else tmp_path / "configured-planner-artifacts"
    )
    assert closed_loop[3]["planner_artifacts"] == expected_artifacts.resolve()
    assert closed_loop[3]["recursion_limit"] == 120
    assert closed_loop[3]["simulation_limit_seconds"] == 30.0


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
        config=SimpleNamespace(
            storage=SimpleNamespace(
                planner_artifacts=tmp_path / "configured-planner-artifacts"
            )
        ),
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
