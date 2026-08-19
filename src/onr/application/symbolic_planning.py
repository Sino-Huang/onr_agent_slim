"""Pure symbolic planning facade."""

from __future__ import annotations

from onr.application.pddl import translate_pddl
from onr.contracts.planning import (
    NormalizedPlan,
    PlannerExecutionEvidence,
    PlanningOutcome,
    SymbolicActionCall,
    SymbolicMissionSpec,
    SymbolicPlanStep,
    SymbolicPlannerExecutionResult,
    SymbolicPlanningResult,
)
from onr.contracts.transport import (
    NormalizedPlanTransportEvent,
    create_normalized_plan_transport_event,
)
from onr.ports.planning import SymbolicPlannerExecutor


class SymbolicPlanning:
    """Translate, execute, and normalize one symbolic planning attempt."""

    def __init__(self, executor: SymbolicPlannerExecutor) -> None:
        self._executor = executor

    def plan(
        self,
        mission_spec: SymbolicMissionSpec,
        plan_revision: int,
        mission_snapshot_id: str,
    ) -> SymbolicPlanningResult:
        if mission_spec.planner_choice.planner_id is None:
            return _terminal_result(
                mission_spec,
                plan_revision,
                mission_snapshot_id,
                PlanningOutcome.UNSUPPORTED,
            )

        execution = self._executor.execute(translate_pddl(mission_spec))
        if not isinstance(execution, SymbolicPlannerExecutionResult):
            return _terminal_result(
                mission_spec,
                plan_revision,
                mission_snapshot_id,
                PlanningOutcome.ERROR,
            )

        outcome = PlanningOutcome(execution.outcome)
        steps: tuple[SymbolicPlanStep, ...] = ()
        if outcome is PlanningOutcome.SOLVED:
            joined = normalize_symbolic_actions(
                mission_spec,
                execution.action_calls,
                execution.total_plan_cost,
            )
            if joined is None:
                outcome = PlanningOutcome.ERROR
            else:
                steps = joined
        return _terminal_result(
            mission_spec,
            plan_revision,
            mission_snapshot_id,
            outcome,
            steps,
            evidence=execution.evidence,
        )

    def plan_event(
        self,
        mission_spec: SymbolicMissionSpec,
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
    mission_spec: SymbolicMissionSpec,
    plan_revision: int,
    mission_snapshot_id: str,
    outcome: PlanningOutcome,
    maneuvers: tuple[SymbolicPlanStep, ...] = (),
    *,
    evidence: PlannerExecutionEvidence | None = None,
) -> SymbolicPlanningResult:
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
    return SymbolicPlanningResult(
        outcome=outcome,
        normalized_plan=normalized_plan,
        evidence=evidence,
    )


def normalize_symbolic_actions(
    mission_spec: SymbolicMissionSpec,
    action_calls: tuple[SymbolicActionCall, ...],
    total_plan_cost: int,
) -> tuple[SymbolicPlanStep, ...] | None:
    maneuver_by_action = {
        item.maneuver_id.lower(): item for item in mission_spec.maneuvers
    }
    if len(maneuver_by_action) != len(mission_spec.maneuvers):
        return None
    if len(action_calls) != len(maneuver_by_action):
        return None

    seen: set[str] = set()
    steps: list[SymbolicPlanStep] = []
    for step_index, action_call in enumerate(action_calls):
        action_name = action_call.action.lower()
        maneuver = maneuver_by_action.get(action_name)
        if maneuver is None or action_name in seen or action_call.arguments:
            return None
        if any(dependency.lower() not in seen for dependency in maneuver.dependencies):
            return None
        seen.add(action_name)
        steps.append(
            SymbolicPlanStep(
                step_index=step_index,
                maneuver_id=maneuver.maneuver_id,
                intent=maneuver.intent,
                dependencies=maneuver.dependencies,
                cost=maneuver.cost,
            )
        )

    if sum(item.cost for item in steps) != total_plan_cost:
        return None
    return tuple(steps)
