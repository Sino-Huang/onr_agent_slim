"""Standalone live Maneuver demo beginning at the accepted post-Hyper seam."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from onr.adapters.file_transport import FileTransport
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.agents.maneuver_tools import ManeuverHeartbeatExecutionRecord
from onr.application.communication import TransportCommunicationPort
from onr.contracts.bayesian_belief import BeliefKey
from onr.contracts.communication import AgentMessage
from onr.contracts.fsm import FSMStatus
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.maneuver_control import (
    ManeuverHeartbeatCompletion,
    ManeuverInvocation,
)
from onr.demo.fake_environment import FakeEnvironment
from onr.demo.maneuver_patrol import (
    MANEUVER_DEMO_INSTRUCTIONS,
    DemoEnvironmentAuthority,
    create_demo_patrol,
)
from onr.runtime.cli import _rollover_demo_artifacts, load_mission_file
from onr.runtime.composition import RuntimeComposition
from onr.runtime.config import EnvironmentUpdateOwnership


@dataclass(frozen=True, slots=True)
class ManeuverDemoRunResult:
    """Public evidence from one standalone post-Hyper Maneuver demo."""

    mission_id: str
    plan_revision: int
    final_state: str
    heartbeat_count: int
    transition_count: int
    physical_actions: tuple[str, ...]
    belief_revision: int
    hyper_message_count: int
    override_confirmed: bool
    statechart_reference: Path
    environment_file: Path | None
    agent_log_directory: Path
    llm_log_directory: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "final_state": self.final_state,
            "heartbeat_count": self.heartbeat_count,
            "transition_count": self.transition_count,
            "physical_actions": list(self.physical_actions),
            "belief_revision": self.belief_revision,
            "hyper_message_count": self.hyper_message_count,
            "override_confirmed": self.override_confirmed,
            "statechart_reference": str(self.statechart_reference),
            "environment_file": (
                str(self.environment_file) if self.environment_file else None
            ),
            "agent_log_directory": str(self.agent_log_directory),
            "llm_log_directory": str(self.llm_log_directory),
        }


def run_maneuver_demo(
    runtime: RuntimeComposition,
    mission_input: MissionInput,
    *,
    repo_root: Path,
    artifact_root: Path,
) -> ManeuverDemoRunResult:
    """Run the real Maneuver agent against accepted demo planning artifacts."""

    if not isinstance(runtime.transport, FileTransport):
        raise TypeError("the Maneuver demo requires file transport")
    artifacts = create_demo_patrol(mission_input)
    plan = artifacts.plan
    chart = artifacts.statechart
    artifact_root.mkdir(parents=True, exist_ok=True)
    statechart_reference = artifact_root / "accepted-statechart.json"
    statechart_reference.write_text(chart.to_canonical_json(), encoding="utf-8")

    environment_updates = runtime.create_environment_update_source(
        mission_id=plan.mission_id,
        ownership=EnvironmentUpdateOwnership.COORDINATOR_DRIVEN,
        output_root=repo_root / "var/environment",
    )
    environment = cast(Any, environment_updates).environment
    environment_updates.planning_view()
    authority = DemoEnvironmentAuthority(environment)
    runner = runtime.create_fsm_runner(mission_id=plan.mission_id)
    status = asyncio.run(runner.activate(chart))
    belief_service = runtime.create_bayesian_belief_service(
        mission_id=plan.mission_id,
        keys=(BeliefKey("ship-1", "collision"),),
        particle_count=256,
        seed=7,
    )
    communication = TransportCommunicationPort(cast(Any, runtime.transport))
    hyper_messages: list[AgentMessage] = []
    communication.register(
        "hyper-agent",
        lambda message: hyper_messages.append(message) or {"status": "received"},
    )
    model = runtime.create_chat_model(
        mission_id=plan.mission_id,
        debug_scope="maneuver-control",
    )
    prompt = load_system_prompt(repo_root / "conf/system_prompt", "maneuver-control")
    control = runtime.create_maneuver_control(
        model=model,
        system_prompt=prompt + MANEUVER_DEMO_INSTRUCTIONS,
        mission_id=plan.mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(repo_root / "conf/skills"),
        backend_root=repo_root,
        fsm_runner=runner,
        belief_service=belief_service,
        communication_port=communication,
    )
    communication.register("maneuver-control", control.handle_agent_message)

    completions: list[ManeuverHeartbeatCompletion] = []
    physical_actions: list[str] = []

    def advance_to(mission_time: float) -> None:
        while environment_updates.current_time < mission_time:
            environment_updates.advance()
            environment_updates.drain_updates()

    def heartbeat(mission_time: float, label: str) -> FSMStatus:
        nonlocal status
        advance_to(mission_time)
        environment_updates.planning_view()
        invocation = ManeuverInvocation(
            request_id=f"maneuver-demo:{plan.mission_id}:{label}",
            correlation_id=f"maneuver-demo:{plan.mission_id}",
            mission_id=plan.mission_id,
            plan_revision=plan.plan_revision,
            statechart_reference=str(statechart_reference),
            fsm_context=control.transition_intents.focused_context(
                status,
                control.transition_intents.current(status, invalidate_stale=True),
            ),
            environment_data=authority.current_environment_data(),
            trigger_identities=(f"manual:{label}",),
            available_recipients=("hyper-agent",),
        )
        completion = control.heartbeat(invocation)
        if not isinstance(completion, ManeuverHeartbeatCompletion):
            raise TypeError("Maneuver demo heartbeat returned an invalid completion")
        completions.append(completion)
        cast(Any, environment_updates).consume_commands()
        record = control.last_execution_record
        if isinstance(record, ManeuverHeartbeatExecutionRecord):
            physical_actions.extend(
                decision.physical_intent.action
                for decision in record.decisions
                if decision.physical_intent is not None
            )
        current = asyncio.run(runner.status())
        if not isinstance(current, FSMStatus):
            raise TypeError("Maneuver demo FSM Runner returned invalid status")
        status = current
        return current

    heartbeat(0, "depart-1")
    _require_state(status, "moving-to-patrol-stop-1")
    _require_navigation(environment, "active")
    advance_to(5)
    _require_navigation(environment, "completed")
    heartbeat(5, "arrive-1")

    heartbeat(6, "depart-2")
    _require_navigation(environment, "active")
    advance_to(10)
    heartbeat(10, "arrive-2")

    heartbeat(11, "depart-3")
    advance_to(15)
    heartbeat(15, "arrive-3")
    if not hyper_messages:
        raise RuntimeError("Maneuver demo did not communicate with Hyper")

    heartbeat(16, "depart-4")
    _require_navigation(environment, "active")
    authority.emergency_override = True
    heartbeat(17, "emergency")
    override_confirmed = _override_confirmed(environment)
    if not override_confirmed:
        raise RuntimeError("Maneuver demo did not override active navigation")
    authority.emergency_override = False
    advance_to(20)

    heartbeat(20, "arrive-4")
    final = heartbeat(21, "complete")
    _require_state(final, "patrol-complete")
    if len(hyper_messages) != 1:
        raise RuntimeError(
            "Maneuver demo expected exactly one Hyper report, got "
            f"{len(hyper_messages)}"
        )
    if len(completions) != 10:
        raise RuntimeError("Maneuver demo heartbeat completions are inconsistent")

    belief = belief_service.load_current_snapshot()
    mission_component = quote(plan.mission_id, safe="._-")
    debug_root = runtime.config.storage.root.parent / "debug"
    result = ManeuverDemoRunResult(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        final_state=final.active_state,
        heartbeat_count=len(completions),
        transition_count=len(chart.transitions),
        physical_actions=tuple(physical_actions),
        belief_revision=belief.belief_revision if belief is not None else 0,
        hyper_message_count=len(hyper_messages),
        override_confirmed=override_confirmed,
        statechart_reference=statechart_reference,
        environment_file=environment.last_output_path,
        agent_log_directory=(
            debug_root / "agent" / "maneuver-control" / mission_component
        ),
        llm_log_directory=(debug_root / "llm" / "maneuver-control" / mission_component),
    )
    environment_updates.stop()
    environment_updates.join()
    return result


def _require_state(status: FSMStatus, expected: str) -> None:
    if status.active_state != expected:
        raise RuntimeError(
            f"Maneuver demo expected FSM state {expected}, got {status.active_state}"
        )


def _require_navigation(environment: FakeEnvironment, expected: str) -> None:
    if environment.navigation_status != expected:
        raise RuntimeError(
            "Maneuver demo expected navigation status "
            f"{expected}, got {environment.navigation_status}"
        )


def _override_confirmed(environment: FakeEnvironment) -> bool:
    feedback = environment.last_override_feedback
    maneuver = environment.current_maneuver
    if feedback is None or not isinstance(maneuver, Mapping):
        return False
    payload = feedback.payload.get("payload")
    return (
        isinstance(payload, Mapping)
        and payload.get("reason") == "overridden"
        and maneuver.get("action") == "land"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the real Maneuver Control agent from accepted demo planning artifacts"
        )
    )
    parser.add_argument("--mission-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config-path", type=Path)
    parser.add_argument(
        "--planner-artifacts",
        type=Path,
        help=(
            "override storage.planner_artifacts for the accepted post-Hyper "
            "Statechart fixture"
        ),
    )
    parser.add_argument(
        "--demo-environment",
        action="store_true",
        required=True,
        help="acknowledge use of the deterministic demo environment",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    stage = "mission input"
    try:
        mission_input = load_mission_file(args.mission_file)
        repo_root = Path(args.repo_root).resolve()
        config_path = Path(args.config_path) if args.config_path else None
        prior_var_exists = (repo_root / "var").exists()
        stage = "runtime configuration"
        runtime = RuntimeComposition.create(
            repo_root=repo_root,
            config_path=config_path,
        )
        artifact_root = (
            Path(args.planner_artifacts)
            if args.planner_artifacts is not None
            else runtime.config.storage.planner_artifacts
        )
        if not artifact_root.is_absolute():
            artifact_root = repo_root / artifact_root
        artifact_root = artifact_root.resolve()
        if prior_var_exists:
            stage = "demo artifact rollover"
            lease = runtime.lease
            if lease is None:
                raise RuntimeError("runtime lease was not initialized")
            archived = _rollover_demo_artifacts(repo_root=repo_root, lease=lease)
            if archived is not None and isinstance(runtime.transport, FileTransport):
                runtime.transport.root.mkdir(parents=True, exist_ok=True)
        stage = "configured LLM endpoint check"
        runtime.verify_llm_reachability()
        stage = "Maneuver demo"
        result = run_maneuver_demo(
            runtime,
            mission_input,
            repo_root=repo_root,
            artifact_root=artifact_root / "maneuver-demo",
        )
        print(json.dumps(result.to_dict(), sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI converts failures to safe status.
        print(
            f"maneuver demo failed during {stage} ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


__all__ = ["ManeuverDemoRunResult", "main", "run_maneuver_demo"]


if __name__ == "__main__":
    raise SystemExit(main())
