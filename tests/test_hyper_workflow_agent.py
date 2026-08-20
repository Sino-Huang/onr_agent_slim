from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from onr.adapters.minizinc import MiniZincExecutor
from onr.adapters.operational_log import InProcessOperationalLog
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.agents.hyper_workflow import (
    DeepAgentsHyperWorkflow,
    HyperWorkflowContext,
    create_hyper_workflow_agent,
)
from onr.application.minizinc_translation import MiniZincTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planner_translation import (
    PlanningTranslationOutcome,
    operational_scene_graph_sha256,
)
from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlanningOutcome,
    TemporalAssignment,
)
from onr.contracts.planning_evidence import TranslationAttemptOutcome
from onr.contracts.transport import TransportEvent

_REPO_ROOT = Path(__file__).parents[1]
_STAGES = (
    "Parse Mission Intent into PlanningIntent",
    "Decide and record the MiniZinc planner inside PlanningIntent",
    "Load the current snapshot-authorized operational evidence",
    "Generate and persist MiniZinc problem files",
    "Run MiniZinc and repair rejected translations",
)


class ScriptedWorkflowModel(BaseChatModel):
    responses: list[AIMessage]
    response_index: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-hyper-workflow"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        _ = messages, stop, run_manager, kwargs
        response = self.responses[self.response_index]
        self.response_index += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(
        self,
        tools: object,
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ScriptedWorkflowModel:
        _ = tools, tool_choice, kwargs
        return self


class RejectingMiniZincPlanner:
    def __init__(self) -> None:
        self.checked_assets: list[dict[str, bytes]] = []

    def check(self, assets: Mapping[str, bytes]) -> bool:
        self.checked_assets.append(dict(assets))
        return False

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        _ = assets
        raise AssertionError("a statically rejected MiniZinc problem must not execute")


class VerifiedMiniZincPlanner:
    def __init__(self, evidence: PlannerExecutionEvidence) -> None:
        self.evidence = evidence
        self.check_count = 0

    def check(self, assets: Mapping[str, bytes]) -> bool:
        self.check_count += 1
        return self.check_count == 2 and set(assets) == {"model.mzn", "data.dzn"}

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        for path in self.evidence.artifact_paths:
            path.write_bytes(assets[path.name])
        return PlannerExecutionResult(
            PlanningOutcome.SOLVED,
            (TemporalAssignment("observe-ship-1", 0, 1),),
            self.evidence,
        )


def _tool_call(name: str, args: dict[str, object], index: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": f"workflow-tool-{index}",
                "type": "tool_call",
            }
        ],
    )


def _todos(active_index: int) -> list[dict[str, str]]:
    return [
        {
            "content": stage,
            "status": (
                "completed"
                if index < active_index
                else "in_progress"
                if index == active_index
                else "pending"
            ),
        }
        for index, stage in enumerate(_STAGES)
    ]


def _scene_context() -> tuple[MissionInput, MissionSnapshot, TransportEvent]:
    mission = MissionInput(
        mission_id="workflow-minizinc-rejection",
        mission_text="Observe the risky ships while maximizing field of view coverage.",
        source_authority="mission-control",
    )
    scene = TransportEvent(
        schema_version=1,
        event_id="scene-workflow-1",
        mission_id=mission.mission_id,
        sequence=0,
        event_kind="operational_scene_graph",
        payload={
            "graph": {
                "mission_id": mission.mission_id,
                "entities": [
                    {
                        "id": "ship-1",
                        "type": "ship",
                        "location": {"x": 12.0, "y": 4.0, "z": 0.0},
                        "risk": 0.8,
                    },
                    {
                        "id": "drone-1",
                        "type": "drone",
                        "location": {"x": 0.0, "y": 0.0, "z": 10.0},
                    },
                ],
            }
        },
    )
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=1,
        created_at="2026-08-20T00:00:00+00:00",
        operational_scene_graph=scene.event_id,
        source_revisions={"operational_scene_graph": 0},
        source_hashes={
            "operational_scene_graph": operational_scene_graph_sha256(scene)
        },
        source_health={"operational_scene_graph": "healthy"},
        source_freshness={"operational_scene_graph": True},
    )
    return mission, snapshot, scene


def _planner_evidence(tmp_path: Path) -> PlannerExecutionEvidence:
    directory = tmp_path / "solver-run"
    directory.mkdir()
    model = directory / "model.mzn"
    data = directory / "data.dzn"
    stdout = directory / "solver.stdout"
    stderr = directory / "solver.stderr"
    model.write_text("solve satisfy;\n", encoding="utf-8")
    data.write_text("horizon = 2;\n", encoding="utf-8")
    stdout.write_text('{"status":"optimal"}\n', encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return PlannerExecutionEvidence(directory, (model, data), stdout, stderr)


def test_verified_hyper_workflow_returns_normalized_plan_and_logs_progress(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    artifact_root = tmp_path / "planner-artifacts"
    draft_root = artifact_root / "drafts" / "001"
    model_path = draft_root / "model.mzn"
    data_path = draft_root / "data.dzn"
    second_draft_root = artifact_root / "drafts" / "002"
    second_model_path = second_draft_root / "model.mzn"
    second_data_path = second_draft_root / "data.dzn"
    planning_intent = {
        "mission_id": mission.mission_id,
        "source_authority": mission.source_authority,
        "objective": "observe risky ships",
        "planning_profile": "temporal",
        "planner_id": "minizinc",
        "rationale": "Observation feasibility depends on time and location.",
        "details": {"observation_objective": "field of view coverage"},
    }
    responses = [
        _tool_call("write_todos", {"todos": _todos(0)}, 1),
        _tool_call(
            "read_file",
            {"file_path": "/conf/skills/hyper/mission-parsing/SKILL.md"},
            2,
        ),
        _tool_call(
            "read_file",
            {"file_path": "/conf/skills/hyper/planner-selection/SKILL.md"},
            3,
        ),
        _tool_call("record_planning_intent", planning_intent, 4),
        _tool_call("write_todos", {"todos": _todos(1)}, 5),
        _tool_call("write_todos", {"todos": _todos(2)}, 6),
        _tool_call("load_planning_context", {}, 7),
        _tool_call("write_todos", {"todos": _todos(3)}, 8),
        _tool_call(
            "read_file",
            {
                "file_path": (
                    "/conf/skills/hyper/creating-minizinc-problem-files/SKILL.md"
                )
            },
            9,
        ),
        _tool_call(
            "persist_planner_assets",
            {
                "attempt_number": 1,
                "model_mzn": "solve satisfy;\n",
                "data_dzn": "horizon = 2;\n",
                "horizon": 2,
                "maneuvers": [
                    {
                        "maneuver_id": "observe-ship-1",
                        "action": "observe",
                        "parameters": {"entity_id": "ship-1"},
                        "dependencies": [],
                        "duration": 1,
                    }
                ],
                "translator_id": "hyper-minizinc",
                "translator_version": "1.0.0",
            },
            10,
        ),
        _tool_call("write_todos", {"todos": _todos(4)}, 11),
        _tool_call(
            "planner_executor",
            {
                "planner_id": "minizinc",
                "asset_references": [str(model_path), str(data_path)],
            },
            12,
        ),
        _tool_call("write_todos", {"todos": _todos(4)}, 13),
        _tool_call(
            "persist_planner_assets",
            {
                "attempt_number": 2,
                "model_mzn": "constraint true;\nsolve satisfy;\n",
                "data_dzn": "horizon = 2;\n",
                "horizon": 2,
                "maneuvers": [
                    {
                        "maneuver_id": "observe-ship-1",
                        "action": "observe",
                        "parameters": {"entity_id": "ship-1"},
                        "dependencies": [],
                        "duration": 1,
                    }
                ],
                "translator_id": "hyper-minizinc",
                "translator_version": "1.0.0",
            },
            14,
        ),
        _tool_call(
            "planner_executor",
            {
                "planner_id": "minizinc",
                "asset_references": [
                    str(second_model_path),
                    str(second_data_path),
                ],
            },
            15,
        ),
        _tool_call("write_todos", {"todos": _todos(5)}, 16),
        _tool_call(
            "HyperWorkflowResultCandidate",
            {"mission_id": mission.mission_id, "outcome": "plan_ready"},
            17,
        ),
    ]
    log = InProcessOperationalLog()
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        scene_graph=scene,
        artifact_root=artifact_root,
        minizinc_translation=MiniZincTranslation(
            VerifiedMiniZincPlanner(_planner_evidence(tmp_path)),
            artifact_root / "generation-attempts",
            max_corrections=0,
        ),
        max_planner_attempts=2,
        operational_log=log,
    )
    graph = create_hyper_workflow_agent(
        model=ScriptedWorkflowModel(responses=responses),
        system_prompt=load_system_prompt(
            _REPO_ROOT / "conf/system_prompt", "hyper-agent"
        ),
        mission_id=mission.mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(_REPO_ROOT / "conf/skills"),
        backend_root=_REPO_ROOT,
        checkpointer=InMemorySaver(),
    )

    result = DeepAgentsHyperWorkflow(graph).run(
        context,
        thread_id=f"planning-run:{mission.mission_id}:verified",
        recursion_limit=64,
    )

    assert result.outcome is HyperWorkflowOutcome.PLAN_READY
    assert result.normalized_plan is not None
    assert result.normalized_plan.mission_id == mission.mission_id
    assert result.normalized_plan.maneuvers[0].maneuver_id == "observe-ship-1"
    assert [todo["status"] for todo in result.todos] == ["completed"] * 5
    assert [record.event_kind for record in log.replay(mission.mission_id)] == [
        "workflow",
        "planning-intent",
        "planner-choice",
        "planning-context",
        "planner-assets",
        "planner-execution",
        "planner-assets",
        "planner-execution",
        "workflow",
    ]
    assert [record.outcome for record in log.replay(mission.mission_id)] == [
        "started",
        "completed",
        "completed",
        "completed",
        "completed",
        "repair_exhausted",
        "completed",
        "verified",
        "completed",
    ]


def test_one_hyper_workflow_reaches_rejected_minizinc_tool_result(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    draft_root = tmp_path / "planner-artifacts" / "drafts" / "001"
    model_path = draft_root / "model.mzn"
    data_path = draft_root / "data.dzn"
    invalid_model = "this is not valid MiniZinc;\n"
    data = "horizon = 2;\n"
    planning_intent = {
        "mission_id": mission.mission_id,
        "source_authority": mission.source_authority,
        "objective": "maximize risk-weighted field of view coverage",
        "planning_profile": "temporal",
        "planner_id": "minizinc",
        "rationale": "The objective depends on observation time and drone location.",
        "details": {"observation_objective": "risk-weighted field of view coverage"},
    }

    responses = [
        _tool_call("write_todos", {"todos": _todos(0)}, 1),
        _tool_call(
            "read_file",
            {"file_path": "/conf/skills/hyper/mission-parsing/SKILL.md"},
            2,
        ),
        _tool_call(
            "read_file",
            {"file_path": "/conf/skills/hyper/planner-selection/SKILL.md"},
            3,
        ),
        _tool_call("record_planning_intent", planning_intent, 4),
        _tool_call("write_todos", {"todos": _todos(1)}, 5),
        _tool_call("write_todos", {"todos": _todos(2)}, 6),
        AIMessage(content=""),
        _tool_call("load_planning_context", {}, 7),
        _tool_call("write_todos", {"todos": _todos(3)}, 8),
        _tool_call(
            "read_file",
            {
                "file_path": (
                    "/conf/skills/hyper/creating-minizinc-problem-files/SKILL.md"
                )
            },
            9,
        ),
        _tool_call(
            "persist_planner_assets",
            {
                "attempt_number": 1,
                "model_mzn": invalid_model,
                "data_dzn": data,
                "horizon": 2,
                "maneuvers": [
                    {
                        "maneuver_id": "observe-ship-1",
                        "action": "observe",
                        "parameters": {"entity_id": "ship-1"},
                        "dependencies": [],
                        "duration": 1,
                    }
                ],
                "translator_id": "hyper-minizinc",
                "translator_version": "1.0.0",
            },
            10,
        ),
        _tool_call("write_todos", {"todos": _todos(4)}, 11),
        _tool_call(
            "planner_executor",
            {
                "planner_id": "minizinc",
                "asset_references": [str(model_path), str(data_path)],
            },
            12,
        ),
        _tool_call("write_todos", {"todos": _todos(4)}, 13),
        _tool_call(
            "HyperWorkflowResultCandidate",
            {
                "mission_id": mission.mission_id,
                "outcome": "planner_rejected",
            },
            14,
        ),
    ]
    model = ScriptedWorkflowModel(responses=responses)
    planner = MiniZincExecutor(
        executable=(
            _REPO_ROOT / "modules/MiniZincIDE-2.9.7-bundle-linux-x86_64/bin/minizinc"
        ),
        artifact_root=tmp_path / "planner-artifacts" / "solver-runs",
        timeout_seconds=10,
    )
    translator = MiniZincTranslation(
        planner,
        tmp_path / "planner-artifacts" / "generation-attempts",
        max_corrections=0,
    )
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        scene_graph=scene,
        artifact_root=tmp_path / "planner-artifacts",
        minizinc_translation=translator,
    )
    graph = create_hyper_workflow_agent(
        model=model,
        system_prompt=load_system_prompt(
            _REPO_ROOT / "conf/system_prompt", "hyper-agent"
        ),
        mission_id=mission.mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(_REPO_ROOT / "conf/skills"),
        backend_root=_REPO_ROOT,
        checkpointer=InMemorySaver(),
    )

    thread_id = f"planning-run:{mission.mission_id}:1"
    result = DeepAgentsHyperWorkflow(graph).run(
        context,
        thread_id=thread_id,
        recursion_limit=64,
    )

    assert result.outcome is HyperWorkflowOutcome.PLANNER_REJECTED
    assert result.planning_intent is not None
    assert result.planning_intent.planner_choice.planner_id == "minizinc"
    assert result.translation is not None
    assert result.translation.outcome is PlanningTranslationOutcome.REPAIR_EXHAUSTED
    assert result.translation.generation_attempts[-1].outcome is (
        TranslationAttemptOutcome.REJECTED
    )
    assert result.translation.correction_feedback[-1].stage == "static"
    assert [todo["status"] for todo in result.todos] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "in_progress",
    ]
    assert model_path.read_text(encoding="utf-8") == invalid_model
    assert data_path.read_text(encoding="utf-8") == data
    rejected_attempt = result.translation.generation_attempts[-1]
    assert {
        name: Path(reference).read_bytes()
        for name, reference in rejected_attempt.asset_references.items()
    } == {"model.mzn": invalid_model.encode(), "data.dzn": data.encode()}
    planner_results = [
        json.loads(cast(str, message.content))
        for message in result.messages
        if isinstance(message, ToolMessage) and message.name == "planner_executor"
    ]
    assert planner_results == [
        {
            "attempt_id": result.translation.generation_attempts[-1].attempt_id,
            "attempt_outcome": "rejected",
            "correction_message": (
                "Generated planner assets failed static validation."
            ),
            "correction_stage": "static",
            "outcome": "repair_exhausted",
            "planner_id": "minizinc",
            "retries_remaining": 0,
        }
    ]
    checkpoint = cast(Any, graph).get_state({"configurable": {"thread_id": thread_id}})
    assert checkpoint.values["todos"] == list(result.todos)
    assert model.response_index == len(responses)


def test_hyper_workflow_recursion_limit_stops_before_planner_execution(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    model = ScriptedWorkflowModel(
        responses=[
            _tool_call("write_todos", {"todos": _todos(0)}, 1),
            _tool_call(
                "read_file",
                {"file_path": "/conf/skills/hyper/mission-parsing/SKILL.md"},
                2,
            ),
            _tool_call(
                "record_planning_intent",
                {
                    "mission_id": mission.mission_id,
                    "source_authority": mission.source_authority,
                    "objective": "observe the risky ships",
                    "planning_profile": "temporal",
                    "planner_id": "minizinc",
                    "rationale": "Observation feasibility depends on time and location.",
                    "details": {"observation_objective": "field of view coverage"},
                },
                3,
            ),
        ]
    )
    planner = RejectingMiniZincPlanner()
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        scene_graph=scene,
        artifact_root=tmp_path / "planner-artifacts",
        minizinc_translation=MiniZincTranslation(
            planner,
            tmp_path / "planner-artifacts" / "generation-attempts",
            max_corrections=0,
        ),
    )
    graph = create_hyper_workflow_agent(
        model=model,
        system_prompt=load_system_prompt(
            _REPO_ROOT / "conf/system_prompt", "hyper-agent"
        ),
        mission_id=mission.mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(_REPO_ROOT / "conf/skills"),
        backend_root=_REPO_ROOT,
        checkpointer=InMemorySaver(),
    )

    with pytest.raises(GraphRecursionError):
        DeepAgentsHyperWorkflow(graph).run(
            context,
            thread_id=f"planning-run:{mission.mission_id}:debug",
            recursion_limit=5,
        )

    assert model.response_index < len(model.responses)
    assert planner.checked_assets == []
    assert not (tmp_path / "planner-artifacts" / "drafts").exists()
