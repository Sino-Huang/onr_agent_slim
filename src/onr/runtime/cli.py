"""Command-line entry point for one configured ONR demo mission run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence, cast

from onr.adapters.file_transport import FileTransport
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.maneuver_control import ManeuverCommand
from onr.demo.fake_environment import FakeEnvironment
from onr.runtime.composition import RuntimeComposition, RuntimeRunResult


_MISSION_FIELDS = {"mission_id", "mission_text", "source_authority"}
_MAX_MISSION_BYTES = 1024 * 1024
_HYPER_PROMPT = (
    "Interpret the supplied MissionInput as one strict Mission Specification. "
    "Preserve mission_id and source_authority exactly, select a configured planner, "
    "and return only the configured structured response."
)
_MANEUVER_PROMPT = (
    "Return only one strict ManeuverControlDecision JSON object for the supplied "
    "snapshot and FSM status. Preserve mission and plan identity and select at most "
    "one enabled physical maneuver."
)


class _DemoManeuverAdapter:
    """Acknowledge demo submissions; FakeEnvironment provides external evidence."""

    def submit(self, command: ManeuverCommand) -> object:
        return {"command_id": command.command_id}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def load_mission_file(path: Path | str) -> MissionInput:
    """Load one exact, finite JSON MissionInput without accepting extensions."""

    selected = Path(path)
    try:
        if selected.stat().st_size > _MAX_MISSION_BYTES:
            raise ValueError("mission file is too large")
        value = json.loads(
            selected.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("mission file cannot be read as JSON") from exc
    if not isinstance(value, Mapping) or set(value) != _MISSION_FIELDS:
        raise ValueError("mission file must contain exactly the required fields")
    fields: dict[str, str] = {}
    for name in sorted(_MISSION_FIELDS):
        item = value.get(name)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"mission field {name} must be a non-empty string")
        fields[name] = item
    return MissionInput(
        mission_id=fields["mission_id"],
        mission_text=fields["mission_text"],
        source_authority=fields["source_authority"],
    )


def _create_runtime(*, repo_root: Path, config_path: Path | None) -> RuntimeComposition:
    return RuntimeComposition.create(repo_root=repo_root, config_path=config_path)


def _create_demo_environment(
    runtime: RuntimeComposition,
    mission_id: str,
    *,
    output_root: Path | None = None,
) -> FakeEnvironment:
    if not isinstance(runtime.transport, FileTransport):
        raise RuntimeError("the demo environment requires file transport")
    return FakeEnvironment(runtime.transport, mission_id, output_root=output_root)


def _safe_result(
    result: RuntimeRunResult, *, environment_file: Path | None = None
) -> dict[str, object]:
    return {
        "mission_id": result.authority.mission_id,
        "plan_revision": result.plan.plan_revision,
        "command_id": result.command.command_id,
        "maneuver_id": result.command.maneuver_id,
        "final_state": result.final_status.active_state,
        "final_status": result.final_status.status,
        "environment_file": str(environment_file) if environment_file is not None else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one configured ONR mission with the deterministic demo environment"
    )
    parser.add_argument("--mission-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config-path", type=Path)
    parser.add_argument(
        "--planner-artifacts", type=Path, default=Path("var/planner-artifacts")
    )
    parser.add_argument(
        "--demo-environment",
        action="store_true",
        required=True,
        help="acknowledge use of the installed deterministic demo environment, not production authority",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    stage = "mission input"
    try:
        mission_input = load_mission_file(args.mission_file)
        repo_root = Path(args.repo_root).resolve()
        config_path = Path(args.config_path) if args.config_path is not None else None

        stage = "runtime configuration"
        runtime = _create_runtime(repo_root=repo_root, config_path=config_path)
        if not isinstance(runtime.transport, FileTransport):
            raise RuntimeError("demo mission requires transport.backend=file")

        stage = "configured LLM endpoint check"
        runtime.verify_llm_reachability()

        stage = "planner and model composition"
        artifact_root = Path(args.planner_artifacts)
        if not artifact_root.is_absolute():
            artifact_root = repo_root / artifact_root
        planners = runtime.create_planners(artifact_root.resolve())
        skill_catalog = FilesystemRoleSkillCatalog(repo_root / "conf/skills")
        try:
            model = runtime.create_chat_model(mission_id=mission_input.mission_id)
        except TypeError:
            if isinstance(runtime, RuntimeComposition):
                raise
            model = runtime.create_chat_model()
        hyper_agent = runtime.create_hyper_agent(
            planners=planners,
            model=model,
            mission_id=mission_input.mission_id,
            system_prompt=_HYPER_PROMPT,
            skill_catalog=skill_catalog,
            backend_root=repo_root,
        )
        maneuver_control = runtime.create_maneuver_control(
            _DemoManeuverAdapter(),
            model=model,
            mission_id=mission_input.mission_id,
            system_prompt=_MANEUVER_PROMPT,
            skill_catalog=skill_catalog,
            backend_root=repo_root,
        )
        context_coordination = runtime.create_context_coordination(
            mission_id=mission_input.mission_id
        )
        fsm_runner = runtime.create_fsm_runner(mission_id=mission_input.mission_id)
        environment = _create_demo_environment(
            runtime,
            mission_input.mission_id,
            output_root=repo_root / "var/environment",
        )

        stage = "mission execution"
        result = runtime.run_mission(
            mission_input,
            hyper_agent=hyper_agent,
            context_coordination=context_coordination,
            fsm_runner=fsm_runner,
            maneuver_control=maneuver_control,
            environment_step=environment.run_once,
            model=model,
        )
        print(
            json.dumps(
                _safe_result(
                    cast(RuntimeRunResult, result),
                    environment_file=environment.last_output_path,
                ),
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            f"mission runtime failed during {stage} ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


__all__ = ["load_mission_file", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
