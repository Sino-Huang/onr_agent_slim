from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from harness.fake_environment import FakeEnvironment
from onr.adapters.file_transport import FileTransport
from onr.application.hyper_agent import PlanningHeartbeatOutcome
from onr.contracts import PlanningIntent
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.human_decision import (
    HumanDecision,
    HumanDecisionAction,
    HumanDecisionCategory,
    HumanDecisionDisposition,
    RunCheckpoint,
)
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    PhysicalAction,
)
from onr.contracts.planner_translation import (
    PlanningTranslationOutcome,
    PlanningTranslationResult,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    ScheduledManeuver,
)
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
)
from onr.contracts.transport import TransportEvent
from onr.runtime import PlanningMissionRunResult, RuntimeComposition


class FixedPlanningIntentInterpreter:
    def interpret(self, mission_input: MissionInput) -> PlanningIntent:
        return PlanningIntent(
            mission_id=mission_input.mission_id,
            source_authority=mission_input.source_authority,
            objective="survey area 7",
            rationale="Timed movement and observation require MiniZinc.",
            planner_choice=PlannerChoice("temporal", "minizinc"),
            details={"observation_objective": "maximize FoV coverage"},
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
        self.overlays: list[InvocationOverlay | None] = []

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
    ) -> ManeuverControlDecision:
        _ = snapshot
        self.overlays.append(overlay)
        return ManeuverControlDecision(
            decision_id="runtime-decision",
            mission_id=status.mission_id,
            plan_revision=status.plan_revision,
            maneuver_id="survey",
            physical_intent=self.intent,
        )


class RecordingTranslator:
    def __init__(self, result: PlanningTranslationResult) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def plan(
        self,
        mission_input: MissionInput,
        planner_choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
        asset_generator: object,
        *,
        plan_revision: int,
    ) -> PlanningTranslationResult:
        self.calls.append(
            (
                mission_input,
                planner_choice,
                snapshot,
                environment_event,
                asset_generator,
                plan_revision,
            )
        )
        return self.result


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


def _terminal_planning_dependencies(
    runtime: RuntimeComposition,
) -> dict[str, Any]:
    return {
        "translator": object(),
        "asset_generator": object(),
        "human_decision_coordinator": runtime.create_human_decision_coordinator(),
        "fsm_runner": object(),
        "maneuver_control": object(),
        "environment_step": lambda: None,
    }


def test_planning_mission_uses_heartbeat_environment_data_without_a_mission_spec(
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
        FixedPlanningIntentInterpreter(),
        mission_id=mission_input.mission_id,
    )
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "planning-runtime",
    )
    environment = FakeEnvironment(
        cast(FileTransport, runtime.transport), mission_input.mission_id
    )
    intent = ManeuverIntent(
        PhysicalAction.NAVIGATE,
        (ManeuverParameter("waypoint", "windmill-area"),),
    )
    plan = NormalizedPlan(
        mission_id=mission_input.mission_id,
        source_authority=mission_input.source_authority,
        plan_revision=1,
        mission_snapshot_id=f"{mission_input.mission_id}:snapshot:1",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(ScheduledManeuver("survey", intent, (), 0, 1),),
    )
    asset_generator = object()
    human_decisions = runtime.create_human_decision_coordinator()
    fsm_runner = runtime.create_fsm_runner(
        mission_id=mission_input.mission_id,
    )
    maneuver_control = runtime.create_maneuver_control(
        RecordingAdapter(),
        FixedDecisionProvider(intent),
    )

    model_path = tmp_path / "attempt" / "model.mzn"
    data_path = tmp_path / "attempt" / "data.dzn"
    model_path.parent.mkdir()
    model_path.write_text("solve satisfy;\n", encoding="utf-8")
    data_path.write_text("horizon = 1;\n", encoding="utf-8")
    references = {
        "model.mzn": str(model_path),
        "data.dzn": str(data_path),
    }
    choice_record = PlannerChoiceRecord.from_planning_intent(
        FixedPlanningIntentInterpreter().interpret(mission_input)
    )
    correction_attempt = PlannerGenerationAttempt(
        attempt_id=f"{choice_record.decision_id}:generation:1",
        decision_id=choice_record.decision_id,
        mission_id=choice_record.mission_id,
        planner_choice=choice_record.planner_choice,
        rationale=choice_record.rationale,
        mission_snapshot_id=f"{mission_input.mission_id}:snapshot:1",
        translator_id="hyper-minizinc",
        translator_version="1.0.0",
        outcome="accepted",
        asset_references=references,
    )
    translator = RecordingTranslator(
        PlanningTranslationResult(
            PlanningTranslationOutcome.VERIFIED,
            1,
            (correction_attempt,),
            normalized_plan=plan,
        )
    )

    def generate(
        choice: PlannerChoiceRecord,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent,
    ) -> PlannerGenerationAttempt:
        assert snapshot.environment_data == environment_event.event_id
        graph = environment_event.payload["scene_graph"]
        assert isinstance(graph, Mapping) and graph["entities"]
        environment_data = cast(
            Mapping[str, object], environment_event.to_dict()["payload"]
        )
        static_info = cast(list[Mapping[str, object]], environment_data["static_info"])
        assert len(static_info) == 253
        assert static_info[0]["position"] == [212.0, 192.9, -250.0]
        assert static_info[-1]["position"] == [1160.3, -1151.1, -250.0]
        return PlannerGenerationAttempt(
            attempt_id="attempt-1",
            decision_id=choice.decision_id,
            mission_id=choice.mission_id,
            planner_choice=choice.planner_choice,
            rationale=choice.rationale,
            mission_snapshot_id=f"{mission_input.mission_id}:snapshot:1",
            translator_id="hyper-minizinc",
            translator_version="1.0.0",
            outcome="accepted",
            asset_references=references,
        )

    with pytest.raises(
        RuntimeError, match="translation-driven NormalizedPlan execution is retired"
    ):
        runtime.run_planning_mission(
            mission_input,
            hyper_agent=hyper_agent,
            context_coordination=context_coordination,
            environment_heartbeat=environment.heartbeat,
            generate=generate,
            translator=translator,
            asset_generator=asset_generator,
            human_decision_coordinator=human_decisions,
            fsm_runner=fsm_runner,
            maneuver_control=maneuver_control,
            environment_step=environment.run_once,
            model=FixedSummaryModel(),
        )


def test_planning_mission_reports_missing_environment_data_without_generation(
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
        FixedPlanningIntentInterpreter(),
        mission_id=mission_input.mission_id,
    )
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "missing-scene-runtime",
    )
    calls: list[object] = []

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start without environment data")

    dependencies = _terminal_planning_dependencies(runtime)
    human_decisions = runtime.create_human_decision_coordinator()
    dependencies["human_decision_coordinator"] = human_decisions
    result = runtime.run_planning_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        environment_heartbeat=lambda: None,
        generate=generate,
        model=FixedSummaryModel(),
        **dependencies,
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA
    assert result.human_decision_request is not None
    assert (
        str(result.human_decision_request.category) == "insufficient_environment_data"
    )
    assert result.execution is None
    assert result.planner_choice is None
    assert result.attempt is None
    assert result.context_snapshot is None
    assert result.environment_event is None

    resumed_from: list[RunCheckpoint] = []

    def resume(checkpoint: RunCheckpoint):
        resumed_from.append(checkpoint)
        return result

    request = result.human_decision_request
    resolution = runtime.resolve_planning_mission(
        HumanDecision(
            decision_id="wait-for-scene",
            request_id=request.request_id,
            mission_id=request.mission_id,
            mission_run_id=request.mission_run_id,
            action=HumanDecisionAction.WAIT_FOR_ENVIRONMENT_DATA,
        ),
        human_decision_coordinator=human_decisions,
        resume=resume,
    )

    assert resolution.resolution.disposition is HumanDecisionDisposition.RESUME
    assert resolution.resumed_run is result
    assert len(resumed_from) == 1
    assert resumed_from[0].checkpoint_id == request.checkpoint_id

    repeated = runtime.resolve_planning_mission(
        resolution.resolution.decision,
        human_decision_coordinator=human_decisions,
        resume=resume,
    )

    assert repeated.resolution == resolution.resolution
    assert repeated.resumed_run is None
    assert len(resumed_from) == 1


def test_end_planning_mission_decision_does_not_resume(tmp_path: Path) -> None:
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )
    coordinator = runtime.create_human_decision_coordinator()
    checkpoint = RunCheckpoint(
        checkpoint_id="checkpoint:end",
        mission_id="mission-end",
        mission_run_id="run:end",
        continuation="retry-planner",
    )
    request = coordinator.pause(
        HumanDecisionCategory.TIMEOUT,
        checkpoint,
        correlation_id="planning:mission-end",
        evidence_references=("solver.stdout",),
    )
    resume_calls: list[RunCheckpoint] = []

    def resume(selected: RunCheckpoint):
        resume_calls.append(selected)
        raise AssertionError("end decision must not resume the Mission Run")

    resolution = runtime.resolve_planning_mission(
        HumanDecision(
            decision_id="end-run",
            request_id=request.request_id,
            mission_id=request.mission_id,
            mission_run_id=request.mission_run_id,
            action=HumanDecisionAction.END_MISSION_RUN,
        ),
        human_decision_coordinator=coordinator,
        resume=resume,
    )

    assert resolution.resolution.disposition is HumanDecisionDisposition.END
    assert resolution.resumed_run is None
    assert resume_calls == []


def test_failed_planning_resume_can_be_retried(tmp_path: Path) -> None:
    planner_path = tmp_path / "planner"
    planner_path.write_text("#!/bin/sh\n", encoding="utf-8")
    planner_path.chmod(0o755)
    runtime = RuntimeComposition.create(
        repo_root=tmp_path,
        config_path=_runtime_config(tmp_path, planner_path),
    )
    coordinator = runtime.create_human_decision_coordinator()
    checkpoint = RunCheckpoint(
        checkpoint_id="checkpoint:retry",
        mission_id="mission-retry",
        mission_run_id="run:retry",
        continuation="retry-planner",
    )
    request = coordinator.pause(
        HumanDecisionCategory.TIMEOUT,
        checkpoint,
        correlation_id="planning:mission-retry",
        evidence_references=("solver.stdout",),
    )
    decision = HumanDecision(
        decision_id="retry-run",
        request_id=request.request_id,
        mission_id=request.mission_id,
        mission_run_id=request.mission_run_id,
        action=HumanDecisionAction.RETRY_PLANNER,
    )
    resume_calls: list[RunCheckpoint] = []
    resumed = PlanningMissionRunResult(
        PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA
    )

    def resume(selected: RunCheckpoint) -> PlanningMissionRunResult:
        resume_calls.append(selected)
        if len(resume_calls) == 1:
            raise RuntimeError("transient resume failure")
        return resumed

    with pytest.raises(RuntimeError, match="transient resume failure"):
        runtime.resolve_planning_mission(
            decision,
            human_decision_coordinator=coordinator,
            resume=resume,
        )

    recovered = runtime.resolve_planning_mission(
        decision,
        human_decision_coordinator=coordinator,
        resume=resume,
    )
    repeated = runtime.resolve_planning_mission(
        decision,
        human_decision_coordinator=coordinator,
        resume=resume,
    )

    assert recovered.resumed_run is resumed
    assert repeated.resumed_run is None
    assert resume_calls == [checkpoint, checkpoint]


def test_planning_mission_reports_stale_environment_data_without_generation(
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
        FixedPlanningIntentInterpreter(),
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
        event_kind="environment_data",
        payload={"graph": {"entities": []}},
    )

    def heartbeat() -> None:
        transport.publish_event("environment-data", scene)
        context_coordination.publish_source_fact(
            "environment_data",
            1,
            reference=scene.event_id,
            fresh=False,
        )

    calls: list[object] = []

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start with stale environment data")

    result = runtime.run_planning_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        environment_heartbeat=heartbeat,
        generate=generate,
        **_terminal_planning_dependencies(runtime),
        model=FixedSummaryModel(),
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA
    assert result.human_decision_request is not None
    assert (
        str(result.human_decision_request.category) == "insufficient_environment_data"
    )
    assert result.execution is None
    assert result.context_snapshot is not None
    assert result.environment_event == scene
    assert result.planner_choice is None
    assert result.attempt is None
    assert calls == []


def test_planning_mission_reports_unreferenced_environment_data_without_generation(
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
        FixedPlanningIntentInterpreter(),
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
        event_kind="environment_data",
        payload={"graph": {"entities": []}},
    )
    calls: list[object] = []

    def heartbeat() -> None:
        transport.publish_event("environment-data", scene)

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start without a snapshot reference")

    result = runtime.run_planning_mission(
        mission_input,
        hyper_agent=hyper_agent,
        context_coordination=context_coordination,
        environment_heartbeat=heartbeat,
        **_terminal_planning_dependencies(runtime),
        generate=generate,
        model=FixedSummaryModel(),
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA
    assert result.human_decision_request is not None
    assert (
        str(result.human_decision_request.category) == "insufficient_environment_data"
    )
    assert result.execution is None
    assert result.context_snapshot is None
    assert result.environment_event == scene
    assert result.planner_choice is None
    assert result.attempt is None
    assert calls == []


def test_direct_authority_plan_completes_physical_mission_run(tmp_path: Path) -> None:
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
        mission_id=mission_input.mission_id,
        source_authority=mission_input.source_authority,
        plan_revision=1,
        mission_snapshot_id="mission-provenance:snapshot:1",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(ScheduledManeuver("survey", intent, (), 0, 1),),
    )
    context = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        clock=lambda: "t-runtime",
    )
    fsm = runtime.create_fsm_runner(
        mission_id=mission_input.mission_id,
    )
    adapter = RecordingAdapter()
    decision_provider = FixedDecisionProvider(intent)
    control = runtime.create_maneuver_control(
        adapter,
        decision_provider,
    )
    environment = FakeEnvironment(
        cast(FileTransport, runtime.transport),
        mission_input.mission_id,
    )

    with pytest.raises(
        RuntimeError, match="direct NormalizedPlan execution is retired"
    ):
        runtime.run_mission(
            mission_input,
            plan=plan,
            context_coordination=context,
            fsm_runner=fsm,
            maneuver_control=control,
            environment_step=environment.run_once,
            model=FixedSummaryModel(),
        )
