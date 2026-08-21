from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from langchain.tools import ToolRuntime
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from pydantic import Field

from onr.adapters.minizinc import MiniZincExecutor
from onr.adapters.operational_log import InProcessOperationalLog
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.agents.hyper_workflow import (
    DeepAgentsHyperWorkflow,
    HyperWorkflowContext,
    TemporalManeuverCandidate,
    _allowed_workflow_tools,
    _normalize_provider_tool_value,
    create_hyper_workflow_agent,
    load_planning_context,
    planner_executor,
    record_planning_intent,
    submit_planner_attempt,
    submit_statechart_draft,
)
from onr.application.bayesian_belief import belief_artifact_reference
from onr.application.minizinc_translation import MiniZincTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planner_translation import (
    PlanningTranslationOutcome,
    environment_data_sha256,
)
from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlannerStaticCheckResult,
    PlanningOutcome,
    TemporalAssignment,
)
from onr.contracts.planning_evidence import TranslationAttemptOutcome
from onr.contracts.transport import TransportEvent
from onr.demo.fake_belief import create_fake_entity_risk_snapshot

_REPO_ROOT = Path(__file__).parents[1]
_STAGES = (
    "Parse Mission Intent into PlanningIntent",
    "Decide and record the MiniZinc planner inside PlanningIntent",
    "Load the current snapshot-authorized operational evidence",
    "Write MiniZinc problem files from the current operational evidence",
    "Submit and statically verify the written MiniZinc attempt",
    "Execute the statically accepted MiniZinc attempt",
    "Generate a semantic Statechart from the verified NormalizedPlan",
    "Validate and repair the Statechart",
    "Hand off verified execution to Maneuver Control",
)
_REFLECTIVE_TOOLS = {
    "record_planning_intent",
    "load_planning_context",
    "planner_executor",
    "submit_planner_attempt",
    "submit_statechart_draft",
}


class ScriptedWorkflowModel(BaseChatModel):
    responses: list[AIMessage]
    response_index: int = 0
    bound_tool_names: list[frozenset[str]] = Field(default_factory=list)

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
        _ = tool_choice, kwargs
        names = {
            cast(str, cast(Any, item).name)
            for item in cast(list[object], tools)
            if isinstance(getattr(item, "name", None), str)
        }
        self.bound_tool_names.append(frozenset(names))
        return self


class RejectingMiniZincPlanner:
    def __init__(self) -> None:
        self.checked_assets: list[dict[str, bytes]] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.checked_assets.append(dict(assets))
        return PlannerStaticCheckResult(
            False,
            1,
            stderr="MiniZinc rejected the scripted model.",
        )

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        _ = assets
        raise AssertionError("a statically rejected MiniZinc problem must not execute")


class VerifiedMiniZincPlanner:
    def __init__(
        self,
        evidence: PlannerExecutionEvidence,
        *,
        reject_first: bool = False,
        rejection_message: str = "MiniZinc rejected the scripted model.",
    ) -> None:
        self.evidence = evidence
        self.reject_first = reject_first
        self.rejection_message = rejection_message
        self.check_count = 0
        self.execute_count = 0

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        self.check_count += 1
        accepted = self.check_count == (2 if self.reject_first else 1) and set(
            assets
        ) == {
            "model.mzn",
            "data.dzn",
        }
        return PlannerStaticCheckResult(
            accepted,
            0 if accepted else 1,
            stderr=self.rejection_message if not accepted else "",
        )

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        self.execute_count += 1
        for path in self.evidence.artifact_paths:
            path.write_bytes(assets[path.name])
        return PlannerExecutionResult(
            PlanningOutcome.SOLVED,
            (TemporalAssignment("observe-ship-1", 0, 1),),
            self.evidence,
        )


class ExecutionRejectingMiniZincPlanner:
    def __init__(self, evidence: PlannerExecutionEvidence) -> None:
        self.evidence = evidence

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        _ = assets
        return PlannerStaticCheckResult(True, 0)

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        _ = assets
        diagnostic = json.dumps(
            {
                "type": "error",
                "location": {
                    "filename": str(self.evidence.artifact_directory / "data.dzn")
                },
                "message": "instance mismatch",
            },
            separators=(",", ":"),
        ) + "\n"
        self.evidence.stdout_path.write_text(diagnostic, encoding="utf-8")
        self.evidence.stderr_path.write_text("planner stderr\n", encoding="utf-8")
        return PlannerExecutionResult(
            PlanningOutcome.ERROR,
            evidence=self.evidence,
            return_code=7,
            stdout=diagnostic,
            stderr="planner stderr\n",
        )


def _tool_call(name: str, args: dict[str, object], index: int) -> AIMessage:
    if name in _REFLECTIVE_TOOLS:
        args = {
            **args,
            "reflection": f"Proceeding with the {name} workflow stage.",
        }
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
        event_kind="environment_data",
        payload={
            "scene_graph": {
                "mission_id": mission.mission_id,
                "entities": [
                    {
                        "id": "ship-1",
                        "type": "ship",
                        "location": {"x": 12.0, "y": 4.0, "z": 0.0},
                    },
                    {
                        "id": "drone-1",
                        "type": "drone",
                        "location": {"x": 0.0, "y": 0.0, "z": 10.0},
                        "max_velocity": 20,
                        "fov_radius": 30,
                    },
                ],
            },
            "static_info": [
                {
                    "time": 0.5,
                    "event type": "intersection decision",
                    "entity_id": 1,
                }
            ],
        },
    )
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=1,
        created_at="2026-08-20T00:00:00+00:00",
        environment_data=scene.event_id,
        source_revisions={"environment_data": 0},
        source_hashes={"environment_data": environment_data_sha256(scene)},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
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


def _runtime(context: HyperWorkflowContext) -> ToolRuntime[HyperWorkflowContext]:
    return ToolRuntime(
        state={"messages": []},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="test-tool-call",
        store=None,
    )


def test_hyper_domain_tools_require_public_reflection() -> None:
    for workflow_tool in (
        record_planning_intent,
        load_planning_context,
        planner_executor,
        submit_planner_attempt,
        submit_statechart_draft,
    ):
        schema = cast(Any, workflow_tool).tool_call_schema.model_json_schema()
        assert "reflection" in schema["required"]
        assert "private reasoning" in schema["properties"]["reflection"]["description"]


def test_provider_quote_markers_are_removed_from_statechart_context_keys() -> None:
    assert _normalize_provider_tool_value(
        {
            "states": ["at-initial-location"],
            "state_context": {'<|"|>at-initial-location<|"|>': {"phase": "stationary"}},
        }
    ) == {
        "states": ["at-initial-location"],
        "state_context": {"at-initial-location": {"phase": "stationary"}},
    }


def test_hyper_tool_contract_renames_submission_without_adding_a_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import onr.agents.hyper_workflow as workflow_module

    captured: dict[str, object] = {}

    def fake_create_deep_agent(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(workflow_module, "_create_deep_agent", fake_create_deep_agent)
    create_hyper_workflow_agent(
        model=object(),
        system_prompt="test",
        mission_id="mission-tool-contract",
    )

    tools = cast(list[object], captured["tools"])
    names = [cast(str, cast(Any, item).name) for item in tools]
    assert len(names) == 6
    assert "submit_planner_attempt" in names
    assert "persist_planner_assets" not in names
    executor_schema = cast(Any, planner_executor).tool_call_schema.model_json_schema()
    assert "attempt_number" in executor_schema["required"]
    assert "asset_references" not in executor_schema["properties"]


def test_event_patrol_generation_phase_exposes_llm_file_writers(
    tmp_path: Path,
) -> None:
    original_mission, snapshot, scene = _scene_context()
    mission = MissionInput(
        original_mission.mission_id,
        "Patrol the environment and account for every reported event.",
        original_mission.source_authority,
    )
    artifact_root = tmp_path / "planner-artifacts"
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=scene,
        artifact_root=artifact_root,
        minizinc_translation=MiniZincTranslation(
            RejectingMiniZincPlanner(),
            artifact_root / "generation-attempts",
            max_corrections=0,
        ),
    )
    runtime = _runtime(context)
    cast(Any, record_planning_intent).func(
        mission_id=mission.mission_id,
        source_authority=mission.source_authority,
        objective="account for every reported event",
        planning_profile="temporal",
        planner_id="minizinc",
        rationale="Event capture depends on time, travel, and field of view.",
        details={"optimization_goal": "information gain"},
        reflection="Recording the event patrol planning decision.",
        runtime=runtime,
    )
    cast(Any, load_planning_context).func(
        reflection="Loading the current flexible environment payload.",
        runtime=runtime,
    )

    allowed = _allowed_workflow_tools(context)

    assert allowed == {"write_file", "edit_file"}
    workspace = artifact_root / "workspace" / "001"
    workspace.mkdir(parents=True)
    (workspace / "model.mzn").write_text("solve satisfy;\n", encoding="utf-8")
    assert _allowed_workflow_tools(context) == {"write_file", "edit_file"}
    (workspace / "data.dzn").write_text("horizon = 2;\n", encoding="utf-8")
    assert _allowed_workflow_tools(context) == {
        "edit_file",
        "write_file",
        "submit_planner_attempt",
    }
    assert "materialize_event_information_patrol" not in allowed


def test_workflow_tools_return_recoverable_prerequisites_and_ready_context(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    belief = create_fake_entity_risk_snapshot(mission.mission_id)
    snapshot = MissionSnapshot(
        mission_id=mission.mission_id,
        version=2,
        created_at="2026-08-20T00:00:01+00:00",
        environment_data=scene.event_id,
        bayesian_belief_snapshot=belief_artifact_reference(
            belief.mission_id, belief.content_sha256
        ),
        source_revisions={
            "environment_data": 0,
            "bayesian_belief_snapshot": belief.belief_revision,
        },
        source_hashes={
            "environment_data": environment_data_sha256(scene),
            "bayesian_belief_snapshot": belief.content_sha256,
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
    artifact_root = tmp_path / "planner-artifacts"
    workspace = artifact_root / "workspace" / "001"
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=scene,
        belief_snapshot=belief,
        artifact_root=artifact_root,
        minizinc_translation=MiniZincTranslation(
            RejectingMiniZincPlanner(),
            artifact_root / "generation-attempts",
            max_corrections=0,
        ),
    )
    runtime = _runtime(context)

    missing_intent = json.loads(
        cast(Any, load_planning_context).func(
            reflection="Checking whether planning prerequisites are recorded.",
            runtime=runtime,
        )
    )
    assert missing_intent == {
        "message": ("Call record_planning_intent, then retry load_planning_context."),
        "missing": ["planning_intent", "planner_choice"],
        "required_tool": "record_planning_intent",
        "retry_tool": "load_planning_context",
        "status": "prerequisite_missing",
    }
    assert context.planning_context_loaded is False

    rejected_intent = json.loads(
        cast(Any, record_planning_intent).func(
            mission_id=mission.mission_id,
            source_authority=mission.source_authority,
            objective="observe risky ships",
            planning_profile="temporal",
            planner_id="minizinc",
            rationale="Observation feasibility depends on time and location.",
            details={"objective": "field of view coverage"},
            reflection="Recording the temporal planning decision.",
            runtime=runtime,
        )
    )
    assert rejected_intent["status"] == "rejected"
    assert "reserved top-level keys" in rejected_intent["correction_message"]
    assert rejected_intent["retry_tool"] == "record_planning_intent"
    assert context.planning_intent is None

    recorded = json.loads(
        cast(Any, record_planning_intent).func(
            mission_id=mission.mission_id,
            source_authority=mission.source_authority,
            objective="observe risky ships",
            planning_profile="temporal",
            planner_id="minizinc",
            rationale="Observation feasibility depends on time and location.",
            details={"observation_objective": "field of view coverage"},
            reflection="Recording the temporal planning decision.",
            runtime=runtime,
        )
    )
    assert recorded["status"] == "accepted"
    assert recorded["next_tool"] == "load_planning_context"

    missing_context = json.loads(
        cast(Any, submit_planner_attempt).func(
            attempt_number=1,
            model_file_location=str(workspace / "model.mzn"),
            data_file_location=str(workspace / "data.dzn"),
            horizon=2,
            maneuvers=[],
            translator_id="hyper-minizinc",
            translator_version="1.0.0",
            reflection="Checking whether the planner context is ready.",
            runtime=runtime,
        )
    )
    assert missing_context["status"] == "prerequisite_missing"
    assert missing_context["required_tool"] == "load_planning_context"
    assert not (artifact_root / "drafts").exists()

    ready = json.loads(
        cast(Any, load_planning_context).func(
            reflection="Loading the snapshot-authorized planning evidence.",
            runtime=runtime,
        )
    )
    assert ready["status"] == "ready"
    assert ready["planning_intent"]["objective"] == "observe risky ships"
    assert ready["planner_choice"]["planner_choice"] == {
        "planning_profile": "temporal",
        "planner_id": "minizinc",
    }
    assert ready["mission_snapshot"] == snapshot.to_dict()
    assert ready["environment_data"] == scene.to_dict()["payload"]
    assert ready["environment_data"]["static_info"][0]["entity_id"] == 1
    drone = ready["environment_data"]["scene_graph"]["entities"][1]
    assert drone["location"] == {"x": 0.0, "y": 0.0, "z": 10.0}
    assert drone["max_velocity"] == 20
    assert drone["fov_radius"] == 30
    assert ready["belief_snapshot"] == belief.to_dict()
    assert ready["planner_asset_locations"] == {
        "model_file_location": str(artifact_root / "workspace/001/model.mzn"),
        "data_file_location": str(artifact_root / "workspace/001/data.dzn"),
    }
    assert context.planning_context_loaded is True

    duplicate = json.loads(
        cast(Any, record_planning_intent).func(
            mission_id=mission.mission_id,
            source_authority=mission.source_authority,
            objective="observe risky ships",
            planning_profile="temporal",
            planner_id="minizinc",
            rationale="Observation feasibility depends on time and location.",
            details={"observation_objective": "field of view coverage"},
            reflection="Confirming the recorded planning decision.",
            runtime=runtime,
        )
    )
    assert duplicate["status"] == "already_recorded"
    assert duplicate["next_tool"] == "load_planning_context"
    assert context.planning_context_loaded is True

    missing_draft = json.loads(
        cast(Any, planner_executor).func(
            planner_id="minizinc",
            attempt_number=1,
            reflection="Checking whether a persisted planner draft is ready.",
            runtime=runtime,
        )
    )
    assert missing_draft["status"] == "prerequisite_missing"
    assert missing_draft["required_tool"] == "submit_planner_attempt"


def test_static_repair_is_frozen_once_then_executed_with_prior_evidence(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    artifact_root = tmp_path / "planner-artifacts"
    planner = VerifiedMiniZincPlanner(
        _planner_evidence(tmp_path),
        reject_first=True,
        rejection_message=(
            f"MiniZinc rejected {tmp_path.resolve()}/planner-artifacts/"
            "workspace/001/data.dzn."
        ),
    )
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=scene,
        artifact_root=artifact_root,
        backend_root=tmp_path,
        planner_workspace_location="/planner-artifacts/workspace",
        minizinc_translation=MiniZincTranslation(
            planner,
            artifact_root / "generation-attempts",
            max_corrections=0,
        ),
        max_planner_attempts=2,
    )
    runtime = _runtime(context)
    cast(Any, record_planning_intent).func(
        mission_id=mission.mission_id,
        source_authority=mission.source_authority,
        objective="observe risky ships",
        planning_profile="temporal",
        planner_id="minizinc",
        rationale="Observation feasibility depends on time and location.",
        details={"observation_objective": "field of view coverage"},
        reflection="Recording the temporal planning decision.",
        runtime=runtime,
    )
    cast(Any, load_planning_context).func(
        reflection="Loading the planning evidence.", runtime=runtime
    )
    first_workspace = artifact_root / "workspace" / "001"
    first_workspace.mkdir(parents=True)
    first_model = b"solve satisfy;\n"
    peer_data = b"horizon = 2;\n"
    (first_workspace / "model.mzn").write_bytes(first_model)
    (first_workspace / "data.dzn").write_bytes(peer_data)
    maneuver = TemporalManeuverCandidate(
        maneuver_id="observe-ship-1",
        action="observe",
        parameters={"entity_id": "ship-1"},
        dependencies=[],
        duration=1,
    )

    first = json.loads(
        cast(Any, submit_planner_attempt).func(
            attempt_number=1,
            model_file_location="/planner-artifacts/workspace/001/model.mzn",
            data_file_location="/planner-artifacts/workspace/001/data.dzn",
            horizon=2,
            maneuvers=[maneuver],
            translator_id="hyper-minizinc",
            translator_version="1.0.0",
            reflection="Submitting the first complete attempt.",
            runtime=runtime,
        )
    )
    assert first["static_status"] == "rejected"
    assert first["verifier_stderr"] == (
        "MiniZinc rejected /planner-artifacts/workspace/001/data.dzn."
    )
    assert first["correction_message"] == first["verifier_stderr"]
    assert str(tmp_path.resolve()) not in json.dumps(first)
    assert first["asset_references"] == [
        "/planner-artifacts/drafts/001/data.dzn",
        "/planner-artifacts/drafts/001/model.mzn",
    ]
    assert first["diagnostic_references"] == {
        "stderr": "/planner-artifacts/drafts/001/minizinc-check.agent.stderr",
        "stdout": "/planner-artifacts/drafts/001/minizinc-check.agent.stdout",
    }
    assert context.static_check_result is not None
    assert str(tmp_path.resolve()) in context.static_check_result.stderr
    assert str(tmp_path.resolve()) in (
        artifact_root / "drafts/001/minizinc-check.stderr"
    ).read_text(encoding="utf-8")
    assert str(tmp_path.resolve()) not in (
        artifact_root / "drafts/001/minizinc-check.agent.stderr"
    ).read_text(encoding="utf-8")
    assert planner.check_count == 1
    assert planner.execute_count == 0
    second_workspace = artifact_root / "workspace" / "002"
    assert (second_workspace / "model.mzn").read_bytes() == first_model
    assert (second_workspace / "data.dzn").read_bytes() == peer_data

    with pytest.raises(ValueError, match="must change"):
        cast(Any, submit_planner_attempt).func(
            attempt_number=2,
            model_file_location="/planner-artifacts/workspace/002/model.mzn",
            data_file_location="/planner-artifacts/workspace/002/data.dzn",
            horizon=2,
            maneuvers=[maneuver],
            translator_id="hyper-minizinc",
            translator_version="1.0.0",
            reflection="Rejecting an unchanged resubmission.",
            runtime=runtime,
        )

    repaired_model = b"constraint true;\nsolve satisfy;\n"
    (second_workspace / "model.mzn").write_bytes(repaired_model)
    second_args = {
        "attempt_number": 2,
        "model_file_location": "/planner-artifacts/workspace/002/model.mzn",
        "data_file_location": "/planner-artifacts/workspace/002/data.dzn",
        "horizon": 2,
        "maneuvers": [maneuver],
        "translator_id": "hyper-minizinc",
        "translator_version": "1.0.0",
        "reflection": "Submitting the repaired complete attempt.",
        "runtime": runtime,
    }
    second_raw = cast(Any, submit_planner_attempt).func(**second_args)
    second = json.loads(second_raw)
    assert second["static_status"] == "accepted"
    assert second["next_tool"] == "planner_executor"
    assert planner.check_count == 2
    assert cast(Any, submit_planner_attempt).func(**second_args) == second_raw
    assert planner.check_count == 2

    executed = json.loads(
        cast(Any, planner_executor).func(
            planner_id="minizinc",
            attempt_number=second["attempt_number"],
            reflection="Executing the statically accepted attempt.",
            runtime=runtime,
        )
    )
    assert executed["outcome"] == "verified"
    assert planner.execute_count == 1
    assert context.translation is not None
    assert [str(item.outcome) for item in context.translation.generation_attempts] == [
        "rejected",
        "accepted",
    ]
    assert context.translation.correction_feedback[0].static_check is not None


def test_planner_executor_returns_exact_virtualized_execution_diagnostic(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    artifact_root = tmp_path / "planner-artifacts"
    planner = ExecutionRejectingMiniZincPlanner(_planner_evidence(tmp_path))
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=scene,
        artifact_root=artifact_root,
        backend_root=tmp_path,
        planner_workspace_location="/planner-artifacts/workspace",
        minizinc_translation=MiniZincTranslation(
            planner,
            artifact_root / "generation-attempts",
            max_corrections=0,
        ),
        max_planner_attempts=2,
    )
    runtime = _runtime(context)
    cast(Any, record_planning_intent).func(
        mission_id=mission.mission_id,
        source_authority=mission.source_authority,
        objective="observe risky ships",
        planning_profile="temporal",
        planner_id="minizinc",
        rationale="Observation feasibility depends on time and location.",
        details={"observation_objective": "field of view coverage"},
        reflection="Recording the temporal planning decision.",
        runtime=runtime,
    )
    cast(Any, load_planning_context).func(
        reflection="Loading the planning evidence.", runtime=runtime
    )
    workspace = artifact_root / "workspace" / "001"
    workspace.mkdir(parents=True)
    (workspace / "model.mzn").write_bytes(b"solve satisfy;\n")
    (workspace / "data.dzn").write_bytes(b"horizon = 2;\n")
    submission = json.loads(
        cast(Any, submit_planner_attempt).func(
            attempt_number=1,
            model_file_location="/planner-artifacts/workspace/001/model.mzn",
            data_file_location="/planner-artifacts/workspace/001/data.dzn",
            horizon=2,
            maneuvers=[
                TemporalManeuverCandidate(
                    maneuver_id="observe-ship-1",
                    action="observe",
                    parameters={"entity_id": "ship-1"},
                    dependencies=[],
                    duration=1,
                )
            ],
            translator_id="hyper-minizinc",
            translator_version="1.0.0",
            reflection="Submitting the complete attempt.",
            runtime=runtime,
        )
    )
    assert submission["static_status"] == "accepted"

    executed_raw = cast(Any, planner_executor).func(
        planner_id="minizinc",
        attempt_number=1,
        reflection="Executing the statically accepted attempt.",
        runtime=runtime,
    )
    executed = json.loads(executed_raw)

    assert executed["outcome"] == "rejected"
    assert executed["correction_stage"] == "execution"
    assert executed["planner_return_code"] == 7
    assert executed["planner_stderr"] == "planner stderr\n"
    assert '"filename":"/solver-run/data.dzn"' in executed["planner_stdout"]
    assert executed["correction_message"] == "planner stderr"
    assert executed["next_attempt_locations"] == {
        "data_file_location": "/planner-artifacts/workspace/002/data.dzn",
        "model_file_location": "/planner-artifacts/workspace/002/model.mzn",
    }
    assert str(tmp_path.resolve()) not in executed_raw
    assert planner.evidence.stdout_path.read_text(encoding="utf-8").find(
        str(tmp_path.resolve())
    ) != -1


def test_verified_hyper_workflow_returns_normalized_plan_and_logs_progress(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    artifact_root = tmp_path / "planner-artifacts"
    first_workspace = artifact_root / "workspace" / "001"
    second_workspace = artifact_root / "workspace" / "002"
    first_workspace.mkdir(parents=True)
    second_workspace.mkdir(parents=True)
    (first_workspace / "model.mzn").write_text("solve satisfy;\n", encoding="utf-8")
    (first_workspace / "data.dzn").write_text("horizon = 2;\n", encoding="utf-8")
    (second_workspace / "model.mzn").write_text(
        "constraint true;\nsolve satisfy;\n", encoding="utf-8"
    )
    (second_workspace / "data.dzn").write_text("horizon = 2;\n", encoding="utf-8")
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
        _tool_call("load_planning_context", {}, 0),
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
        _tool_call("write_todos", {"todos": _todos(4)}, 90),
        _tool_call(
            "submit_planner_attempt",
            {
                "attempt_number": 1,
                "model_file_location": str(first_workspace / "model.mzn"),
                "data_file_location": str(first_workspace / "data.dzn"),
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
        _tool_call("write_todos", {"todos": _todos(5)}, 11),
        _tool_call(
            "planner_executor",
            {
                "planner_id": "minizinc",
                "attempt_number": 1,
            },
            12,
        ),
        _tool_call("write_todos", {"todos": _todos(6)}, 16),
        _tool_call(
            "read_file",
            {"file_path": ("/conf/skills/hyper/creating-statechart-files/SKILL.md")},
            17,
        ),
        _tool_call(
            "submit_statechart_draft",
            {
                "attempt_number": 1,
                "statechart": {
                    "entry_state": "at-initial-location",
                    "terminal_states": ["observation-complete"],
                    "states": [
                        "at-initial-location",
                        "observing-ship-1",
                        "observation-complete",
                    ],
                    "state_context": {
                        "at-initial-location": {
                            "phase": "stationary",
                            "x": 0,
                            "y": 0,
                        },
                        "observing-ship-1": {
                            "phase": "observing",
                            "entity_id": "ship-1",
                        },
                        "observation-complete": {"phase": "complete"},
                    },
                    "transitions": [
                        {
                            "event": "begin-observation",
                            "source": "at-initial-location",
                            "target": "observing-ship-1",
                            "conditions": [
                                {
                                    "kind": "environment_time_at_or_after",
                                    "time_tick": 0,
                                    "time_scale": 1,
                                }
                            ],
                        },
                        {
                            "event": "complete-observation",
                            "source": "observing-ship-1",
                            "target": "observation-complete",
                            "conditions": [
                                {
                                    "kind": "environment_time_at_or_after",
                                    "time_tick": 1,
                                    "time_scale": 1,
                                }
                            ],
                        },
                    ],
                },
            },
            18,
        ),
        _tool_call("write_todos", {"todos": _todos(9)}, 19),
        _tool_call(
            "HyperWorkflowResultCandidate",
            {"mission_id": mission.mission_id, "outcome": "execution_ready"},
            20,
        ),
    ]
    log = InProcessOperationalLog()
    context = HyperWorkflowContext(
        mission_input=mission,
        mission_snapshot=snapshot,
        environment_event=scene,
        artifact_root=artifact_root,
        minizinc_translation=MiniZincTranslation(
            VerifiedMiniZincPlanner(_planner_evidence(tmp_path)),
            artifact_root / "generation-attempts",
            max_corrections=0,
        ),
        max_planner_attempts=2,
        state_machine_factory=PythonStateMachineFactory(),
        operational_log=log,
    )
    model = ScriptedWorkflowModel(responses=responses)
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

    result = DeepAgentsHyperWorkflow(graph).run(
        context,
        thread_id=f"planning-run:{mission.mission_id}:verified",
        recursion_limit=96,
    )

    assert result.outcome is HyperWorkflowOutcome.EXECUTION_READY
    assert result.normalized_plan is not None
    assert result.normalized_plan.mission_id == mission.mission_id
    assert result.normalized_plan.maneuvers[0].maneuver_id == "observe-ship-1"
    assert result.statechart is not None
    assert result.statechart_reference is not None
    assert result.initial_fsm_status is not None
    assert [todo["status"] for todo in result.todos] == ["completed"] * 9
    early_context_result = next(
        json.loads(cast(str, message.content))
        for message in result.messages
        if isinstance(message, ToolMessage)
        and message.name == "load_planning_context"
        and json.loads(cast(str, message.content))["status"] == "prerequisite_missing"
    )
    assert early_context_result["required_tool"] == "record_planning_intent"
    assert [record.event_kind for record in log.replay(mission.mission_id)] == [
        "workflow",
        "planning-intent",
        "planner-choice",
        "planning-context",
        "planner-assets",
        "planner-execution",
        "statechart-generation",
        "workflow",
    ]
    assert [record.outcome for record in log.replay(mission.mission_id)] == [
        "started",
        "completed",
        "completed",
        "completed",
        "accepted",
        "verified",
        "verified",
        "completed",
    ]
    after_intent_tools = model.bound_tool_names[5]
    assert "record_planning_intent" not in after_intent_tools
    assert "load_planning_context" in after_intent_tools
    assert "HyperWorkflowResultCandidate" not in after_intent_tools


def test_one_hyper_workflow_reaches_rejected_minizinc_tool_result(
    tmp_path: Path,
) -> None:
    mission, snapshot, scene = _scene_context()
    draft_root = tmp_path / "planner-artifacts" / "drafts" / "001"
    model_path = draft_root / "model.mzn"
    data_path = draft_root / "data.dzn"
    invalid_model = "this is not valid MiniZinc;\n"
    data = "horizon = 2;\n"
    workspace = tmp_path / "planner-artifacts" / "workspace" / "001"
    workspace.mkdir(parents=True)
    (workspace / "model.mzn").write_text(invalid_model, encoding="utf-8")
    (workspace / "data.dzn").write_text(data, encoding="utf-8")
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
        _tool_call("write_todos", {"todos": _todos(4)}, 90),
        _tool_call(
            "submit_planner_attempt",
            {
                "attempt_number": 1,
                "model_file_location": str(workspace / "model.mzn"),
                "data_file_location": str(workspace / "data.dzn"),
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
        environment_event=scene,
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
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    assert model_path.read_text(encoding="utf-8") == invalid_model
    assert data_path.read_text(encoding="utf-8") == data
    rejected_attempt = result.translation.generation_attempts[-1]
    assert {
        name: Path(reference).read_bytes()
        for name, reference in rejected_attempt.asset_references.items()
    } == {"model.mzn": invalid_model.encode(), "data.dzn": data.encode()}
    submission_results = [
        json.loads(cast(str, message.content))
        for message in result.messages
        if isinstance(message, ToolMessage) and message.name == "submit_planner_attempt"
    ]
    check_stdout = draft_root / "minizinc-check.stdout"
    check_stderr = draft_root / "minizinc-check.stderr"
    assert check_stdout.is_file()
    assert check_stderr.is_file()
    assert "syntax error" in check_stderr.read_text(encoding="utf-8")
    assert len(submission_results) == 1
    submission = submission_results[0]
    assert submission["attempt_number"] == 1
    assert submission["static_status"] == "repair_exhausted"
    assert submission["planner_id"] == "minizinc"
    assert submission["verifier_id"] == "minizinc-instance-check"
    assert submission["verifier_return_code"] != 0
    assert submission["verifier_stdout"] == check_stdout.read_text(encoding="utf-8")
    assert submission["verifier_stderr"] == check_stderr.read_text(encoding="utf-8")
    assert (
        submission["correction_message"]
        == check_stderr.read_text(encoding="utf-8").strip()
    )
    agent_check_stdout = draft_root / "minizinc-check.agent.stdout"
    agent_check_stderr = draft_root / "minizinc-check.agent.stderr"
    assert submission["diagnostic_references"] == {
        "stderr": str(agent_check_stderr.resolve()),
        "stdout": str(agent_check_stdout.resolve()),
    }
    assert submission["retries_remaining"] == 0
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
        environment_event=scene,
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
