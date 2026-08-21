"""Accepted post-Hyper patrol artifacts for the live Maneuver demo."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from onr.contracts.fsm import Statechart, StatechartCondition, StatechartTransition
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanProvenance,
    ScheduledManeuver,
    VerifiableReference,
)
from onr.demo.fake_environment import FakeEnvironment


@dataclass(frozen=True, slots=True)
class DemoPatrolArtifacts:
    """Verified planning artifacts injected at the post-Hyper seam."""

    plan: NormalizedPlan
    statechart: Statechart


class DemoEnvironmentAuthority:
    """Expose current fake evidence plus a controllable emergency fact."""

    def __init__(self, environment: FakeEnvironment) -> None:
        self.environment = environment
        self.emergency_override = False

    def current_environment_data(self) -> Mapping[str, object]:
        payload = self.environment.current_environment_data()
        graph = dict(cast(Mapping[str, object], payload["scene_graph"]))
        if self.emergency_override:
            graph["emergency_override"] = {
                "action": "land",
                "maneuver_id": "emergency-landing",
                "x": 300,
                "y": 50,
                "reason": "simulated loss of safe navigation",
            }
        return {**payload, "scene_graph": graph}


MANEUVER_DEMO_INSTRUCTIONS = """

For this live post-Hyper patrol demo, act deterministically from the injected
evidence:

- When the exact live transition candidate's time condition is satisfied, call
  transition_fsm once with that exact event.
- After a successful transition, use only the returned active_state_context for
  every remaining physical, belief, or communication choice in that heartbeat;
  do not act on the source-state context. If phase is moving, call navigate using
  its maneuver_id, target_x, target_y, and speed. Do not call a physical tool for
  waiting, observing, or complete phases.
- If an observing context contains belief_observation and belief_snapshot is
  null, call update_belief with the stated values and one association for its
  entity_id with weight 1.0.
- If an observing context contains report_to_hyper, call communicate to
  hyper-agent with kind report and that exact message.
- If environment_data.scene_graph.emergency_override is present, do not attempt
  an early transition. Call land with its maneuver_id, x, and y, even if another
  physical action is active.
- Do not call tools not directed by these rules. Finish each heartbeat with a
  completion consistent with the tool effects performed.
"""


def create_demo_patrol(mission_input: MissionInput) -> DemoPatrolArtifacts:
    """Create one accepted four-stop plan and flexible ten-state Statechart."""

    mission_id = mission_input.mission_id
    stops = (
        (1, 0, 5, 0, 0, 100, 0),
        (2, 6, 10, 100, 0, 200, 50),
        (3, 11, 15, 200, 50, 300, 50),
        (4, 16, 20, 300, 50, 400, 0),
    )
    maneuvers = tuple(
        ScheduledManeuver(
            maneuver_id=f"patrol-stop-{number}",
            intent=ManeuverIntent(
                "navigate",
                (
                    ManeuverParameter("x", x),
                    ManeuverParameter("y", y),
                    ManeuverParameter("speed", 10),
                ),
            ),
            dependencies=((f"patrol-stop-{number - 1}",) if number > 1 else ()),
            start=move_start,
            duration=arrive - move_start,
        )
        for number, move_start, arrive, _from_x, _from_y, x, y in stops
    )
    plan = NormalizedPlan(
        plan_revision=1,
        mission_snapshot_id=f"{mission_id}:snapshot:1",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=maneuvers,
        provenance=PlanProvenance(
            mission_id=mission_id,
            source_authority=mission_input.source_authority,
            mission_intent=VerifiableReference("mission-input", "1" * 64),
            planning_decision=VerifiableReference("planner-choice", "2" * 64),
            environment_data=VerifiableReference("environment", "3" * 64),
            generated_assets={"model": VerifiableReference("model", "4" * 64)},
            solver_evidence={"result": VerifiableReference("result", "5" * 64)},
        ),
    )

    states = ["at-initial-location"]
    contexts: dict[str, dict[str, object]] = {
        "at-initial-location": {"phase": "waiting", "x": 0, "y": 0}
    }
    transitions: list[StatechartTransition] = []
    source = "at-initial-location"
    for number, move_start, arrive, from_x, from_y, x, y in stops:
        moving = f"moving-to-patrol-stop-{number}"
        at_stop = f"at-patrol-stop-{number}"
        states.extend((moving, at_stop))
        contexts[moving] = {
            "phase": "moving",
            "maneuver_id": f"patrol-stop-{number}",
            "from_x": from_x,
            "from_y": from_y,
            "target_x": x,
            "target_y": y,
            "speed": 10,
        }
        contexts[at_stop] = {
            "phase": "observing",
            "stop_number": number,
            "x": x,
            "y": y,
        }
        if number == 2:
            contexts[at_stop]["belief_observation"] = {
                "risk_type": "collision",
                "entity_id": "ship-1",
                "likelihood_given_risk": 0.9,
                "likelihood_given_safe": 0.1,
            }
        if number == 3:
            contexts[at_stop]["report_to_hyper"] = (
                "Patrol stop 3 has been reached under the verified plan."
            )
        transitions.extend(
            (
                StatechartTransition(
                    event=f"depart for waypoint {number}",
                    source=source,
                    target=moving,
                    conditions=(StatechartCondition(move_start, 1),),
                    requires_decision=True,
                ),
                StatechartTransition(
                    event=f"confirm waypoint {number}",
                    source=moving,
                    target=at_stop,
                    conditions=(StatechartCondition(arrive, 1),),
                    requires_decision=True,
                ),
            )
        )
        source = at_stop
    states.append("patrol-complete")
    contexts["patrol-complete"] = {"phase": "complete"}
    transitions.append(
        StatechartTransition(
            event="close the patrol",
            source=source,
            target="patrol-complete",
            conditions=(StatechartCondition(21, 1),),
            requires_decision=True,
        )
    )
    statechart = Statechart(
        mission_id=mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        normalized_plan_sha256=hashlib.sha256(
            plan.to_canonical_json().encode("utf-8")
        ).hexdigest(),
        entry_state="at-initial-location",
        terminal_states=("patrol-complete",),
        states=tuple(states),
        state_context=contexts,
        transitions=tuple(transitions),
    )
    return DemoPatrolArtifacts(plan, statechart)


__all__ = [
    "MANEUVER_DEMO_INSTRUCTIONS",
    "DemoEnvironmentAuthority",
    "DemoPatrolArtifacts",
    "create_demo_patrol",
]
