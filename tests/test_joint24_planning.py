"""joint24 PDDL golden tests: real Fast Downward + VAL on materialized problems."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onr.adapters.fast_downward import FastDownwardExecutor
from onr.adapters.val import VALPlanValidator
from onr.application import joint24_planning
from onr.application.mission2_planning import Mission2ReplanGate
from onr.application.mission4_planning import Mission4AdaptivePlanner
from onr.contracts.fsm import FSMStatus
from onr.contracts.planning import PlanningOutcome
from onr.runtime.cli import _mission_mode_context

_REPO = Path(__file__).resolve().parents[1]
_FD = _REPO / "modules/downward/fast-downward.py"
_VAL = _REPO / "modules/VAL/build/linux64/Release/bin/Validate"

def _environment(*, m2_point: tuple[float, float, float], m4_deadline_s: float, now: float = 0.0):
    x, y, at = m2_point
    return {
        "mission_id": "joint24-mission",
        "mission_time_seconds": now,
        "controlled_vehicle": {
            "entity_id": "drone",
            "position": {"x": 0.0, "y": 0.0, "z": -25.0},
            "max_velocity": 8.0,
            "fov_radius": 100.0,
        },
        "world_model_info": {
            "mission_mode": "joint24",
            "mission_end_time_s": 900.0,
            "visible_ship_ids": [],
            "visible_ships": [],
            "ship_event_reports": [],
            "event_report_checks": [],
            "perception_predictions": {
                "schema_version": 1,
                "status": "ready",
                "valid_until_s": now + 5.0,
                "run_id": "run-1",
                "sequence": 1,
                "source": "simulated",
                "trajectories": {
                    "1": {
                        "ready": True,
                        "sampled_at_s": now,
                        "points": [{"time_s": at, "position": {"x": x, "y": y, "z": 0.0}}],
                    },
                    "2": {"ready": False, "sampled_at_s": now, "points": []},
                },
                "alerts": [],
                "active_pairs": [
                    {
                        "ship_ids": [1, 2],
                        "predicted_contact_at_s": at,
                        "probability": None,
                        "position": {"x": x, "y": y, "z": 0.0},
                    }
                ],
            },
            "mission4": {
                "schema_version": 1,
                "status": "active",
                "revision": 1,
                "deadline_s": m4_deadline_s,
                "objectives": {
                    "worker:1": {
                        "description": "red container at the dock",
                        "attributes": {"type": "container", "color": "red"},
                        "area_ids": ["dock"],
                    }
                },
                "requests": [],
                "observations": [],
                "coverage": {},
                "package": {
                    "schema_version": 1,
                    "vocabulary": {"type": ["container"], "color": ["red"]},
                    "areas": {
                        "dock": {"polygon": [[-20.0, -20.0], [20.0, -20.0], [20.0, 20.0], [-20.0, 20.0]], "prior": 1.0}
                    },
                    "found_threshold": 0.1,
                    "mission_time_budget_s": 900.0,
                    "obstacles": [],
                    "keep_out_zones": [],
                },
            },
        },
        "maneuver_lifecycle": None,
    }


def _decision(x: float, y: float) -> dict:
    return {
        "action": "investigate",
        "reason": "worker:1",
        "parameters": {"target": {"x": x, "y": y, "z": -25.0}},
        "target_ids": ["worker:1"],
    }


def _plan_cost(schedule, order: tuple[str, ...], deferred: tuple[str, ...]) -> float:
    """Cost of one serve-order/defer combination as the PDDL problem encodes it."""
    costs = schedule["costs"]
    total = 0.0
    site = "drone"
    for mission in order:
        total += costs[f"serve-{mission}-from-{site}-cost"]
        site = f"{mission}-site"
    for mission in deferred:
        total += costs[f"defer-cost {mission}"]
    return total


def _optimal_costs(schedule) -> list[float]:
    """Enumerate every serve-order/defer combination the PDDL problem encodes."""
    candidates = []
    for order in ((), ("m2",), ("m4",), ("m2", "m4"), ("m4", "m2")):
        deferred = tuple(mission for mission in ("m2", "m4") if mission not in order)
        candidates.append(_plan_cost(schedule, order, deferred))
    return sorted(candidates)


def _solve(domain: Path, problem: Path, tmp_path: Path):
    executor = FastDownwardExecutor(executable=_FD, artifact_root=tmp_path, timeout_seconds=60.0)
    result = executor.execute(
        {
            "domain.pddl": domain.read_bytes(),
            "problem.pddl": problem.read_bytes(),
        }
    )
    assert result.outcome is PlanningOutcome.SOLVED
    plan_path = next(path for path in result.evidence.artifact_paths if path.name == "sas_plan")
    plan_text = plan_path.read_text(encoding="utf-8")
    assert VALPlanValidator(executable=_VAL).validate(result.evidence), plan_text
    return plan_text


def test_revision_class_mapping() -> None:
    environment = _environment(m2_point=(65.0, 0.0, 5.0), m4_deadline_s=900.0)
    assert joint24_planning.joint24_revision_class(environment, ()) == "routine"
    preemptive = _environment(m2_point=(65.0, 0.0, 5.0), m4_deadline_s=45.0)
    assert (
        joint24_planning.joint24_revision_class(preemptive, ("mission4-gate:{\"deadline_s\": 45}",))
        == "preemptive"
    )
    assert (
        joint24_planning.joint24_revision_class(environment, ("mission2-gate:new_warning:collision-risk",))
        == "preemptive"
    )


@pytest.mark.parametrize(
    ("m2_point", "m4_site", "m4_deadline_s", "expected_order", "expected_deferred"),
    (
        # Preemptive (M4 deadline 45 s): serving the close urgent M4 first then the
        # routine M2 is strictly cheaper than any deferral.
        ((257.0, 0.0, 29.0), (160.0, 0.0), 45.0, ["m4", "m2"], []),
        # Routine with a far M4: deferring both beats serving either.
        ((65.0, 0.0, 5.0), (2400.0, 0.0), 900.0, [], ["m2", "m4"]),
    ),
)
def test_materialize_solve_and_validate(
    m2_point, m4_site, m4_deadline_s, expected_order, expected_deferred, tmp_path
) -> None:
    environment = _environment(m2_point=m2_point, m4_deadline_s=m4_deadline_s)
    triggers = (
        ("mission4-gate:{\"deadline_s\": %g}" % m4_deadline_s,) if m4_deadline_s <= 60.0 else ()
    )
    domain_path, problem_path, schedule = joint24_planning.materialize_joint24_pddl(
        environment,
        None,
        _decision(*m4_site),
        tmp_path,
        trigger_identities=triggers,
    )
    plan_text = _solve(domain_path, problem_path, tmp_path / "fd")
    order, deferred = joint24_planning.joint24_plan_order(plan_text)
    assert order == expected_order
    assert sorted(deferred) == sorted(expected_deferred)
    # Served order and deferrals must be cost-minimal for the encoded problem.
    assert _plan_cost(schedule, tuple(order), tuple(deferred)) == _optimal_costs(schedule)[0]


def test_emit_statechart_blocks_follow_plan(tmp_path) -> None:
    environment = _environment(m2_point=(257.0, 0.0, 29.0), m4_deadline_s=45.0)

    _, _, schedule = joint24_planning.materialize_joint24_pddl(
        environment,
        None,
        _decision(160.0, 0.0),
        tmp_path,
        trigger_identities=("mission4-gate:{\"deadline_s\": 45}",),
    )
    chart_path = tmp_path / "statechart.json"
    # Serve M4 then M2 in plan order, matching the preemptive optimal schedule.
    plan = tmp_path / "sas_plan"
    plan.write_text(
        "0: (serve-m4-from-drone) [80000]\n1: (serve-m2-from-m4-site) [13125]\n; cost = 93125 (general cost)\n",
        encoding="utf-8",
    )
    joint24_planning.emit_joint24_statechart(schedule, plan, chart_path)
    chart = json.loads(chart_path.read_text(encoding="utf-8"))
    assert chart["entry_state"] == "scheduling"
    assert chart["states"] == ["scheduling", "mission4-block", "mission2-block", "joint24-complete"]
    assert chart["terminal_states"] == ["joint24-complete"]
    # Mission 2 shape: block completion follows the observation window.
    m2_context = chart["state_context"]["mission2-block"]
    assert m2_context["candidate_id"] == schedule["selected_m2_candidate"]["candidate_id"]
    assert m2_context["observation_window"]["end_s"] == schedule["selected_m2_candidate"]["end_s"]
    transitions = {(t["source"], t["target"]): t for t in chart["transitions"]}
    m2_done = transitions[("mission2-block", "joint24-complete")]
    assert m2_done["context"]["readiness"] == {
        "mission_time_at_or_after": {"seconds": schedule["mission_end_time_s"]}
    }
    # Mission 4 shape: block completion follows terminal maneuver feedback.
    m4_done = transitions[("mission4-block", "mission2-block")]
    assert m4_done["context"]["readiness"] == {"matching_maneuver_lifecycle_terminal": True}

    # A deferred mission's block is omitted.
    defer_plan = tmp_path / "defer_plan"
    defer_plan.write_text("0: (serve-m4-from-drone) [80000]\n1: (defer-m2) [100000]\n", encoding="utf-8")
    defer_chart_path = tmp_path / "defer_statechart.json"
    joint24_planning.emit_joint24_statechart(schedule, defer_plan, defer_chart_path)
    defer_chart = json.loads(defer_chart_path.read_text(encoding="utf-8"))
    assert defer_chart["states"] == ["scheduling", "mission4-block", "joint24-complete"]


def test_mode_guards_active_under_joint24() -> None:
    environment = _environment(m2_point=(65.0, 0.0, 5.0), m4_deadline_s=900.0)
    world = environment["world_model_info"]
    mode, ship_ids = _mission_mode_context(world)
    assert mode == "joint24"
    assert ship_ids == (1, 2)
    # The Mission 2 replan gate activates under joint24 instead of rejecting.
    status = FSMStatus(
        mission_id="joint24-mission",
        plan_revision=1,
        statechart_revision=1,
        active_state="collision-monitoring-active",
    )
    assert Mission2ReplanGate().assess(environment, status).reason != "not_mission2"
    # The Mission 4 adaptive planner decides under joint24 instead of rejecting.
    decision = Mission4AdaptivePlanner("joint24-mission").decide(environment)
    assert decision is not None
