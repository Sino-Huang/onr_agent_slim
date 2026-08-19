from __future__ import annotations

from pathlib import Path

import pytest

from onr.application.symbolic_planning import SymbolicPlanning
from onr.contracts.planning import (
    ManeuverIntent,
    PlannerChoice,
    PlannerExecutionEvidence,
    PlanningOutcome,
    SymbolicActionCall,
    SymbolicManeuver,
    SymbolicMissionSpec,
    SymbolicPlannerExecutionResult,
)


class FakeSymbolicExecutor:
    def __init__(self, result: SymbolicPlannerExecutionResult) -> None:
        self.result = result
        self.calls = 0

    def execute(self, assets: object) -> SymbolicPlannerExecutionResult:
        self.calls += 1
        return self.result


def _two_step_mission(choice: PlannerChoice | None = None) -> SymbolicMissionSpec:
    return SymbolicMissionSpec(
        mission_id="mission-1",
        objective="Survey then return",
        planner_choice=choice or PlannerChoice("symbolic", "fast-downward"),
        maneuvers=(
            SymbolicManeuver(
                maneuver_id="return-to-base",
                intent=ManeuverIntent("return-to-base"),
                dependencies=("survey",),
                cost=2,
            ),
            SymbolicManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent("survey"),
                dependencies=(),
                cost=5,
            ),
        ),
        source_authority="mission-control",
        domain_revision=3,
    )


def test_symbolic_planning_normalizes_ordered_actions_and_emits_transport() -> None:
    mission = _two_step_mission()
    executor = FakeSymbolicExecutor(
        SymbolicPlannerExecutionResult(
            outcome=PlanningOutcome.SOLVED,
            evidence=PlannerExecutionEvidence(
                artifact_directory=Path("artifacts/symbolic"),
                artifact_paths=(Path("artifacts/symbolic/domain.pddl"),),
                stdout_path=Path("artifacts/symbolic/solver.stdout"),
                stderr_path=Path("artifacts/symbolic/solver.stderr"),
            ),
            action_calls=(
                SymbolicActionCall("survey"),
                SymbolicActionCall("return-to-base"),
            ),
            total_plan_cost=7,
        )
    )
    planning = SymbolicPlanning(executor)

    result = planning.plan(mission, 4, "snapshot-8")
    event = planning.plan_event(
        mission,
        4,
        "snapshot-8",
        event_id="event-4",
        sequence=9,
    )

    assert result.outcome is PlanningOutcome.SOLVED
    assert result.evidence == executor.result.evidence
    assert tuple(
        (step.step_index, step.maneuver_id, step.cost)
        for step in result.normalized_plan.symbolic_steps
    ) == ((0, "survey", 5), (1, "return-to-base", 2))
    document = result.normalized_plan.to_canonical_json()
    assert '"start"' not in document
    assert '"duration"' not in document
    assert event.contract_revision == 1
    assert event.payload.source_authority == "mission-control"
    assert event.payload.normalized_plan_document == document
    assert executor.calls == 2


def test_unsupported_symbolic_choice_does_not_call_executor() -> None:
    mission = _two_step_mission(PlannerChoice.unsupported_symbolic())
    executor = FakeSymbolicExecutor(
        SymbolicPlannerExecutionResult(PlanningOutcome.SOLVED, (), 0)
    )

    result = SymbolicPlanning(executor).plan(
        mission, 1, "snapshot-unsupported-symbolic"
    )

    assert result.outcome is PlanningOutcome.UNSUPPORTED
    assert result.normalized_plan.maneuvers == ()
    assert executor.calls == 0


@pytest.mark.parametrize(
    ("calls", "cost"),
    (
        ((SymbolicActionCall("survey"),), 5),
        ((SymbolicActionCall("survey"), SymbolicActionCall("survey")), 10),
        ((SymbolicActionCall("survey"), SymbolicActionCall("unexpected")), 7),
        (
            (
                SymbolicActionCall("return-to-base"),
                SymbolicActionCall("survey"),
            ),
            7,
        ),
        (
            (
                SymbolicActionCall("survey"),
                SymbolicActionCall("return-to-base"),
            ),
            8,
        ),
    ),
)
def test_symbolic_planning_rejects_invalid_solved_plans(
    calls: tuple[SymbolicActionCall, ...], cost: int
) -> None:
    executor = FakeSymbolicExecutor(
        SymbolicPlannerExecutionResult(
            PlanningOutcome.SOLVED,
            action_calls=calls,
            total_plan_cost=cost,
        )
    )

    result = SymbolicPlanning(executor).plan(
        _two_step_mission(), 2, "snapshot-invalid"
    )

    assert result.outcome is PlanningOutcome.ERROR
    assert result.normalized_plan.maneuvers == ()
