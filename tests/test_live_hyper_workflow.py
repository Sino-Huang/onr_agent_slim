from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.agents import DeepAgentsHyperWorkflow, create_hyper_workflow_agent
from onr.agents.hyper_workflow import HyperWorkflowContext
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planning import PlannerExecutionResult, PlannerStaticCheckResult
from onr.contracts.transport import TransportEvent
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

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        _ = assets
        raise AssertionError("static rejection must stop before solver execution")


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


def _event_materialization_context(
    tmp_path: Path,
) -> tuple[HyperWorkflowContext, RejectingPlanner]:
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
    scene = TransportEvent(
        schema_version=1,
        event_id=f"scene:{mission.mission_id}:1",
        mission_id=mission.mission_id,
        sequence=0,
        event_kind="environment_data",
        payload={
            "scene_graph": {
                "mission_id": mission.mission_id,
                "mission_time_seconds": 0.0,
                "entities": [
                    {
                        "id": "drone-1",
                        "type": "drone",
                        "location": {"x": 0.0, "y": 0.0, "z": 10.0},
                        "max_velocity": 20,
                        "fov_radius": 30,
                    }
                ],
            },
            "static_info": records,
        },
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
    planner = RejectingPlanner()
    environment_file = _persist_environment(tmp_path, mission.mission_id, scene)
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


def test_live_hyper_workflow_materializes_event_data_in_multiple_batches(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.verify_llm_reachability()
    context, planner = _event_materialization_context(tmp_path)
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
        recursion_limit=300,
    )

    assert result.outcome is HyperWorkflowOutcome.PLANNER_REJECTED
    assert planner.checked_assets
    data = planner.checked_assets[-1]["data.dzn"].decode()
    assert "event_count = 253;" in data
    debug_directory = tmp_path / "debug" / "agent" / "hyper-agent" / mission_id
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(debug_directory.glob("*.json"))
    ]
    tool_records = [item for item in records if item.get("kind") == "tool"]
    initialization = next(
        item
        for item in tool_records
        if item.get("name") == "initialize_event_data_materialization"
    )
    batches = [
        item
        for item in tool_records
        if item.get("name") == "materialize_event_information_data"
        and item.get("error") is None
    ]
    assert initialization["input"]["total_event_count"] == 253
    assert len(batches) >= 2
    assert sum(len(item["input"]["events"]) for item in batches) == 253
    execute_records = [
        item
        for item in tool_records
        if item.get("name") == "execute" and item.get("error") is None
    ]
    jq_commands = [item["input"]["command"] for item in execute_records]
    assert any("jq 'keys'" in command for command in jq_commands)
    assert any(".static_info | length" in command for command in jq_commands)
    assert len([command for command in jq_commands if "to_entries" in command]) >= 2
    last_batch_sequence = batches[-1]["sequence"]
    reads_after_materialization = [
        item
        for item in tool_records
        if item.get("name") == "read_file"
        and item.get("sequence", 0) > last_batch_sequence
    ]
    assert {
        Path(item["input"]["file_path"]).name for item in reads_after_materialization
    } >= {
        "model.mzn",
        "data.dzn",
    }
    assert any(
        item.get("name") == "edit_file"
        and Path(item["input"]["file_path"]).name == "data.dzn"
        and item.get("sequence", 0) > last_batch_sequence
        for item in tool_records
    )
