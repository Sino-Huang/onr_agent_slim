from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

from harness.fake_environment import FakeEnvironment
from onr.adapters.file_transport import FileTransport
from onr.adapters.operational_log import FileOperationalLog
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.hyper_agent import FrozenMissionSpec, MissionInput
from onr.contracts.maneuver_control import (
    ManeuverControlDecision,
    ManeuverCommand,
    PhysicalAction,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    TemporalManeuver,
    ScheduledManeuver,
)
from onr.contracts.transport import TransportEvent
from onr.runtime import RuntimeComposition


class FixedInterpreter:
    def interpret(self, mission_input: MissionInput) -> MissionSpec:
        return MissionSpec(
            mission_id=mission_input.mission_id,
            objective="survey area 7",
            planner_choice=PlannerChoice("temporal", "minizinc"),
            maneuvers=(
                TemporalManeuver(
                    maneuver_id="survey",
                    intent=ManeuverIntent(
                        PhysicalAction.NAVIGATE,
                        (ManeuverParameter("waypoint", "area-7"),),
                    ),
                    dependencies=(),
                    duration=1,
                ),
            ),
            horizon=2,
            source_authority=mission_input.source_authority,
        )


class FixedPlanner:
    def plan(
        self, spec: MissionSpec, revision: int, snapshot_id: str
    ) -> NormalizedPlan:
        return NormalizedPlan(
            mission_spec=spec,
            plan_revision=revision,
            mission_snapshot_id=snapshot_id,
            planner_choice=spec.planner_choice,
            outcome=PlanningOutcome.SOLVED,
            maneuvers=(
                ScheduledManeuver(
                    maneuver_id="survey",
                    intent=spec.maneuvers[0].intent,
                    dependencies=(),
                    start=0,
                    duration=1,
                ),
            ),
        )


class RecordingAdapter:
    def __init__(self) -> None:
        self.commands: list[ManeuverCommand] = []

    def submit(self, command: ManeuverCommand) -> object:
        self.commands.append(command)
        return {"command_id": command.command_id}


class FixedDecisionProvider:
    def __init__(self, intent: ManeuverIntent) -> None:
        self.intent = intent

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: object = None,
    ) -> ManeuverControlDecision:
        _ = snapshot, overlay
        return ManeuverControlDecision(
            decision_id="runtime-decision",
            mission_id=status.mission_id,
            plan_revision=status.plan_revision,
            maneuver_id="survey",
            physical_intent=self.intent,
        )


def _runtime_config(tmp_path: Path, planner_path: Path) -> Path:
    config = tmp_path / "runtime.yaml"
    config.write_text(
        f"""llm:
  provider: test
  base_url: http://127.0.0.1:8000/v1
  model: test-model
  api_key: test-key
  temperature: 0
planners:
  temporal:
    entrypoint: {planner_path}
    timeout_seconds: 1
  symbolic:
    entrypoint: {planner_path}
    timeout_seconds: 1
heartbeats:
  hyper_seconds: 1
  maneuver_seconds: 1
transport:
  backend: file
  root: transport
storage:
  root: storage
services:
  hyper_agent: hyper-agent
  maneuver_control: maneuver-control
  context_coordination: context-coordination
  fsm_runner: fsm-runner
  planner: planner
""",
        encoding="utf-8",
    )
    return config


def test_file_backed_runtime_composes_one_physical_maneuver(tmp_path: Path) -> None:
    """Exercise the public MissionInput-to-feedback runtime seam."""

    # The fixture is deliberately self-contained so transport and FSM storage
    # cannot observe or reuse state from another acceptance test.
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )

    mission_input = MissionInput(
        mission_id="mission-runtime",
        mission_text="Survey area 7.",
        source_authority="mission-control",
    )
    hyper_agent = runtime.create_hyper_agent(
        FixedInterpreter(),
        FixedPlanner(),
        mission_id=mission_input.mission_id,
    )
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "t-runtime",
    )
    fsm_runner = runtime.create_fsm_runner(
        mission_id=mission_input.mission_id,
        clock=lambda: 0,
    )
    intent = FixedInterpreter().interpret(mission_input).maneuvers[0].intent
    adapter = RecordingAdapter()
    maneuver_control = runtime.create_maneuver_control(
        adapter,
        FixedDecisionProvider(intent),
    )
    environment = FakeEnvironment(
        cast(FileTransport, runtime.transport), mission_input.mission_id
    )
    environment_steps: list[bool] = []

    def environment_step() -> object:
        environment_steps.append(True)
        return environment.run_once()

    result = runtime.run_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        fsm_runner=fsm_runner,
        maneuver_control=maneuver_control,
        environment_step=environment_step,
    )

    assert isinstance(result.authority, FrozenMissionSpec)
    assert result.authority.mission_input == mission_input
    assert result.authority.canonical_document == result.authority.mission_spec.to_canonical_json()
    assert result.authority.content_hash == hashlib.sha256(
        result.authority.canonical_document.encode("utf-8")
    ).hexdigest()
    assert result.plan.outcome is PlanningOutcome.SOLVED
    assert len(result.plan.maneuvers) == 1
    assert isinstance(result.context_snapshot, MissionSnapshot)
    assert result.context_snapshot.plan_revision == result.plan.plan_revision
    assert result.context_snapshot.operational_scene_graph == result.scene_graph.event_id
    assert result.scene_graph.event_kind == "operational_scene_graph"
    assert result.feedback.event_kind == "maneuver-feedback"
    assert result.feedback.payload["command_id"] == result.command.command_id
    assert result.feedback.payload["correlation_id"] == result.command.correlation_id
    assert result.command.mission_id == mission_input.mission_id
    assert result.command.plan_revision == result.plan.plan_revision
    assert result.command.maneuver_id == result.plan.maneuvers[0].maneuver_id
    assert result.command.correlation_id == result.command.command_id
    assert result.status_before_feedback.active_state == "state-0"
    assert result.status_before_feedback.last_applied_event is None
    assert result.final_status.active_state == "state-1"
    assert result.final_status.last_applied_event == "advance:survey"
    assert environment_steps == [True]
    command_files = (
        tmp_path
        / "transport"
        / "commands"
        / "maneuver-adapter"
        / mission_input.mission_id
    ).glob("*.json")
    assert len(list(command_files)) == 1
    assert isinstance(result.scene_graph, TransportEvent)

    operational_log_root = tmp_path / "storage" / "operational-log"
    records = FileOperationalLog(operational_log_root).replay(mission_input.mission_id)
    assert records
    assert [record.sequence for record in records] == list(range(1, len(records) + 1))
    assert {
        "agent",
        "heartbeat",
        "planning",
        "solver",
        "control",
        "fsm",
        "transport",
        "environment",
    }.issubset({record.event_kind for record in records})
    raw_log = "\n".join(
        (tmp_path / "storage" / "operational-log" / mission_input.mission_id / "events" / f"{record.sequence:020d}.json").read_text(encoding="utf-8")
        for record in records
    )
    assert mission_input.mission_text not in raw_log
    assert (tmp_path / "storage" / "operational-log" / mission_input.mission_id / "events").is_dir()
    assert not (tmp_path / "storage" / "mission-memory" / mission_input.mission_id).exists()
