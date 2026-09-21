"""joint34 PDDL golden tests: real Fast Downward + VAL on materialized problems."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onr.adapters.fast_downward import FastDownwardExecutor
from onr.adapters.val import VALPlanValidator
from onr.application import joint34_planning
from onr.application.mission3_planning import Mission3AdaptivePlanner, Mission3ReplanGate
from onr.application.mission4_planning import Mission4AdaptivePlanner
from onr.application.mission2_planning import Mission2ReplanGate
from onr.contracts.fsm import FSMStatus
from onr.contracts.planning import PlanningOutcome
from onr.runtime.cli import _mission_mode_context

_REPO = Path(__file__).resolve().parents[1]
_FD = _REPO / "modules/downward/fast-downward.py"
_VAL = _REPO / "modules/VAL/build/linux64/Release/bin/Validate"


def _ship(ship_id: int, x: float, y: float, *, status: str = "unresolved") -> dict:
    return {
        "ship_id": ship_id,
        "screening": {"status": "unobserved", "usable_observation_count": 0},
        "investigation": {"status": "unobserved", "usable_observation_count": 0},
        "evidence_ids": [],
        "usable_view_evidence_ids": [],
        "suspected_object_evidence_ids": [],
        "perception_status": "unobserved",
        "verdict": None,
        "resolution": {"status": status, "incomplete_reason": None, "marked_at_s": None},
    }


def _observation(ship_id: int, x: float, y: float, now: float = 0.0) -> dict:
    return {
        "ship_id": ship_id,
        "sampled_at_s": now,
        "age_s": 0.0,
        "source": "gps",
        "estimated_position": {"x": x, "y": y, "z": 0.0},
        "estimated_speed_mps": 0.8,
        "estimated_heading_degrees": 90.0,
    }


def _environment(*, m3_ships: tuple[tuple[int, float, float], ...], m4_deadline_s: float, now: float = 0.0):
    ships = [_ship(ship_id, x, y) for ship_id, x, y in m3_ships]
    observations = [
        _observation(ship_id, x, y, now) for ship_id, x, y in m3_ships
    ]
    return {
        "mission_id": "joint34-mission",
        "mission_time_seconds": now,
        "controlled_vehicle": {
            "entity_id": "drone",
            "position": {"x": 0.0, "y": 0.0, "z": -25.0},
            "max_velocity": 8.0,
            "fov_radius": 100.0,
        },
        "world_model_info": {
            "mission_mode": "joint34",
            "mission_end_time_s": 120.0,
            "visible_ship_ids": [],
            "visible_ships": [],
            "ship_event_reports": {},
            "event_report_checks": [],
            "mission3": {
                "schema_version": 1,
                "selected_ship_ids": [ship_id for ship_id, _, _ in m3_ships],
                "target_observations": observations,
                "target_observation_max_age_s": 7.5,
                "ships": ships,
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
                        "dock": {
                            "polygon": [
                                [-20.0, -20.0],
                                [20.0, -20.0],
                                [20.0, 20.0],
                                [-20.0, 20.0],
                            ],
                            "prior": 1.0,
                        }
                    },
                    "found_threshold": 0.1,
                    "mission_time_budget_s": 300.0,
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
    for order in ((), ("m3",), ("m4",), ("m3", "m4"), ("m4", "m3")):
        deferred = tuple(mission for mission in ("m3", "m4") if mission not in order)
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
    environment = _environment(m3_ships=((1, 960.0, 0.0),), m4_deadline_s=300.0)
    assert joint34_planning.joint34_revision_class(environment, ()) == "routine"
    # An M4 trigger whose ledger deadline falls inside the preemptive window.
    preemptive = _environment(m3_ships=((1, 960.0, 0.0),), m4_deadline_s=45.0)
    assert (
        joint34_planning.joint34_revision_class(preemptive, ("mission4-gate:{\"deadline_s\": 45}",))
        == "preemptive"
    )
    assert joint34_planning.joint34_revision_class(
        preemptive, ("mission3-gate:adaptive_tour:navigate:1",)
    ) == "preemptive"
    preemptive["world_model_info"]["mission4"]["status"] = "completed"
    assert joint34_planning.joint34_revision_class(
        preemptive, ("mission3-gate:adaptive_tour:navigate:1",)
    ) == "routine"
    # Fresh inconclusive evidence and a moved target are preemptive; a routine
    # adaptive-tour trigger is not.
    assert (
        joint34_planning.joint34_revision_class(
            environment, ("mission3-gate:new_inconclusive_evidence:investigate:1",)
        )
        == "preemptive"
    )
    assert (
        joint34_planning.joint34_revision_class(
            environment, ("mission3-gate:target_moved:navigate:1",)
        )
        == "preemptive"
    )
    assert (
        joint34_planning.joint34_revision_class(
            environment, ("mission3-gate:adaptive_tour:navigate:1",)
        )
        == "routine"
    )


def test_revision_class_clamps_deadline_to_run_bound() -> None:
    # The ledger deadline (package budget 300 s) lies past the 120 s run
    # bound; within 60 s of the bound the effective deadline is imminent and
    # the revision flips preemptive, exactly like the M4 planner's clamp.
    environment = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=300.0)
    early = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=300.0, now=40.0)
    triggers = ("mission4-gate:{\"deadline_s\": 300}",)
    assert joint34_planning.joint34_revision_class(early, triggers) == "routine"
    late = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=300.0, now=70.0)
    assert joint34_planning.joint34_revision_class(late, triggers) == "preemptive"


@pytest.mark.parametrize(
    ("m3_ship", "m4_site", "m4_deadline_s", "expected_order", "expected_deferred"),
    (
        # Golden case: routine M3 inspection (120 s travel away) plus a
        # preemptive M4 request (deadline in 45 s). The preemptive defer price
        # dominates every serve cost at this scale, so both missions are
        # served and the urgent M4 goes first.
        ((1, 960.0, 0.0), (160.0, 0.0), 45.0, ["m4", "m3"], []),
        # Both routine with a far M4: deferring both beats serving either.
        ((1, 60.0, 0.0), (2400.0, 0.0), 300.0, [], ["m3", "m4"]),
    ),
)
def test_materialize_solve_and_validate(
    m3_ship, m4_site, m4_deadline_s, expected_order, expected_deferred, tmp_path
) -> None:
    environment = _environment(m3_ships=(m3_ship,), m4_deadline_s=m4_deadline_s)
    triggers = (
        ("mission3-gate:adaptive_tour:navigate:1;mission4-gate:{\"deadline_s\": %g}" % m4_deadline_s,) if m4_deadline_s <= 60.0 else ()
    )
    domain_path, problem_path, schedule = joint34_planning.materialize_joint34_pddl(
        environment,
        None,
        _decision(*m4_site),
        tmp_path,
        trigger_identities=triggers,
    )
    plan_text = _solve(domain_path, problem_path, tmp_path / "fd")
    order, deferred = joint34_planning.joint34_plan_order(plan_text)
    assert order == expected_order
    assert sorted(deferred) == sorted(expected_deferred)
    # Served order and deferrals must be cost-minimal for the encoded problem.
    assert _plan_cost(schedule, tuple(order), tuple(deferred)) == _optimal_costs(schedule)[0]


def test_navigation_grounding_changes_validated_mission_order(tmp_path) -> None:
    environment = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=45.0)
    domain, problem, _ = joint34_planning.materialize_joint34_pddl(
        environment,
        None,
        {"action": "navigate", "parameters": {"x": 2400.0, "y": 0.0}},
        tmp_path,
        trigger_identities=("mission4-gate:request",),
    )
    plan = _solve(domain, problem, tmp_path / "solver")
    assert joint34_planning.joint34_plan_order(plan) == (["m3", "m4"], [])


def test_emit_statechart_blocks_follow_plan(tmp_path) -> None:
    environment = _environment(m3_ships=((1, 960.0, 0.0),), m4_deadline_s=45.0)

    _, _, schedule = joint34_planning.materialize_joint34_pddl(
        environment,
        None,
        _decision(160.0, 0.0),
        tmp_path,
        trigger_identities=("mission4-gate:{\"deadline_s\": 45}",),
    )
    chart_path = tmp_path / "statechart.json"
    # Serve M4 then M3 in plan order, matching the preemptive optimal schedule.
    plan = tmp_path / "sas_plan"
    plan.write_text(
        "0: (serve-m4-from-drone) [80000]\n1: (serve-m3-from-m4-site) [106875]\n; cost = 186875 (general cost)\n",
        encoding="utf-8",
    )
    joint34_planning.emit_joint34_statechart(schedule, plan, chart_path)
    chart = json.loads(chart_path.read_text(encoding="utf-8"))
    assert chart["entry_state"] == "scheduling"
    assert chart["states"] == ["scheduling", "mission4-block", "mission3-block", "joint34-complete"]
    assert chart["terminal_states"] == ["joint34-complete"]
    # Mission 3 shape: the block context carries the Mission 3 decision.
    m3_context = chart["state_context"]["mission3-block"]
    assert m3_context["planner_item"] == json.loads(
        json.dumps(schedule["mission3_decision"])
    )
    transitions = {(t["source"], t["target"]): t for t in chart["transitions"]}
    # Mission 3/4 entries never route through the Mission 1/2 deterministic
    # entry dispatch; scheduling commits them immediately via a deterministic
    # not_before bound.
    m4_entry = transitions[("scheduling", "mission4-block")]
    assert m4_entry["context"]["readiness"] == {"not_before": {"seconds": 0.0}}
    m3_done = transitions[("mission3-block", "joint34-complete")]
    assert m3_done["context"]["readiness"] == {
        "not_before": {"seconds": schedule["mission_end_time_s"]},
        "mission_time_at_or_after": {"seconds": schedule["mission_end_time_s"]},
    }
    # Both blocks complete on terminal maneuver feedback (maneuver-serving
    # decisions).
    m4_done = transitions[("mission4-block", "mission3-block")]
    assert m4_done["context"]["readiness"] == {"matching_maneuver_lifecycle_terminal": True}

    # A M3-first handoff is time-bounded by the same not_before rule.
    m3_first_plan = tmp_path / "m3_first_plan"
    m3_first_plan.write_text(
        "0: (serve-m3-from-drone) [126875]\n1: (serve-m4-from-m3-site) [156875]\n; cost = 283750 (general cost)\n",
        encoding="utf-8",
    )
    m3_first_chart_path = tmp_path / "m3_first_statechart.json"
    joint34_planning.emit_joint34_statechart(schedule, m3_first_plan, m3_first_chart_path)
    m3_first_chart = json.loads(m3_first_chart_path.read_text(encoding="utf-8"))
    m3_first_transitions = {
        (t["source"], t["target"]): t for t in m3_first_chart["transitions"]
    }
    assert m3_first_transitions[("scheduling", "mission3-block")]["context"]["readiness"] == {
        "not_before": {"seconds": 0.0}
    }
    budget_exit = next(
        transition for transition in m3_first_chart["transitions"]
        if transition["event"] == "mission3-budget-exhausted"
    )
    assert budget_exit["source"] == "mission3-block"
    assert budget_exit["target"] == "mission4-block"
    assert budget_exit["context"]["readiness"]["mission_time_at_or_after"] == {"seconds": 120.0}

    # A deferred mission's block is omitted.
    defer_plan = tmp_path / "defer_plan"
    defer_plan.write_text(
        "0: (serve-m4-from-drone) [80000]\n1: (defer-m3) [100000]\n", encoding="utf-8"
    )
    defer_chart_path = tmp_path / "defer_statechart.json"
    joint34_planning.emit_joint34_statechart(schedule, defer_plan, defer_chart_path)
    defer_chart = json.loads(defer_chart_path.read_text(encoding="utf-8"))
    assert defer_chart["states"] == ["scheduling", "mission4-block", "joint34-complete"]


def test_mission3_report_block_completes_immediately(tmp_path) -> None:
    environment = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=45.0)
    _, _, schedule = joint34_planning.materialize_joint34_pddl(
        environment,
        {
            "action": "report",
            "entity_id": None,
            "parameters": {},
            "reason": "all_resolved",
            "report": {"schema_version": 1},
        },
        _decision(160.0, 0.0),
        tmp_path,
        trigger_identities=("mission4-gate:{\"deadline_s\": 45}",),
    )
    plan = tmp_path / "sas_plan"
    plan.write_text(
        "0: (serve-m3-from-drone) [126875]\n1: (serve-m4-from-m3-site) [156875]\n; cost = 283750 (general cost)\n",
        encoding="utf-8",
    )
    chart_path = tmp_path / "statechart.json"
    joint34_planning.emit_joint34_statechart(schedule, plan, chart_path)
    chart = json.loads(chart_path.read_text(encoding="utf-8"))
    transitions = {(t["source"], t["target"]): t for t in chart["transitions"]}
    # A Mission 3 report decision completes its block without waiting for
    # maneuver feedback: there is no maneuver to terminate.
    done = transitions[("mission3-block", "mission4-block")]
    assert done["context"]["readiness"] == {"not_before": {"seconds": 0.0}}
    # The chart still terminates only at the run bound.
    bound = transitions[("mission4-block", "joint34-complete")]
    assert bound["context"]["readiness"] == {
        "not_before": {"seconds": schedule["mission_end_time_s"]},
        "mission_time_at_or_after": {"seconds": schedule["mission_end_time_s"]},
    }


def test_mission3_planner_tolerates_other_mission_maneuver() -> None:
    environment = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=300.0)
    # The joint34 scheduler serves the Mission 4 half first: an in-flight
    # search command targets no selected ship. The inspection planner must
    # keep deciding normally instead of asserting on the foreign maneuver.
    environment["maneuver_lifecycle"] = {
        "command_id": "command-9",
        "action": "search_area",
        "lifecycle": "active",
        "parameters": {"polygon": [{"x": -20.0, "y": -20.0}, {"x": 20.0, "y": 20.0}]},
    }
    decision = Mission3AdaptivePlanner().decide(environment)
    assert decision is not None
    assert decision.action in {"navigate", "pursue"}


def test_m4_request_replans_while_m3_maneuver_is_active() -> None:
    environment = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=60.0)
    planner = Mission4AdaptivePlanner("joint34-mission")
    first = planner.decide(environment)
    assert first is not None and first.action == "search_area"
    environment["mission_time_seconds"] = 20.0
    environment["maneuver_lifecycle"] = {
        "command_id": "m3-navigation",
        "action": "navigate",
        "lifecycle": "active",
        "parameters": {"x": 60.0, "y": 0.0, "z": -25.0},
    }
    search = environment["world_model_info"]["mission4"]
    search["revision"] += 1
    search["objectives"]["worker:3"] = {
        **search["objectives"]["worker:1"], "description": "another red container"
    }
    decision = planner.decide(environment)
    assert decision is not None
    assert set(decision.target_ids) == {"worker:1", "worker:3"}


def test_mode_guards_active_under_joint34() -> None:
    environment = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=300.0)
    world = environment["world_model_info"]
    mode, roster = _mission_mode_context(world)
    assert mode == "joint34"
    assert roster == ()
    # The Mission 3 replan gate activates under joint34 instead of rejecting.
    trigger = Mission3ReplanGate().assess(environment)
    assert trigger is not None
    assert trigger.startswith("mission3-gate:")
    # The Mission 4 adaptive planner decides under joint34 instead of rejecting.
    decision = Mission4AdaptivePlanner("joint34-mission").decide(environment)
    assert decision is not None
    # No Mission 2 half: the collision gate rejects as out of mission.
    status = FSMStatus(
        mission_id="joint34-mission",
        plan_revision=1,
        statechart_revision=1,
        active_state="scheduling",
    )
    assert Mission2ReplanGate().assess(environment, status).reason == "not_mission2"


def test_joint34_reports_unresolved_at_run_bound() -> None:
    environment = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=900.0, now=100.0)
    planner = Mission4AdaptivePlanner("joint34-mission")
    # A heartbeat before the run bound keeps searching.
    first = planner.decide(environment)
    assert first is None or first.action != "report"
    # The M3 budget ends the run with an unchanged ledger: the signature alone
    # must not suppress the explicit-unresolved report owed at the bound.
    environment["mission_time_seconds"] = 120.0
    decision = Mission4AdaptivePlanner("joint34-mission").decide(environment)
    assert decision is not None
    assert decision.action == "report"
    # The run-bound ending uses the ledger's explicit-unresolved vocabulary so
    # the worker accepts the report and files its finish request.
    assert decision.reason == "search_exhausted"

    # Mission 4 standalone ignores the run bound and follows its own worker
    # deadline.
    environment["world_model_info"]["mission_mode"] = "mission4"
    standalone = Mission4AdaptivePlanner("joint34-mission").decide(environment)
    assert standalone is None or standalone.action != "report"


def test_joint34_gates_use_snapshot_authorized_worker_revision() -> None:
    from copy import deepcopy
    from types import SimpleNamespace
    from test_mission2_closed_loop_gate import (
        _CoordinationHarness, _Environment, _Hyper, _active_revision,
    )
    from onr.application.mission4_planning import decision_from_trigger

    captured = _environment(m3_ships=((1, 60.0, 0.0),), m4_deadline_s=60.0)
    unpublished = deepcopy(captured)
    search = unpublished["world_model_info"]["mission4"]
    search["revision"] = 2
    search["objectives"] = {"worker:future": search["objectives"]["worker:1"]}

    class AheadOfSnapshot(_Environment):
        def planning_view(self):
            return SimpleNamespace(environment_event=SimpleNamespace(payload=unpublished))

    hyper = _Hyper()
    coordinator = _CoordinationHarness(AheadOfSnapshot(captured), hyper)
    coordinator.run(_active_revision(1))
    decisions = [
        decision
        for invocation in hyper.invocations
        for trigger in invocation.trigger_identities
        if (decision := decision_from_trigger(trigger)) is not None
    ]
    assert [decision.target_ids for decision in decisions] == [("worker:1",)]
