import hashlib

from onr.application.temporal_planning import TemporalPlanning
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    PlannerChoice,
    PlannerExecutionResult,
    PlanningOutcome,
    TemporalAssignment,
    TemporalManeuver,
)
from onr.contracts.transport import create_normalized_plan_transport_event


_CANONICAL_NORMALIZED_PLAN = (
    '{"maneuvers":['
    '{"dependencies":[],"duration":4,"intent":{"action":"survey",'
    '"parameters":{"altitude_m":120}},'
    '"maneuver_id":"survey","start":0}],'
    '"mission_snapshot_id":"snapshot-9",'
    '"mission_spec":{"horizon":10,"maneuvers":['
    '{"dependencies":[],"duration":4,"intent":{"action":"survey",'
    '"parameters":{"altitude_m":120}},"maneuver_id":"survey"}],'
    '"mission_id":"mission-transport-1","objective":"Survey alpha",'
    '"planner_choice":{"planner_id":"minizinc",'
    '"planning_profile":"temporal"},'
    '"source_authority":"mission-control"},'
    '"outcome":"solved","plan_revision":3,'
    '"planner_choice":{"planner_id":"minizinc",'
    '"planning_profile":"temporal"}}'
)
_CANONICAL_NORMALIZED_PLAN_SHA256 = hashlib.sha256(
    _CANONICAL_NORMALIZED_PLAN.encode("utf-8")
).hexdigest()


class SolvedExecutor:
    def execute(self, assets: object) -> PlannerExecutionResult:
        return PlannerExecutionResult(
            outcome=PlanningOutcome.SOLVED,
            assignments=(
                TemporalAssignment(maneuver_id="survey", start=0, duration=4),
            ),
        )


def test_normalized_plan_transport_event_preserves_revision_and_identity() -> None:
    planner_choice = PlannerChoice(
        planning_profile="temporal",
        planner_id="minizinc",
    )
    mission_spec = MissionSpec(
        mission_id="mission-transport-1",
        objective="Survey alpha",
        planner_choice=planner_choice,
        maneuvers=(
            TemporalManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent(
                    action="survey",
                    parameters=(
                        ManeuverParameter(name="altitude_m", value=120),
                    ),
                ),
                dependencies=(),
                duration=4,
            ),
        ),
        horizon=10,
        source_authority="mission-control",
    )
    normalized_plan = TemporalPlanning(executor=SolvedExecutor()).plan(
        mission_spec=mission_spec,
        plan_revision=3,
        mission_snapshot_id="snapshot-9",
    ).normalized_plan
    assert normalized_plan.to_canonical_json() == _CANONICAL_NORMALIZED_PLAN

    event = create_normalized_plan_transport_event(
        normalized_plan,
        event_id="event-normalized-plan-3",
        sequence=19,
    )

    assert event.event_id == "event-normalized-plan-3"
    assert event.event_kind == "normalized-plan"
    assert event.contract_revision == 1
    assert event.sequence == 19
    assert event.mission_id == "mission-transport-1"
    assert event.plan_revision == 3
    assert event.outcome is PlanningOutcome.SOLVED
    assert event.payload.mission_snapshot_id == "snapshot-9"
    assert event.payload.planner_choice == planner_choice
    assert event.payload.source_authority == "mission-control"
    assert event.normalized_plan == normalized_plan
    assert event.payload.normalized_plan_document == _CANONICAL_NORMALIZED_PLAN
    assert event.normalized_plan_sha256 == _CANONICAL_NORMALIZED_PLAN_SHA256
