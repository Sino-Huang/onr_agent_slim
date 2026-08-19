from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml

from harness.fake_environment import FakeEnvironment
from onr.adapters.file_transport import FileTransport
from onr.contracts.hyper_agent import FrozenMissionSpec, MissionInput
from onr.contracts.planning import PlanningOutcome
from onr.runtime import RuntimeComposition


pytestmark = pytest.mark.live

_PROFILE_CASES = (
    ("temporal", "minizinc"),
    ("symbolic", "fast-downward"),
)


class _LiveManeuverAdapter:
    def submit(self, command: object) -> object:
        return {"command_id": getattr(command, "command_id", None)}


def _runtime_with_temporary_roots(tmp_path: Path) -> RuntimeComposition:
    repository_root = Path(__file__).resolve().parents[1]
    source = repository_root / "conf" / "onr_agent_params.yaml"
    values = yaml.safe_load(source.read_text(encoding="utf-8"))
    values["transport"]["root"] = str(tmp_path / "transport")
    values["storage"]["root"] = str(tmp_path / "storage")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return RuntimeComposition.create(
        repo_root=repository_root,
        config_path=config_path,
    )


def _hyper_prompt(profile: str, planner_id: str) -> str:
    if profile == "temporal":
        plan_fields = (
            f'"planner_choice":{{"planning_profile":"{profile}","planner_id":"{planner_id}"}},'
            '"maneuvers":[{"maneuver_id":"survey","intent":{"action":"survey",'
            '"parameters":{}},"dependencies":[],"duration":1}],"horizon":2'
        )
    else:
        plan_fields = (
            f'"planner_choice":{{"planning_profile":"{profile}","planner_id":"{planner_id}"}},'
            '"maneuvers":[{"maneuver_id":"survey","intent":{"action":"survey",'
            '"parameters":{}},"dependencies":[],"cost":1}],"domain_revision":1'
        )
    return (
        "Return only one JSON Mission Specification object, with no markdown. "
        f"Use planner profile {profile!r} and planner ID {planner_id!r}. "
        "Create exactly one maneuver with maneuver_id 'survey' and action 'survey'. "
        "Use the mission input mission_id, mission_text as objective, and source_authority "
        "from the input. The object must contain mission_id, objective, source_authority, "
        f"{plan_fields}."
    )


def _maneuver_prompt(mission_id: str) -> str:
    return (
        "Return only one JSON ManeuverControlDecision object, with no markdown. "
        f"Use mission_id {mission_id!r}, the plan_revision from fsm_status, and maneuver_id "
        "'survey'. Select exactly one physical intent with action 'survey' and no parameters. "
        "Set transition_event and choice to null, payload to {}, and schema_version to 1. "
        "Use any non-empty decision_id."
    )


@pytest.mark.parametrize("profile, planner_id", _PROFILE_CASES)
def test_live_vllm_real_solver_pipeline(
    tmp_path: Path,
    profile: str,
    planner_id: str,
) -> None:
    runtime = _runtime_with_temporary_roots(tmp_path)
    artifact_root = tmp_path / "planner-artifacts"
    mission_id = f"issue22-phase3-{profile}-{uuid4().hex}"

    try:
        runtime.verify_llm_reachability()
        planners = runtime.create_planners(artifact_root)
        assert profile in planners
        selected_planner = planners[profile]
        assert selected_planner is not None
        model = runtime.create_chat_model()
        hyper_agent = runtime.create_hyper_agent(
            planner=selected_planner,
            model=model,
            planners=planners,
            mission_id=mission_id,
            system_prompt=_hyper_prompt(profile, planner_id),
        )
        maneuver_control = runtime.create_maneuver_control(
            _LiveManeuverAdapter(),
            model=model,
            mission_id=mission_id,
            system_prompt=_maneuver_prompt(mission_id),
        )
        mission_input = MissionInput(
            mission_id=mission_id,
            mission_text=f"Perform the {profile} survey maneuver.",
            source_authority="live-phase-3-test",
        )
        context_coordination = runtime.create_context_coordination(
            mission_id=mission_id,
            clock=lambda: "live-phase-3",
        )
        fsm_runner = runtime.create_fsm_runner(mission_id=mission_id, clock=lambda: 0)
        environment = FakeEnvironment(
            cast(FileTransport, runtime.transport),
            mission_id,
        )
        environment_calls: list[bool] = []

        def environment_step() -> object:
            environment_calls.append(True)
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
        assert result.authority.mission_id == mission_id
        assert result.plan.outcome is PlanningOutcome.SOLVED
        assert len(result.plan.maneuvers) == 1
        assert result.feedback.mission_id == mission_id
        assert result.feedback.payload["command_id"] == result.command.command_id
        assert result.feedback.payload["correlation_id"] == result.command.correlation_id
        assert environment_calls == [True]
        assert environment.run_once() is None

        command_dir = (
            runtime.config.transport.root
            / "commands"
            / "maneuver-adapter"
            / mission_id
        )
        assert len(list(command_dir.glob("*.json"))) == 1
        feedback_dir = (
            runtime.config.transport.root
            / "topics"
            / "maneuver-feedback"
            / "missions"
            / mission_id
        )
        assert len(list(feedback_dir.glob("*.json"))) == 1

        profile_root = artifact_root / profile
        run_directories = [path for path in profile_root.iterdir() if path.is_dir()]
        assert len(run_directories) == 1
        expected_artifacts = (
            {"model.mzn", "data.dzn", "solver.stdout", "solver.stderr"}
            if profile == "temporal"
            else {"domain.pddl", "problem.pddl", "sas_plan", "solver.stdout", "solver.stderr"}
        )
        run_files = {path.name for path in run_directories[0].iterdir() if path.is_file()}
        assert expected_artifacts <= run_files
        print(
            json.dumps(
                {
                    "endpoint": runtime.config.llm.base_url,
                    "model": runtime.config.llm.model,
                    "artifact_directory": str(run_directories[0]),
                    "artifact_paths": sorted(map(str, run_directories[0].iterdir())),
                    "stdout_path": str(run_directories[0] / "solver.stdout"),
                    "stderr_path": str(run_directories[0] / "solver.stderr"),
                },
                sort_keys=True,
            )
        )
    except Exception:
        diagnostics = (
            sorted(str(path) for path in artifact_root.rglob("*"))
            if artifact_root.exists()
            else []
        )
        print(
            f"live pipeline failed endpoint={runtime.config.llm.base_url} "
            f"model={runtime.config.llm.model} artifact_root={artifact_root} "
            f"artifact_paths={diagnostics} stdout/stderr paths are under each run directory",
        )
        raise
