from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from onr.adapters.minizinc import MiniZincExecutor
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.agents import DeepAgentsHyperWorkflow, create_hyper_workflow_agent
from onr.agents.hyper_workflow import HyperWorkflowContext
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planning import PlannerExecutionResult, PlannerStaticCheckResult
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot
from onr.contracts.transport import TransportEvent
from onr.demo.fake_belief import create_fake_entity_risk_snapshot
from onr.runtime import RuntimeComposition

pytestmark = pytest.mark.live
_REPO_ROOT = Path(__file__).parents[1]


class RejectingPlanner:
    def __init__(self) -> None:
        self.checked_assets: list[dict[str, bytes]] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked_assets.append(dict(assets))
        return PlannerStaticCheckResult(
            False,
            1,
            stderr="MiniZinc rejected the live-test model.",
        )

    def execute(
        self, assets: Mapping[str, bytes], solver: str
    ) -> PlannerExecutionResult:
        _ = assets, solver
        raise AssertionError("static rejection must stop before solver execution")


class PlannerAssetsCaptured(BaseException):
    """Terminate the live agent test at the planner verification boundary."""


class CapturingPlanner:
    def __init__(self) -> None:
        self.checked_assets: list[dict[str, bytes]] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked_assets.append(dict(assets))
        raise PlannerAssetsCaptured

    def execute(
        self, assets: Mapping[str, bytes], solver: str
    ) -> PlannerExecutionResult:
        _ = assets, solver
        raise AssertionError("capture must stop before solver execution")


class RecordingMiniZinc:
    def __init__(self, executor: MiniZincExecutor) -> None:
        self.executor = executor
        self.checked_assets: list[dict[str, bytes]] = []
        self.executed_assets: list[dict[str, bytes]] = []
        self.solvers: list[str] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked_assets.append(dict(assets))
        return self.executor.check(assets)

    def execute(
        self, assets: Mapping[str, bytes], solver: str
    ) -> PlannerExecutionResult:
        self.executed_assets.append(dict(assets))
        self.solvers.append(solver)
        return self.executor.execute(assets, solver)  # type: ignore[arg-type]


class UnusedFastDownward:
    def execute(self, assets: Mapping[str, bytes]) -> object:
        raise AssertionError("Fast Downward was not selected")


class UnusedVAL:
    def check(self, assets: Mapping[str, bytes]) -> object:
        raise AssertionError("VAL was not selected")

    def validate(self, evidence: object) -> bool:
        raise AssertionError("VAL was not selected")


def _persist_environment(
    tmp_path: Path, mission_id: str, event: TransportEvent
) -> Path:
    path = tmp_path / "var/environment" / mission_id / "environment.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(event.to_dict()["payload"]), encoding="utf-8")
    return path


def _runtime(tmp_path: Path) -> RuntimeComposition:
    values = yaml.safe_load(
        (_REPO_ROOT / "conf/onr_agent_params.yaml").read_text(encoding="utf-8")
    )
    values["transport"]["root"] = str(tmp_path / "transport")
    values["storage"]["root"] = str(tmp_path / "storage")
    values["planners"]["temporal"]["entrypoint"] = sys.executable
    values["planners"]["symbolic"]["entrypoint"] = sys.executable
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return RuntimeComposition.create(repo_root=_REPO_ROOT, config_path=config_path)


def _planning_context(
    tmp_path: Path,
) -> tuple[HyperWorkflowContext, RejectingPlanner]:
    mission = MissionInput(
        mission_id=f"live-hyper-workflow-{uuid4().hex}",
        mission_text=(
            "Observe ship-1 from drone-1 over a two-step horizon. Use temporal "
            "planning because feasibility depends on observation time and drone "
            "location. Maximize risk-weighted field-of-view coverage. The supplied "
            "risk score for ship-1 is 0.8."
        ),
        source_authority="live-workflow-test",
    )
    scene = TransportEvent(
        schema_version=1,
        event_id=f"scene:{mission.mission_id}:1",
        mission_id=mission.mission_id,
        sequence=0,
        event_kind="environment_data",
        payload={
            "scene_graph": {
                "mission_id": mission.mission_id,
                "entities": [
                    {
                        "id": "ship-1",
                        "type": "ship",
                        "risk": 0.8,
                        "location": {"x": 10.0, "y": 0.0, "z": 0.0},
                    },
                    {
                        "id": "drone-1",
                        "type": "drone",
                        "location": {"x": 0.0, "y": 0.0, "z": 10.0},
                    },
                ],
            },
            "static_info": [],
        },
    )
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=1,
        created_at="2026-08-20T00:00:00+00:00",
        environment_data=scene.event_id,
        source_revisions={"environment_data": 1},
        source_references={"environment_data": scene.event_id},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    planner = RejectingPlanner()
    environment_file = _persist_environment(tmp_path, mission.mission_id, scene)
    return (
        HyperWorkflowContext(
            mission_input=mission,
            mission_snapshot=snapshot,
            environment_event=scene,
            environment_file=environment_file,
            artifact_root=tmp_path / "planner-artifacts",
            backend_root=Path("/"),
            minizinc_planner=planner,
            fast_downward_planner=UnusedFastDownward(),
            val_validator=UnusedVAL(),
            max_planner_attempts=1,
        ),
        planner,
    )


def _alternate_event_schema(report: dict[str, object]) -> dict[str, object]:
    scene = report["scene_graph"]
    records = report["static_info"]
    assert isinstance(scene, dict) and isinstance(records, list)
    entities = scene["entities"]
    assert isinstance(entities, list)
    vehicles = []
    for entity in entities:
        assert isinstance(entity, dict)
        vehicle = {
            "callsign": entity["id"],
            "role": entity["type"],
            "kinematics": {"position_m": entity["location"]},
        }
        if entity["type"] == "drone":
            vehicle["performance"] = {
                "speed_mps": entity["max_velocity"],
                "sensor": {"radius_m": entity["fov_radius"]},
            }
        vehicles.append(vehicle)
    observations = []
    for index, record in enumerate(records, start=1):
        assert isinstance(record, dict)
        observations.append(
            {
                "observation_id": f"source-event-{index}",
                "subject": {"callsign": record["entity_id"]},
                "category": record["event type"],
                "occurred_at_s": record["time"],
                "coordinates_m": {
                    "east": record["position"][0],
                    "north": record["position"][1],
                    "altitude": record["position"][2],
                },
                "attributes": record["event information"],
            }
        )
    return {
        "mission_state": {"clock_seconds": scene["mission_time_seconds"]},
        "assets": {"vehicles": vehicles},
        "observations": {"timeline": observations},
    }


def _event_information_context(
    tmp_path: Path, *, alternate_schema: bool
) -> tuple[HyperWorkflowContext, RecordingMiniZinc]:
    mission = MissionInput(
        mission_id=f"live-event-materialization-{uuid4().hex}",
        mission_text=(
            "Patrol the environment and account for every event in the report. "
            "Choose a timed drone route and dwell schedule that maximizes captured "
            "information gain using 1 - probability_risk for each event entity."
        ),
        source_authority="live-workflow-test",
    )
    report_path = (
        _REPO_ROOT
        / "data/past_debug_rounds/20260822T033147.679043Z/var/environment"
        / "mission%3Ademo/environment.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    records = report["static_info"]
    assert isinstance(records, list) and len(records) == 253
    payload = _alternate_event_schema(report) if alternate_schema else report
    scene = TransportEvent(
        schema_version=1,
        event_id=f"scene:{mission.mission_id}:1",
        mission_id=mission.mission_id,
        sequence=0,
        event_kind="environment_data",
        payload=payload,
    )
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=1,
        created_at="2026-08-22T00:00:00+00:00",
        environment_data=scene.event_id,
        source_revisions={"environment_data": 1},
        source_references={"environment_data": scene.event_id},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    planner = RecordingMiniZinc(
        MiniZincExecutor(
            _REPO_ROOT / "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc",
            tmp_path / "minizinc-runs",
            timeout_seconds=30,
        )
    )
    environment_file = _persist_environment(tmp_path, mission.mission_id, scene)
    belief = create_fake_entity_risk_snapshot(mission.mission_id)
    belief_file = tmp_path / "var/beliefs" / mission.mission_id / "current.json"
    belief_file.parent.mkdir(parents=True)
    belief_file.write_text(belief.to_canonical_json() + "\n", encoding="utf-8")
    return (
        HyperWorkflowContext(
            mission_input=mission,
            mission_snapshot=snapshot,
            environment_event=scene,
            environment_file=environment_file,
            artifact_root=tmp_path / "event-planner-artifacts",
            backend_root=Path("/"),
            minizinc_planner=planner,
            fast_downward_planner=UnusedFastDownward(),
            val_validator=UnusedVAL(),
            max_planner_attempts=1,
            max_statechart_attempts=3,
            state_machine_factory=PythonStateMachineFactory(),
            belief_snapshot=belief,
            belief_file=belief_file,
        ),
        planner,
    )


def _mission1_replan_context(
    tmp_path: Path,
) -> tuple[HyperWorkflowContext, CapturingPlanner]:
    example_root = (
        _REPO_ROOT
        / "conf/skills/hyper/creating-minizinc-problem-files/examples/"
        "event-information-patrol"
    )
    environment = json.loads(
        (example_root / "replan-environment.json").read_text(encoding="utf-8")
    )
    belief = ReportingReliabilitySnapshot.from_dict(
        json.loads((example_root / "replan-belief.json").read_text(encoding="utf-8"))
    )
    mission = MissionInput(
        mission_id="mission-1",
        mission_text=(
            "Replan the reliability-aware event patrol from the current Mission "
            "time and vehicle pose. Generate a fresh MiniZinc candidate-DAG model "
            "and data file for revision 2, excluding checked and expired reports."
        ),
        source_authority="live-replan-test",
    )
    event = TransportEvent(
        schema_version=2,
        event_id="environment-data:mission-1:replan",
        mission_id=mission.mission_id,
        sequence=16,
        event_kind="environment_data",
        payload=environment,
    )
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=16,
        created_at="2026-09-03T00:00:08+10:00",
        environment_data=event.event_id,
        bayesian_belief_snapshot="belief:mission-1:revision-2",
        plan_reference="planner-artifacts/revision-001/planner-plan.json",
        plan_revision=1,
        source_revisions={
            "environment_data": 16,
            "bayesian_belief_snapshot": 2,
            "plan": 1,
        },
        source_references={
            "environment_data": event.event_id,
            "bayesian_belief_snapshot": "belief:mission-1:revision-2",
            "plan": "planner-artifacts/revision-001/planner-plan.json",
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
    planner = CapturingPlanner()
    environment_file = _persist_environment(tmp_path, mission.mission_id, event)
    shutil.copytree(_REPO_ROOT / "conf/skills", tmp_path / "conf/skills")
    belief_file = tmp_path / "var/beliefs/replan-belief.json"
    belief_file.parent.mkdir(parents=True)
    belief_file.write_text(belief.to_canonical_json() + "\n", encoding="utf-8")
    return (
        HyperWorkflowContext(
            mission_input=mission,
            mission_snapshot=snapshot,
            environment_event=event,
            environment_file=environment_file,
            artifact_root=tmp_path / "planner-artifacts/revision-002",
            backend_root=tmp_path,
            minizinc_planner=planner,
            fast_downward_planner=UnusedFastDownward(),
            val_validator=UnusedVAL(),
            max_planner_attempts=1,
            belief_snapshot=belief,
            belief_file=belief_file,
        ),
        planner,
    )


def test_live_hyper_workflow_generates_minizinc_and_receives_rejection(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.verify_llm_reachability()
    context, planner = _planning_context(tmp_path)
    mission_id = context.mission_input.mission_id
    model = runtime.create_chat_model(
        mission_id=mission_id,
        debug_scope="hyper-agent",
    )
    prompt = load_system_prompt(
        _REPO_ROOT / "conf/system_prompt",
        "hyper-agent",
    )
    graph = create_hyper_workflow_agent(
        model=model,
        system_prompt=f"You are agent {runtime.config.agent_name}. {prompt}",
        mission_id=mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(_REPO_ROOT / "conf/skills"),
        backend_root=context.backend_root,
        planner_workspace_location=context.planner_workspace_location,
        checkpointer=InMemorySaver(),
    )

    result = DeepAgentsHyperWorkflow(graph).run(
        context,
        thread_id=f"planning-run:{mission_id}:1",
        recursion_limit=120,
    )

    assert result.outcome is HyperWorkflowOutcome.PLANNER_REJECTED
    assert result.planning_intent is not None
    assert result.planning_intent.planner_choice.planner_id == "minizinc"
    assert planner.checked_assets
    assert set(planner.checked_assets[-1]) == {"model.mzn", "data.dzn"}
    assert all(planner.checked_assets[-1].values())
    assert len(result.todos) == 8
    assert [item["status"] for item in result.todos[:5]] == [
        "completed",
        "completed",
        "completed",
        "in_progress",
        "pending",
    ]
    debug_directory = tmp_path / "debug" / "agent" / "hyper-agent" / mission_id
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(debug_directory.glob("*.json"))
    ]
    tool_records = [item for item in records if item.get("kind") == "tool"]
    successful_writes = [
        item
        for item in tool_records
        if item.get("name") == "write_file" and item.get("error") is None
    ]
    assert [Path(item["input"]["file_path"]).name for item in successful_writes] == [
        "model.mzn",
        "data.dzn",
    ]
    assert all(item["input"]["content"] for item in successful_writes)
    assert not any(item.get("name") == "load_planning_context" for item in tool_records)
    submission = next(
        item for item in tool_records if item.get("name") == "submit_planner_attempt"
    )
    assert submission["sequence"] > successful_writes[-1]["sequence"]
    assert not any(item.get("name") == "planner_executor" for item in tool_records)
    controlled_records = [
        item
        for item in tool_records
        if item.get("name")
        in {"record_planning_intent", "submit_planner_attempt", "planner_executor"}
    ]
    assert "sha256" not in json.dumps(controlled_records).casefold()


def test_live_hyper_workflow_regenerates_mission1_minizinc_for_replan(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.verify_llm_reachability()
    context, planner = _mission1_replan_context(tmp_path)
    model = runtime.create_chat_model(
        mission_id=context.mission_input.mission_id,
        debug_scope="hyper-agent",
    )
    prompt = load_system_prompt(_REPO_ROOT / "conf/system_prompt", "hyper-agent")
    graph = create_hyper_workflow_agent(
        model=model,
        system_prompt=f"You are agent {runtime.config.agent_name}. {prompt}",
        mission_id=context.mission_input.mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(tmp_path / "conf/skills"),
        backend_root=context.backend_root,
        planner_workspace_location=context.planner_workspace_location,
        checkpointer=InMemorySaver(),
    )

    with pytest.raises(PlannerAssetsCaptured):
        DeepAgentsHyperWorkflow(graph).run(
            context,
            thread_id="planning-run:mission-1:2",
            recursion_limit=160,
        )

    assert planner.checked_assets
    assets = planner.checked_assets[-1]
    assert set(assets) == {"model.mzn", "data.dzn"}
    workspace = context.artifact_root / "workspace/001"
    assert {path.name for path in workspace.iterdir()} == {"model.mzn", "data.dzn"}
    example_root = (
        _REPO_ROOT
        / "conf/skills/hyper/creating-minizinc-problem-files/examples/"
        "event-information-patrol"
    )
    assert assets["model.mzn"] == (example_root / "model.mzn").read_bytes()
    assert assets["data.dzn"] == (example_root / "replan-data.dzn").read_bytes()

    solved = subprocess.run(
        [
            str(_REPO_ROOT / "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc"),
            "--solver",
            "coin-bc",
            str(workspace / "model.mzn"),
            str(workspace / "data.dzn"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(solved.stdout.splitlines()[0])
    assert output["assignments"][0]["surveillance_mode"] == "pursue_ship"
    assert output["assignments"][0]["entity_id"] == 7
    assert set(output["assignments"][0]["parameters"]["report_ids"]) == {
        "report-future-a",
        "report-future-b",
    }
    assert not {
        "report-past-altered",
        "report-past-clean",
        "report-checked",
    } & set(assets["data.dzn"].decode().split('"'))


@pytest.mark.parametrize(
    "alternate_schema", [False, True], ids=["current", "renamed-nested"]
)
def test_live_hyper_workflow_authors_event_dag_generator(
    tmp_path: Path, alternate_schema: bool
) -> None:
    runtime = _runtime(tmp_path)
    runtime.verify_llm_reachability()
    context, planner = _event_information_context(
        tmp_path, alternate_schema=alternate_schema
    )
    mission_id = context.mission_input.mission_id
    model = runtime.create_chat_model(
        mission_id=mission_id,
        debug_scope="hyper-agent",
    )
    prompt = load_system_prompt(
        _REPO_ROOT / "conf/system_prompt",
        "hyper-agent",
    )
    graph = create_hyper_workflow_agent(
        model=model,
        system_prompt=f"You are agent {runtime.config.agent_name}. {prompt}",
        mission_id=mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(_REPO_ROOT / "conf/skills"),
        backend_root=context.backend_root,
        planner_workspace_location=context.planner_workspace_location,
        checkpointer=InMemorySaver(),
    )

    result = DeepAgentsHyperWorkflow(graph).run(
        context,
        thread_id=f"planning-run:{mission_id}:1",
        recursion_limit=180,
    )

    assert result.outcome is HyperWorkflowOutcome.EXECUTION_READY
    assert planner.checked_assets
    assert planner.executed_assets
    assert planner.solvers == ["coin-bc"]
    data = planner.checked_assets[-1]["data.dzn"].decode()
    assert "source_event_count = 253;" in data
    assert "action_count = 786;" in data
    assert "arc_count = 14423;" in data
    debug_directory = tmp_path / "debug" / "agent" / "hyper-agent" / mission_id
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(debug_directory.glob("*.json"))
    ]
    tool_records = [item for item in records if item.get("kind") == "tool"]
    assert not any(
        item.get("name")
        in {
            "initialize_event_data_materialization",
            "materialize_event_information_data",
        }
        for item in tool_records
    )
    execute_records = [
        item
        for item in tool_records
        if item.get("name") == "execute" and item.get("error") is None
    ]
    jq_commands = [item["input"]["command"] for item in execute_records]
    assert any("jq 'keys'" in command for command in jq_commands)
    assert any("generate_data.py" in command for command in jq_commands)
    if alternate_schema:
        assert any(
            term in "\n".join(jq_commands)
            for term in ("observations", "timeline", "vehicles")
        )

    workspace = context.artifact_root / "workspace/001"
    generated_script = workspace / "generate_data.py"
    assert generated_script.is_file()
    script_text = generated_script.read_text(encoding="utf-8")
    if alternate_schema:
        assert "static_info" not in script_text
        assert all(
            term in script_text
            for term in ("observations", "timeline", "vehicles", "occurred_at_s")
        )
    native_plan = context.planner_plan
    assert native_plan is not None
    assert context.backend_root is not None
    plan_path = (
        context.backend_root / native_plan.planner_native_plan_artifact_reference
    )
    stream = [
        json.loads(line) for line in plan_path.read_text(encoding="utf-8").splitlines()
    ]
    solution = next(item for item in stream if item["type"] == "solution")
    output = json.loads(solution["output"]["default"])
    assert output["information_gain"] == 15_221
    assert output["stop_count"] == len(output["assignments"]) == 15
    execution_call = next(
        item for item in tool_records if item.get("name") == "planner_executor"
    )
    assert execution_call["input"]["minizinc_solver"] == "coin-bc"

    statechart_generator = workspace / "generate_statechart.py"
    statechart_path = workspace / "statechart.json"
    assert statechart_generator.is_file() and statechart_path.is_file()
    statechart_script = statechart_generator.read_text(encoding="utf-8")
    assert all(
        term in statechart_script
        for term in (
            "extract_assignments",
            "build_statechart",
            "represented_once",
            "planner_order_preserved",
        )
    )
    chart = json.loads(statechart_path.read_text(encoding="utf-8"))
    assert len(chart["states"]) == 32
    assert len(chart["transitions"]) == 31
    assert len(chart["terminal_states"]) == 1
    planner_identities = {
        value["planner_identity"]
        for value in chart["state_context"].values()
        if "planner_identity" in value
    }
    assert len(planner_identities) == 15

    def output_text(item: Mapping[str, object]) -> str:
        output = item.get("output")
        if isinstance(output, Mapping) and isinstance(output.get("content"), str):
            return output["content"]
        return json.dumps(output)

    manifest_record = next(
        item
        for item in execute_records
        if '"planner_order_preserved":true' in output_text(item).replace(" ", "")
    )
    manifest_text = output_text(manifest_record).replace(" ", "")
    assert '"planner_items":15' in manifest_text
    assert chart["terminal_states"][0] in manifest_text
    assert context.statechart is not None
    accepted = context.statechart.to_dict()
    assert accepted["schema_version"] == 2
    assert "planner_native_plan_artifact_reference" not in accepted
    assert all(
        set(edge) == {"event", "source", "target", "context"}
        for edge in accepted["transitions"]
    )
