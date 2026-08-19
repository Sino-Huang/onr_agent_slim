from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from harness.fake_environment import FakeEnvironment
from onr.adapters.file_transport import FileTransport
from onr.adapters.operational_log import FileOperationalLog
from onr.application.hyper_agent import PlanningHeartbeatOutcome
from onr.contracts import PlanningIntent
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.bayesian_belief import BeliefKey
from onr.contracts.fsm import FSMStatus
from onr.contracts.hyper_agent import FrozenMissionSpec, MissionInput
from onr.contracts.maneuver_control import (
    ManeuverControlDecision,
    ManeuverCommand,
    PhysicalAction,
)
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    NormalizedPlan,
    PlanProvenance,
    PlannerChoice,
    PlanningOutcome,
    ScheduledManeuver,
    TemporalManeuver,
    VerifiableReference,
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


class FixedPlanningIntentInterpreter:
    def interpret(self, mission_input: MissionInput) -> PlanningIntent:
        return PlanningIntent(
            mission_id=mission_input.mission_id,
            source_authority=mission_input.source_authority,
            objective="survey area 7",
            rationale="Timed movement and observation require MiniZinc.",
            planner_choice=PlannerChoice("temporal", "minizinc"),
            mission_input_sha256=hashlib.sha256(
                mission_input.to_canonical_json().encode("utf-8")
            ).hexdigest(),
            details={"observation_objective": "maximize FoV coverage"},
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


class FixedSummaryModel:
    def __init__(self) -> None:
        self.invocation_kwargs: list[dict[str, object]] = []

    def invoke(self, prompt: str, **kwargs: object) -> str:
        assert "NEW LOG RECORDS" in prompt
        self.invocation_kwargs.append(kwargs)
        return "Mission runtime completed one maneuver."


def _runtime_config(tmp_path: Path, planner_path: Path) -> Path:
    config = tmp_path / "runtime.yaml"
    config.write_text(
        f"""agent_name: test-agent
debug: false
llm:
  provider: test
  base_url: http://127.0.0.1:14398/v1
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
  summary_seconds: 30
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
agents:
  hyper_agent:
    output_structure_retry:
      max_retries: 2
  maneuver_control:
    output_structure_retry:
      max_retries: 1
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
    belief_service = runtime.create_bayesian_belief_service(
        mission_id=mission_input.mission_id,
        keys=tuple(BeliefKey(f"ship-{index}", "collision") for index in range(1, 4)),
        particle_count=128,
        seed=2,
        clock=lambda: "2026-08-19T12:00:00+00:00",
    )
    environment_steps: list[bool] = []

    def environment_step() -> object:
        environment_steps.append(True)
        return environment.run_once()

    summary_model = FixedSummaryModel()
    result = runtime.run_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        fsm_runner=fsm_runner,
        maneuver_control=maneuver_control,
        environment_step=environment_step,
        bayesian_belief_service=belief_service,
        model=summary_model,
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
    assert result.belief_snapshot is not None
    assert result.belief_context_snapshot == result.context_snapshot
    assert result.context_snapshot.source_hashes["bayesian_belief_snapshot"] == (
        result.belief_snapshot.content_sha256
    )
    assert result.belief_heartbeat is not None
    assert result.belief_heartbeat.belief_snapshot == result.belief_snapshot
    assert result.belief_heartbeat.plan_revision == result.plan.plan_revision + 1
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
    assert summary_model.invocation_kwargs == [
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    ]
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
    assert (
        tmp_path
        / "storage"
        / "summaries"
        / mission_input.mission_id
        / "00000000000000000001.json"
    ).is_file()

    with pytest.raises(ValueError, match="particle count"):
        runtime.create_bayesian_belief_service(
            mission_id=mission_input.mission_id,
            keys=tuple(
                BeliefKey(f"ship-{index}", "collision") for index in range(1, 4)
            ),
            particle_count=129,
        )
    with pytest.raises(ValueError, match="seed cannot be supplied"):
        runtime.create_bayesian_belief_service(
            mission_id=mission_input.mission_id,
            seed=2,
        )



def test_planning_mission_uses_heartbeat_scene_without_a_mission_spec(
    tmp_path: Path,
) -> None:
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )
    mission_input = MissionInput(
        mission_id="planning-runtime",
        mission_text="Observe risky ships with maximum field of view coverage.",
        source_authority="mission-control",
    )
    hyper_agent = runtime.create_hyper_agent(
        FixedInterpreter(),
        FixedPlanner(),
        planning_intent_interpreter=FixedPlanningIntentInterpreter(),
        mission_id=mission_input.mission_id,
    )
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "planning-runtime",
    )
    environment = FakeEnvironment(
        cast(FileTransport, runtime.transport), mission_input.mission_id
    )
    model_path = tmp_path / "attempt" / "model.mzn"
    data_path = tmp_path / "attempt" / "data.dzn"
    model_path.parent.mkdir()
    model_path.write_text("solve satisfy;\n", encoding="utf-8")
    data_path.write_text("horizon = 1;\n", encoding="utf-8")

    def generate(
        choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        scene_graph: TransportEvent,
    ) -> PlannerGenerationAttempt:
        assert snapshot.operational_scene_graph == scene_graph.event_id
        graph = scene_graph.payload["graph"]
        assert isinstance(graph, Mapping) and graph["entities"]
        references = {
            "model.mzn": str(model_path),
            "data.dzn": str(data_path),
        }
        return PlannerGenerationAttempt(
            attempt_id="attempt-1",
            decision_id=choice.decision_id,
            mission_id=choice.mission_id,
            mission_input_sha256=choice.mission_input_sha256,
            planning_intent_sha256=choice.planning_intent_sha256,
            planner_choice=choice.planner_choice,
            rationale=choice.rationale,
            mission_snapshot_id=f"{mission_input.mission_id}:snapshot:1",
            translator_id="hyper-minizinc",
            translator_version="1.0.0",
            outcome="accepted",
            asset_references=references,
            asset_sha256={
                name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                for name, path in references.items()
            },
        )

    result = runtime.run_planning_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        environment_heartbeat=environment.heartbeat,
        generate=generate,
        model=FixedSummaryModel(),
    )

    assert result.attempt is not None
    assert result.context_snapshot is not None
    assert result.scene_graph is not None
    assert result.attempt.outcome == "accepted"
    assert result.context_snapshot.operational_scene_graph == result.scene_graph.event_id
    assert hyper_agent.authority(mission_input.mission_id) is None
    assert runtime.transport.latest_event(
        "mission-specifications", mission_input.mission_id
    ) is None
    evidence = runtime.transport.latest_event(
        "planning-evidence", mission_input.mission_id
    )
    assert evidence is not None
    assert evidence.event_kind == "planner-generation-attempt"


def test_planning_mission_reports_missing_heartbeat_scene_without_generation(
    tmp_path: Path,
) -> None:
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )
    mission_input = MissionInput(
        mission_id="missing-scene-runtime",
        mission_text="Observe risky ships.",
        source_authority="mission-control",
    )
    hyper_agent = runtime.create_hyper_agent(
        FixedInterpreter(),
        FixedPlanner(),
        planning_intent_interpreter=FixedPlanningIntentInterpreter(),
        mission_id=mission_input.mission_id,
    )
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "missing-scene-runtime",
    )
    calls: list[object] = []

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start without scene evidence")

    result = runtime.run_planning_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        environment_heartbeat=lambda: None,
        generate=generate,
        model=FixedSummaryModel(),
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE
    assert result.planner_choice is None
    assert result.attempt is None
    assert result.context_snapshot is None
    assert result.scene_graph is None


def test_planning_mission_reports_stale_snapshot_scene_without_generation(
    tmp_path: Path,
) -> None:
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )
    mission_input = MissionInput(
        mission_id="stale-scene-runtime",
        mission_text="Observe risky ships.",
        source_authority="mission-control",
    )
    hyper_agent = runtime.create_hyper_agent(
        FixedInterpreter(),
        FixedPlanner(),
        planning_intent_interpreter=FixedPlanningIntentInterpreter(),
        mission_id=mission_input.mission_id,
    )
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "stale-scene-runtime",
    )
    transport = cast(FileTransport, runtime.transport)
    scene = TransportEvent(
        schema_version=1,
        event_id="stale-scene",
        mission_id=mission_input.mission_id,
        sequence=0,
        event_kind="operational_scene_graph",
        payload={"graph": {"entities": []}},
    )

    def heartbeat() -> None:
        transport.publish_event("operational-scene-graph", scene)
        context_coordination.publish_source_fact(
            "operational_scene_graph",
            1,
            reference=scene.event_id,
            fresh=False,
        )

    calls: list[object] = []

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start with stale scene evidence")

    result = runtime.run_planning_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        environment_heartbeat=heartbeat,
        generate=generate,
        model=FixedSummaryModel(),
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE
    assert result.context_snapshot is not None
    assert result.scene_graph == scene
    assert result.planner_choice is None
    assert result.attempt is None
    assert calls == []


def test_planning_mission_reports_unreferenced_scene_without_generation(
    tmp_path: Path,
) -> None:
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )
    mission_input = MissionInput(
        mission_id="unreferenced-scene-runtime",
        mission_text="Observe risky ships.",
        source_authority="mission-control",
    )
    hyper_agent = runtime.create_hyper_agent(
        FixedInterpreter(),
        FixedPlanner(),
        planning_intent_interpreter=FixedPlanningIntentInterpreter(),
        mission_id=mission_input.mission_id,
    )
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "unreferenced-scene-runtime",
    )
    transport = cast(FileTransport, runtime.transport)
    scene = TransportEvent(
        schema_version=1,
        event_id="unreferenced-scene",
        mission_id=mission_input.mission_id,
        sequence=0,
        event_kind="operational_scene_graph",
        payload={"graph": {"entities": []}},
    )
    calls: list[object] = []

    def heartbeat() -> None:
        transport.publish_event("operational-scene-graph", scene)

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start without a snapshot reference")

    result = runtime.run_planning_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        environment_heartbeat=heartbeat,
        generate=generate,
        model=FixedSummaryModel(),
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE
    assert result.context_snapshot is None
    assert result.scene_graph == scene
    assert result.planner_choice is None
    assert result.attempt is None
    assert calls == []


def test_provenance_only_plan_completes_physical_mission_run(tmp_path: Path) -> None:
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )
    mission_input = MissionInput(
        mission_id="mission-provenance",
        mission_text="Survey area 7.",
        source_authority="mission-control",
    )
    intent = ManeuverIntent(
        PhysicalAction.NAVIGATE,
        (ManeuverParameter("waypoint", "area-7"),),
    )
    plan = NormalizedPlan(
        mission_spec=None,
        plan_revision=1,
        mission_snapshot_id="mission-provenance:snapshot:1",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(ScheduledManeuver("survey", intent, (), 0, 1),),
        provenance=PlanProvenance(
            mission_id=mission_input.mission_id,
            source_authority=mission_input.source_authority,
            mission_intent=VerifiableReference("mission-input:1", "1" * 64),
            planning_decision=VerifiableReference("planner-choice:1", "2" * 64),
            operational_scene_graph=VerifiableReference("scene:1", "3" * 64),
            generated_assets={
                "model.mzn": VerifiableReference("model.mzn", "4" * 64),
            },
            solver_evidence={
                "stdout": VerifiableReference("solver.stdout", "5" * 64),
            },
        ),
    )
    context = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "t-runtime",
    )
    fsm = runtime.create_fsm_runner(
        mission_id=mission_input.mission_id,
        clock=lambda: 0,
    )
    adapter = RecordingAdapter()
    control = runtime.create_maneuver_control(
        adapter,
        FixedDecisionProvider(intent),
    )
    environment = FakeEnvironment(
        cast(FileTransport, runtime.transport),
        mission_input.mission_id,
    )

    result = runtime.run_provenance_mission(
        mission_input,
        plan=plan,
        context_coordination=context,
        fsm_runner=fsm,
        maneuver_control=control,
        environment_step=environment.run_once,
        model=FixedSummaryModel(),
    )

    assert result.authority is None
    assert result.plan is plan
    assert result.command.maneuver_id == "survey"
    assert result.feedback.maneuver_id == "survey"
    assert result.status_before_feedback.active_state == "state-0"
    assert result.final_status.active_state == "state-1"
    planning_record = next(
        record
        for record in FileOperationalLog(
            tmp_path / "storage" / "operational-log"
        ).replay(mission_input.mission_id)
        if record.event_kind == "planning"
    )
    assert planning_record.details["planning_decision_reference"] == "planner-choice:1"
    assert planning_record.details["scene_graph_reference"] == "scene:1"
    assert planning_record.details["generated_assets"] == "model.mzn"
    assert planning_record.details["solver_evidence"] == "stdout"
