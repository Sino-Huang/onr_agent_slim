from __future__ import annotations

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
from onr.application.minizinc_translation import MiniZincTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planner_translation import environment_data_sha256
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
        source_hashes={
            "environment_data": environment_data_sha256(scene)
        },
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    planner = RejectingPlanner()
    return (
        HyperWorkflowContext(
            mission_input=mission,
            mission_snapshot=snapshot,
            environment_event=scene,
            artifact_root=tmp_path / "planner-artifacts",
            minizinc_translation=MiniZincTranslation(
                planner,
                tmp_path / "planner-artifacts/generation-attempts",
                max_corrections=0,
            ),
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
        backend_root=_REPO_ROOT,
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
    assert [item["status"] for item in result.todos[:5]] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "in_progress",
    ]
