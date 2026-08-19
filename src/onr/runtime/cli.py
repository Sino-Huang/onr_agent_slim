"""Command-line entry point for one configured ONR demo mission run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence, cast

from onr.adapters.file_transport import FileTransport
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.contracts.bayesian_belief import BeliefKey
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.maneuver_control import ManeuverCommand
from onr.demo.fake_environment import FakeEnvironment
from onr.runtime.composition import RuntimeComposition, RuntimeRunResult
from onr.runtime.lease import RuntimeLeaseStore


_MISSION_FIELDS = {"mission_id", "mission_text", "source_authority"}
_MAX_MISSION_BYTES = 1024 * 1024


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


def _utc_archive_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _rollover_demo_artifacts(
    *, repo_root: Path, lease: RuntimeLeaseStore
) -> Path | None:
    source = repo_root / "var"
    if not source.exists():
        return None

    current = lease.inspect()
    if current is not None and current.status == "active":
        raise RuntimeError("another runtime session is active")

    archive_root = (
        repo_root / "data/past_debug_rounds" / _utc_archive_timestamp()
    )
    archive_root.mkdir(parents=True, exist_ok=False)
    destination = archive_root / "var"
    source.rename(destination)
    return destination


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
        "belief_revision": (
            result.belief_snapshot.belief_revision
            if result.belief_snapshot is not None
            else None
        ),
        "belief_sha256": (
            result.belief_snapshot.content_sha256
            if result.belief_snapshot is not None
            else None
        ),
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
        prior_var_exists = (repo_root / "var").exists()

        stage = "runtime configuration"
        runtime = _create_runtime(repo_root=repo_root, config_path=config_path)
        if not isinstance(runtime.transport, FileTransport):
            raise RuntimeError("demo mission requires transport.backend=file")

        if prior_var_exists:
            stage = "demo artifact rollover"
            lease = runtime.lease
            if lease is None:
                raise RuntimeError("runtime lease was not initialized")
            archived = _rollover_demo_artifacts(repo_root=repo_root, lease=lease)
            if archived is not None:
                # Runtime composition creates the transport root. Recreate it only
                # after the wholesale move so this run cannot write into the archive.
                runtime.transport.root.mkdir(parents=True, exist_ok=True)

        stage = "configured LLM endpoint check"
        runtime.verify_llm_reachability()

        stage = "system prompt loading"
        prompt_root = repo_root / "conf/system_prompt"
        hyper_prompt = load_system_prompt(prompt_root, "hyper-agent")
        maneuver_prompt = load_system_prompt(prompt_root, "maneuver-control")

        stage = "planner and model composition"
        artifact_root = Path(args.planner_artifacts)
        if not artifact_root.is_absolute():
            artifact_root = repo_root / artifact_root
        planners = runtime.create_planners(artifact_root.resolve())
        skill_catalog = FilesystemRoleSkillCatalog(repo_root / "conf/skills")
        hyper_model = runtime.create_chat_model(
            mission_id=mission_input.mission_id,
            debug_scope="hyper-agent",
        )
        maneuver_model = runtime.create_chat_model(
            mission_id=mission_input.mission_id,
            debug_scope="maneuver-control",
        )
        summary_model = runtime.create_chat_model(
            mission_id=mission_input.mission_id,
            debug_scope="mission-summary",
        )
        hyper_agent = runtime.create_hyper_agent(
            planners=planners,
            model=hyper_model,
            mission_id=mission_input.mission_id,
            system_prompt=f"You are agent {runtime.config.agent_name}. {hyper_prompt}",
            skill_catalog=skill_catalog,
            backend_root=repo_root,
        )
        maneuver_control = runtime.create_maneuver_control(
            _DemoManeuverAdapter(),
            model=maneuver_model,
            mission_id=mission_input.mission_id,
            system_prompt=f"You are agent {runtime.config.agent_name}. {maneuver_prompt}",
            skill_catalog=skill_catalog,
            backend_root=repo_root,
        )
        context_coordination = runtime.create_context_coordination(
            mission_id=mission_input.mission_id
        )
        fsm_runner = runtime.create_fsm_runner(mission_id=mission_input.mission_id)
        belief_service = runtime.create_bayesian_belief_service(
            mission_id=mission_input.mission_id,
            keys=tuple(
                BeliefKey(f"ship-{index}", "collision") for index in range(1, 4)
            ),
            particle_count=512,
            seed=0,
        )
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
            bayesian_belief_service=belief_service,
            model=summary_model,
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
