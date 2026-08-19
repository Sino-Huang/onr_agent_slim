"""Pure temporal planning facade."""

from __future__ import annotations

from onr.application.minizinc import translate_minizinc
from onr.contracts.planning import (
    MissionSpec,
    NormalizedPlan,
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlanningOutcome,
    ScheduledManeuver,
    TemporalAssignment,
    TemporalPlanningResult,
)
from onr.contracts.transport import (
    NormalizedPlanTransportEvent,
    create_normalized_plan_transport_event,
)
from onr.ports.planning import TemporalPlannerExecutor


class TemporalPlanning:
    """Translate, execute, and normalize one temporal planning attempt."""

    def __init__(self, executor: TemporalPlannerExecutor) -> None:
        self._executor = executor

    def plan(
        self,
        mission_spec: MissionSpec,
        plan_revision: int,
        mission_snapshot_id: str,
    ) -> TemporalPlanningResult:
        execution = self._executor.execute(translate_minizinc(mission_spec))
        if not isinstance(execution, PlannerExecutionResult):
            return _terminal_result(
                mission_spec,
                plan_revision,
                mission_snapshot_id,
                PlanningOutcome.ERROR,
            )

        outcome = PlanningOutcome(execution.outcome)
        maneuvers: tuple[ScheduledManeuver, ...] = ()
        if outcome is PlanningOutcome.SOLVED:
            joined = normalize_temporal_assignments(mission_spec, execution.assignments)
            if joined is None:
                outcome = PlanningOutcome.ERROR
            else:
                maneuvers = joined

        return _terminal_result(
            mission_spec,
            plan_revision,
            mission_snapshot_id,
            outcome,
            maneuvers,
            evidence=execution.evidence,
        )

    def plan_event(
        self,
        mission_spec: MissionSpec,
        plan_revision: int,
        mission_snapshot_id: str,
        *,
        event_id: str,
        sequence: int,
    ) -> NormalizedPlanTransportEvent:
        result = self.plan(mission_spec, plan_revision, mission_snapshot_id)
        if result.normalized_plan is None:
            raise ValueError("only a solved result can be published as a Normalized Plan")
        return create_normalized_plan_transport_event(
            result.normalized_plan,
            event_id=event_id,
            sequence=sequence,
        )


def _terminal_result(
    mission_spec: MissionSpec,
    plan_revision: int,
    mission_snapshot_id: str,
    outcome: PlanningOutcome,
    maneuvers: tuple[ScheduledManeuver, ...] = (),
    *,
    evidence: PlannerExecutionEvidence | None = None,
) -> TemporalPlanningResult:
    normalized_plan = None
    if outcome is PlanningOutcome.SOLVED:
        normalized_plan = NormalizedPlan(
            mission_spec=mission_spec,
            plan_revision=plan_revision,
            mission_snapshot_id=mission_snapshot_id,
            planner_choice=mission_spec.planner_choice,
            outcome=outcome,
            maneuvers=maneuvers,
        )
    return TemporalPlanningResult(
        outcome=outcome,
        normalized_plan=normalized_plan,
        evidence=evidence,
    )


def normalize_temporal_assignments(
    mission_spec: MissionSpec,
    assignments: tuple[TemporalAssignment, ...],
) -> tuple[ScheduledManeuver, ...] | None:
    assignment_by_id = {item.maneuver_id: item for item in assignments}
    maneuver_by_id = {item.maneuver_id: item for item in mission_spec.maneuvers}
    if len(assignment_by_id) != len(assignments):
        return None
    if set(assignment_by_id) != set(maneuver_by_id):
        return None

    scheduled = []
    for maneuver_id, maneuver in maneuver_by_id.items():
        assignment = assignment_by_id[maneuver_id]
        if assignment.duration != maneuver.duration:
            return None
        if assignment.start + assignment.duration > mission_spec.horizon:
            return None
        scheduled.append(
            ScheduledManeuver(
                maneuver_id=maneuver_id,
                intent=maneuver.intent,
                dependencies=maneuver.dependencies,
                start=assignment.start,
                duration=assignment.duration,
            )
        )

    scheduled_by_id = {item.maneuver_id: item for item in scheduled}
    if any(
        scheduled_by_id[item.maneuver_id].start
        < scheduled_by_id[dependency].start
        + scheduled_by_id[dependency].duration
        for item in mission_spec.maneuvers
        for dependency in item.dependencies
    ):
        return None
    return tuple(scheduled)
