from __future__ import annotations

import pytest

from onr.application.pddl import translate_pddl
from onr.contracts.planning import (
    ManeuverIntent,
    PlannerChoice,
    SymbolicManeuver,
    SymbolicMissionSpec,
)


def _mission(maneuvers: tuple[SymbolicManeuver, ...]) -> SymbolicMissionSpec:
    return SymbolicMissionSpec(
        mission_id="mission-12",
        objective="Complete all declared maneuvers",
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        maneuvers=maneuvers,
        source_authority="mission-control",
        domain_revision=4,
    )


def test_pddl_translation_is_deterministic_and_declares_action_cost_semantics() -> None:
    prepare = SymbolicManeuver("prepare", ManeuverIntent("prepare"), (), 2)
    inspect = SymbolicManeuver("inspect", ManeuverIntent("inspect"), (), 3)
    return_home = SymbolicManeuver(
        "return-home",
        ManeuverIntent("return-home"),
        ("inspect", "prepare"),
        5,
    )

    first = translate_pddl(_mission((return_home, inspect, prepare)))
    second = translate_pddl(
        _mission(
            (
                prepare,
                SymbolicManeuver(
                    "return-home",
                    ManeuverIntent("return-home"),
                    ("prepare", "inspect"),
                    5,
                ),
                inspect,
            )
        )
    )

    assert dict(first) == dict(second)
    assert tuple(first) == ("domain.pddl", "problem.pddl")
    domain = first["domain.pddl"].decode("ascii")
    problem = first["problem.pddl"].decode("ascii")
    assert "; ONR symbolic domain revision 4" in domain
    assert "(domain onr-symbolic-r4)" in domain
    assert "(:requirements :strips :action-costs)" in domain
    assert "(completed-inspect)" in domain
    assert "(completed-prepare)" in domain
    assert "(increase (total-cost) 5)" in domain
    assert ":precondition (and (completed-inspect) (completed-prepare))" in domain
    assert "(:domain onr-symbolic-r4)" in problem
    assert "(:metric minimize (total-cost))" in problem
    with pytest.raises(TypeError):
        first["extra.pddl"] = b"not mutable"  # type: ignore[index]
