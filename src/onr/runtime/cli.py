"""Command-line entry point for one configured ONR demo mission run."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from onr.adapters.file_transport import FileTransport
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
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
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _rollover_demo_artifacts(
    *, repo_root: Path, lease: RuntimeLeaseStore
) -> Path | None:
    source = repo_root / "var"
    if not source.exists():
        return None

    current = lease.inspect()
    if current is not None and current.status == "active":
        raise RuntimeError("another runtime session is active")

    archive_root = repo_root / "data/past_debug_rounds" / _utc_archive_timestamp()
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
        "mission_id": result.plan.mission_id,
        "plan_revision": result.plan.plan_revision,
        "command_id": result.command.command_id,
        "maneuver_id": result.command.maneuver_id,
        "final_state": result.final_status.active_state,
        "final_status": result.final_status.status,
        "environment_file": str(environment_file)
        if environment_file is not None
        else None,
    }


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one configured ONR mission with the deterministic demo environment"
    )
    parser.add_argument("--mission-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config-path", type=Path)
    parser.add_argument(
        "--planner-artifacts",
        type=Path,
        default=Path("var/planner-artifacts"),
        help="directory for generated planner drafts and solver evidence",
    )
    parser.add_argument(
        "--recursion-limit",
        type=_positive_integer,
        default=100,
        help="maximum Deep Agent graph steps for the Hyper planning episode",
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
        planner_artifacts = Path(args.planner_artifacts)
        if not planner_artifacts.is_absolute():
            planner_artifacts = repo_root / planner_artifacts
        planner_artifacts = planner_artifacts.resolve()
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

        stage = "model composition"
        skill_catalog = FilesystemRoleSkillCatalog(repo_root / "conf/skills")
        hyper_model = runtime.create_chat_model(
            mission_id=mission_input.mission_id,
            debug_scope="hyper-agent",
        )
        context_coordination = runtime.create_context_coordination(
            mission_id=mission_input.mission_id
        )
        environment = _create_demo_environment(
            runtime,
            mission_input.mission_id,
            output_root=repo_root / "var/environment",
        )

        stage = "environment heartbeat"
        heartbeat = environment.heartbeat()
        with runtime.transport.open_consumer(
            context_coordination.subscription
        ) as context_consumer:
            planning_snapshot = context_coordination.run_once(context_consumer)
        if (
            not isinstance(planning_snapshot, MissionSnapshot)
            or planning_snapshot.environment_data
            != heartbeat.environment_event.event_id
        ):
            raise RuntimeError(
                "Context Coordination did not publish the planning snapshot"
            )

        stage = "Hyper planning"
        hyper_workflow = runtime.create_hyper_workflow(
            model=hyper_model,
            system_prompt=hyper_prompt,
            mission_id=mission_input.mission_id,
            skill_catalog=skill_catalog,
            backend_root=repo_root,
        )
        hyper_context = runtime.create_hyper_workflow_context(
            mission_input,
            planning_snapshot,
            heartbeat.environment_event,
            artifact_root=planner_artifacts,
        )
        hyper_result = hyper_workflow.run(
            hyper_context,
            thread_id=f"planning-run:{mission_input.mission_id}:1",
            recursion_limit=args.recursion_limit,
        )
        plan = hyper_result.normalized_plan
        if hyper_result.outcome is not HyperWorkflowOutcome.PLAN_READY or plan is None:
            raise RuntimeError(
                f"Hyper planning ended without a Normalized Plan: {hyper_result.outcome}"
            )

        stage = "execution model composition"
        maneuver_model = runtime.create_chat_model(
            mission_id=mission_input.mission_id,
            debug_scope="maneuver-control",
        )
        summary_model = runtime.create_chat_model(
            mission_id=mission_input.mission_id,
            debug_scope="mission-summary",
        )
        maneuver_control = runtime.create_maneuver_control(
            _DemoManeuverAdapter(),
            model=maneuver_model,
            mission_id=mission_input.mission_id,
            system_prompt=f"You are agent {runtime.config.agent_name}. {maneuver_prompt}",
            skill_catalog=skill_catalog,
            backend_root=repo_root,
        )
        fsm_runner = runtime.create_fsm_runner(mission_id=mission_input.mission_id)

        stage = "mission execution"
        result = runtime.run_mission(
            mission_input,
            plan=plan,
            context_coordination=context_coordination,
            fsm_runner=fsm_runner,
            maneuver_control=maneuver_control,
            environment_step=environment.run_once,
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
