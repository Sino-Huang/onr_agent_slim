"""Command-line entry point for one configured ONR demo mission run."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from onr.adapters.file_transport import FileTransport
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.application.context_coordination import (
    ActivePlanRevision,
    ClosedLoopRunResult,
)
from onr.application.mission4_planning import (
    Mission4Decision,
)
from onr.application.mission4_planning import (
    decision_from_trigger as mission4_decision_from_trigger,
)
from onr.contracts.bayesian_belief import BayesianBeliefSnapshot, BeliefKey
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import HyperHeartbeatInvocation, MissionInput
from onr.contracts.hyper_workflow import HyperWorkflowOutcome
from onr.contracts.planning import PlannerPlan
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot
from onr.contracts.transport import TransportEvent
from onr.ports.environment import EnvironmentPlanningView
from onr.runtime.composition import RuntimeComposition
from onr.runtime.lease import RuntimeLeaseStore

_MISSION_FIELDS = {"mission_id", "mission_text", "source_authority"}
_MAX_MISSION_BYTES = 1024 * 1024


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


def _rollover_runtime_artifacts(
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


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_number(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one configured ONR mission")
    parser.add_argument("--mission-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config-path", type=Path)
    parser.add_argument(
        "--planner-artifacts",
        type=Path,
        help=(
            "override storage.planner_artifacts for generated planner drafts "
            "and solver evidence"
        ),
    )
    parser.add_argument(
        "--recursion-limit",
        type=_positive_integer,
        default=240,
        help="maximum Deep Agent graph steps for the Hyper planning episode",
    )
    parser.add_argument(
        "--simulation-limit-seconds",
        type=_positive_number,
        default=600.0,
        help="maximum simulated Mission duration before the loop stops",
    )
    parser.add_argument(
        "--demo-environment",
        action="store_true",
        help=(
            "acknowledge the deterministic fake adapter when that adapter is "
            "selected; unnecessary for external physical profiles"
        ),
    )
    parser.add_argument(
        "--skip-runtime-artifact-rollover",
        action="store_true",
        help=(
            "keep repo-root/var in place when this run already uses isolated "
            "artifact paths"
        ),
    )
    parser.add_argument(
        "--result-path",
        type=Path,
        help="write the terminal closed-loop result as JSON",
    )
    return parser


def _mission_mode_context(
    world_model_info: Mapping[str, object],
) -> tuple[str, tuple[int | str, ...]]:
    mode = world_model_info.get("mission_mode", "mission1")
    if mode not in {"mission1", "mission2", "mission3", "mission4", "joint"}:
        raise ValueError("environment planning view has an unsupported mission mode")
    if mode in {"mission1", "joint"}:
        reports = world_model_info.get("ship_event_reports")
        if not isinstance(reports, Mapping):
            raise TypeError("environment planning view has no ship report roster")
        return str(mode), tuple(sorted(int(entity_id) for entity_id in reports))
    if mode == "mission2":
        predictions = world_model_info.get("perception_predictions")
        trajectories = (
            predictions.get("trajectories")
            if isinstance(predictions, Mapping)
            else None
        )
        if not isinstance(trajectories, Mapping):
            raise TypeError("environment planning view has no Mission 2 trajectories")
        return str(mode), tuple(sorted(int(entity_id) for entity_id in trajectories))
    if mode == "mission3":
        inspection = world_model_info.get("mission3")
        if not isinstance(inspection, Mapping) or inspection.get("schema_version") != 1:
            raise TypeError("environment planning view has no Mission 3 roster")
        roster = inspection.get("selected_ship_ids")
        if not isinstance(roster, (list, tuple)):
            raise TypeError("environment planning view has invalid Mission 3 roster")
        return str(mode), tuple(roster)
    search = world_model_info.get("mission4")
    if not isinstance(search, Mapping) or search.get("schema_version") != 1:
        raise TypeError("environment planning view has no Mission 4 search state")
    objectives = search.get("objectives")
    if not isinstance(objectives, Mapping) or not objectives:
        raise RuntimeError("Mission 4 requires an accepted worker objective before planning")
    return str(mode), ()


def _mission_end_time(
    world_model_info: Mapping[str, object], mission_mode: str
) -> float:
    if mission_mode == "mission4":
        search = world_model_info["mission4"]
        if not isinstance(search, Mapping):
            raise TypeError("environment planning view has no Mission 4 search state")
        return float(search["deadline_s"])
    return float(world_model_info["mission_end_time_s"])


class MissionRejectedError(RuntimeError):
    """The Hyper workflow refused the operator Mission Intent as out of scope."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Mission rejected: {reason}")
        self.reason = reason


def _run_hyper_revision(
    runtime: RuntimeComposition,
    mission_input: MissionInput,
    *,
    model: object,
    system_prompt: str,
    skill_catalog: FilesystemRoleSkillCatalog,
    backend_root: Path,
    artifact_root: Path,
    planning_snapshot: MissionSnapshot,
    environment_event: TransportEvent,
    environment_file: Path,
    belief_snapshot: BayesianBeliefSnapshot | ReportingReliabilitySnapshot | None,
    belief_file: Path | None,
    revision: int,
    recursion_limit: int,
    belief_service: Any | None = None,
    refresh_planning_context: Callable[[], MissionSnapshot] | None = None,
    mission4_gate_decision: Mission4Decision | None = None,
) -> ActivePlanRevision | None:
    revision_root = artifact_root / f"revision-{revision:03d}"
    workflow = runtime.create_hyper_workflow(
        model=model,
        system_prompt=system_prompt,
        mission_id=mission_input.mission_id,
        skill_catalog=skill_catalog,
        backend_root=backend_root,
        artifact_root=revision_root,
    )
    context = runtime.create_hyper_workflow_context(
        mission_input,
        planning_snapshot,
        environment_event,
        environment_file,
        artifact_root=revision_root,
        belief_snapshot=belief_snapshot,
        belief_file=belief_file,
        backend_root=backend_root,
        belief_service=belief_service,
        refresh_planning_context=refresh_planning_context,
        mission4_gate_decision=mission4_gate_decision,
    )
    result = workflow.run(
        context,
        thread_id=f"planning-run:{mission_input.mission_id}:{revision}",
        recursion_limit=recursion_limit,
    )
    if result.outcome is HyperWorkflowOutcome.MISSION_REJECTED:
        rejection = result.mission_rejection
        raise MissionRejectedError(
            "out-of-scope Mission Intent" if rejection is None else rejection.reason
        )
    if result.outcome is not HyperWorkflowOutcome.EXECUTION_READY:
        return None
    if (
        not isinstance(result.planner_plan, PlannerPlan)
        or result.statechart is None
        or result.statechart_reference is None
    ):
        raise RuntimeError("Hyper planning success lacks accepted workflow evidence")
    if result.planner_plan.plan_revision != revision:
        raise RuntimeError("Hyper planning returned an unexpected plan revision")
    planner_plan_reference = revision_root / "planner-plan.json"
    planner_plan_reference.parent.mkdir(parents=True, exist_ok=True)
    planner_plan_reference.write_text(
        result.planner_plan.to_canonical_json() + "\n", encoding="utf-8"
    )
    return ActivePlanRevision(
        planner_plan=result.planner_plan,
        planner_plan_reference=str(planner_plan_reference.resolve()),
        statechart=result.statechart,
        statechart_reference=result.statechart_reference,
    )


def run_closed_loop_demo(
    runtime: RuntimeComposition,
    mission_input: MissionInput,
    *,
    repo_root: Path,
    planner_artifacts: Path,
    recursion_limit: int,
    simulation_limit_seconds: float,
) -> ClosedLoopRunResult:
    """Compose initial planning and run the complete live simulation."""

    if not isinstance(runtime.transport, FileTransport):
        raise RuntimeError("demo mission requires transport.backend=file")
    prompt_root = repo_root / "conf/system_prompt"
    hyper_prompt = load_system_prompt(prompt_root, "hyper-agent")
    supervisor_prompt = load_system_prompt(prompt_root, "hyper-supervisor")
    maneuver_prompt = load_system_prompt(prompt_root, "maneuver-control")
    skills = FilesystemRoleSkillCatalog(repo_root / "conf/skills")
    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        input_topic="planning-evidence",
        clock=lambda: "2026-08-23T00:00:00+10:00",
    )
    environment = runtime.create_environment_update_source(
        mission_id=mission_input.mission_id,
        output_root=runtime.config.environment_profile.artifact_root,
        context_topic="planning-evidence",
    )
    planning_view = environment.planning_view()
    event_report = planning_view.environment_event.payload.get("static_info")
    if not isinstance(event_report, (list, tuple)) or not all(
        isinstance(item, Mapping) for item in event_report
    ):
        raise TypeError("environment planning view has no static_info evidence")
    world_model_info = planning_view.environment_event.payload.get("world_model_info")
    if not isinstance(world_model_info, Mapping):
        raise TypeError("environment planning view has no world-model evidence")
    mode, ship_ids = _mission_mode_context(world_model_info)
    if mode in {"mission2", "mission3", "mission4", "joint"}:
        simulation_limit_seconds = min(
            simulation_limit_seconds, _mission_end_time(world_model_info, mode)
        )
    planning_backend_root = Path(
        os.path.commonpath(
            (
                repo_root.resolve(),
                planner_artifacts.resolve(),
                planning_view.environment_file.resolve(),
            )
        )
    )
    belief_service = (
        None
        if mode not in {"mission1", "joint"}
        else runtime.create_bayesian_belief_service(
            mission_id=mission_input.mission_id,
            keys=tuple(
                BeliefKey(entity_id, "reporting-corruption") for entity_id in ship_ids
            ),
            belief_kind="reporting_reliability",
            context_topic="planning-evidence",
            clock=lambda: "2026-08-23T00:00:00+10:00",
        )
    )
    belief = None if belief_service is None else belief_service.load_current_snapshot()
    with runtime.transport.open_consumer(
        context_coordination.subscription
    ) as context_consumer:
        planning_snapshot = context_coordination.drain_to_latest(context_consumer)
    if not isinstance(planning_snapshot, MissionSnapshot):
        raise RuntimeError("Context Coordination did not publish initial evidence")

    def refresh_planning_context() -> MissionSnapshot:
        with runtime.transport.open_consumer(
            context_coordination.subscription
        ) as refresh_consumer:
            refreshed = context_coordination.drain_to_latest(refresh_consumer)
        if not isinstance(refreshed, MissionSnapshot):
            raise RuntimeError(
                "prior initialization did not publish a Mission Snapshot"
            )
        return refreshed

    hyper_model = runtime.create_chat_model(
        mission_id=mission_input.mission_id,
        debug_scope="hyper-agent",
    )
    active = _run_hyper_revision(
        runtime,
        mission_input,
        model=hyper_model,
        system_prompt=hyper_prompt,
        skill_catalog=skills,
        backend_root=planning_backend_root,
        artifact_root=planner_artifacts,
        planning_snapshot=planning_snapshot,
        environment_event=planning_view.environment_event,
        environment_file=planning_view.environment_file,
        belief_snapshot=belief,
        belief_file=None
        if belief_service is None
        else belief_service.current_snapshot_path(),
        revision=1,
        recursion_limit=recursion_limit,
        belief_service=belief_service,
        refresh_planning_context=refresh_planning_context,
    )
    if active is None:
        raise RuntimeError(
            "initial Hyper planning did not produce an executable revision"
        )

    supervisor_model = runtime.create_chat_model(
        mission_id=mission_input.mission_id,
        debug_scope="hyper-agent-supervisor",
    )
    supervisor = runtime.create_hyper_supervisor(
        model=supervisor_model,
        system_prompt=supervisor_prompt,
        mission_id=mission_input.mission_id,
        skill_catalog=skills,
        backend_root=planning_backend_root,
    )
    communication = runtime.create_communication_port(
        hyper_handler=supervisor.handle_agent_message
    )
    fsm_runner = runtime.create_fsm_runner(mission_id=mission_input.mission_id)
    maneuver_model = runtime.create_chat_model(
        mission_id=mission_input.mission_id,
        debug_scope="maneuver-control",
    )
    maneuver_control = runtime.create_maneuver_control(
        model=maneuver_model,
        mission_id=mission_input.mission_id,
        system_prompt=f"You are agent {runtime.config.agent_name}. {maneuver_prompt}",
        skill_catalog=skills,
        backend_root=planning_backend_root,
        fsm_runner=fsm_runner,
        belief_service=belief_service,
        communication_port=communication,
    )

    def replan(
        invocation: HyperHeartbeatInvocation,
        revision: int,
        snapshot: MissionSnapshot,
        latest_planning_view: EnvironmentPlanningView,
    ) -> ActivePlanRevision | None:
        mission4_decision = None
        for trigger in getattr(invocation, "trigger_identities", ()):
            mission4_decision = mission4_decision_from_trigger(trigger)
            if mission4_decision is not None:
                break
        try:
            return _run_hyper_revision(
                runtime,
                mission_input,
                model=hyper_model,
                system_prompt=hyper_prompt,
                skill_catalog=skills,
                backend_root=planning_backend_root,
                artifact_root=planner_artifacts,
                planning_snapshot=snapshot,
                environment_event=latest_planning_view.environment_event,
                environment_file=latest_planning_view.environment_file,
                belief_snapshot=None
                if belief_service is None
                else belief_service.load_current_snapshot(),
                belief_file=None
                if belief_service is None
                else belief_service.current_snapshot_path(),
                revision=revision,
                recursion_limit=recursion_limit,
                belief_service=belief_service,
                refresh_planning_context=refresh_planning_context,
                mission4_gate_decision=mission4_decision,
            )
        except MissionRejectedError:
            # A replan refusal keeps the currently accepted plan revision.
            return None

    context_coordination = runtime.create_context_coordination(
        mission_id=mission_input.mission_id,
        input_topic="planning-evidence",
        clock=lambda: "2026-08-23T00:00:00+10:00",
        environment_update_source=environment,
        fsm_runner=fsm_runner,
        maneuver_control=maneuver_control,
        hyper_supervisor=supervisor,
        belief_service=belief_service,
        replan_workflow=replan,
        maneuver_seconds=runtime.config.heartbeats.maneuver_seconds,
        hyper_seconds=runtime.config.heartbeats.hyper_seconds,
        simulation_limit_seconds=simulation_limit_seconds,
    )
    communication.register(
        "maneuver-control", context_coordination.handle_agent_message
    )
    return context_coordination.run(active)


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
        if (
            runtime.config.environment_profile.adapter_kind == "fake"
            and not args.demo_environment
        ):
            raise RuntimeError(
                "the fake environment profile requires --demo-environment"
            )
        planner_artifacts = (
            Path(args.planner_artifacts)
            if args.planner_artifacts is not None
            else runtime.config.storage.planner_artifacts
        )
        if not planner_artifacts.is_absolute():
            planner_artifacts = repo_root / planner_artifacts
        planner_artifacts = planner_artifacts.resolve()
        if not isinstance(runtime.transport, FileTransport):
            raise RuntimeError("demo mission requires transport.backend=file")

        if prior_var_exists and not args.skip_runtime_artifact_rollover:
            stage = "runtime artifact rollover"
            lease = runtime.lease
            if lease is None:
                raise RuntimeError("runtime lease was not initialized")
            archived = _rollover_runtime_artifacts(repo_root=repo_root, lease=lease)
            if archived is not None:
                # Runtime composition creates the transport root. Recreate it only
                # after the wholesale move so this run cannot write into the archive.
                runtime.transport.root.mkdir(parents=True, exist_ok=True)

        stage = "configured LLM endpoint check"
        runtime.verify_llm_reachability()

        stage = "system prompt loading"
        load_system_prompt(repo_root / "conf/system_prompt", "hyper-agent")
        load_system_prompt(repo_root / "conf/system_prompt", "hyper-supervisor")
        load_system_prompt(repo_root / "conf/system_prompt", "maneuver-control")

        stage = "closed-loop Mission run"
        with runtime.runtime_session():
            result = run_closed_loop_demo(
                runtime,
                mission_input,
                repo_root=repo_root,
                planner_artifacts=planner_artifacts,
                recursion_limit=args.recursion_limit,
                simulation_limit_seconds=args.simulation_limit_seconds,
            )
        result_payload = result.to_dict()
        if args.result_path is not None:
            args.result_path.parent.mkdir(parents=True, exist_ok=True)
            args.result_path.write_text(
                json.dumps(result_payload, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result_payload, sort_keys=True))
        return 0
    except MissionRejectedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"mission runtime failed during {stage} ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


__all__ = ["MissionRejectedError", "load_mission_file", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
