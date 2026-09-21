"""Joint34 (Mission 3 + Mission 4) three-tier mission scheduling.

Code-owned combo seam. The per-mission middle tiers stay the within-mission
movement authority: Mission 3's adaptive inspection decision (nearest-feasible
screening with evidence-priority investigation, plus its MiniZinc receipt
model) and Mission 4's adaptive planner decision are the grounding inputs.
This module encodes both missions' pending work, deadlines and switch costs as
a small STRIPS + action-costs PDDL problem per plan revision; Fast Downward
(astar(lmcut())) produces the mission ordering and VAL validates it. The
checked-in domain under
``conf/skills/hyper/creating-pddl-problem-files/examples/joint34-scheduler/``
is submitted unchanged; only the generated problem file carries numbers.

Scheduling policy: priority classes + bounded preemption. A revision is
preemptive-class when a Mission 4 request deadline falls within
``JOINT34_PREEMPTIVE_DEADLINE_S``, or a Mission 3 trigger reports fresh
inconclusive evidence or a moved target; otherwise it is routine. Deferring a
preemptive revision's work costs ``DEFER_COST_PREEMPTIVE`` — priced above any
feasible travel at the harbor scene's kilometer scale (the Mission 2 selection
model's 100000 weight scale raised accordingly) — so urgency always outranks
travel; routine deferral costs ``DEFER_COST_ROUTINE``, letting the solver skip
a low-urgency mission when switch travel outweighs it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from onr.application.mission3_planning import Mission3AdaptivePlanner
from onr.application.mission4_planning import Mission4AdaptivePlanner

JOINT34_PREEMPTIVE_DEADLINE_S = 60.0
DEFER_COST_PREEMPTIVE = 1000000
DEFER_COST_ROUTINE = 1000
JOINT34_M4_SERVICE_MILLIS = 60000
# Mission 3 block service estimates follow the decision's maneuver step:
# approach/screening dwell for navigation and pursuit, one bounded
# investigation orbit for investigation — same scale as the Mission 4
# service constant.
_JOINT34_M3_SERVICE_MILLIS = {
    "navigate": 10000,
    "pursue": 20000,
    "investigate": 30000,
    "report": 0,
}

DOMAIN_REFERENCE = (
    Path(__file__).resolve().parents[3]
    / "conf/skills/hyper/creating-pddl-problem-files/examples/joint34-scheduler/domain.pddl"
)

_SCHEDULE_FILE = "joint34-schedule.json"
_ACTION_RE = re.compile(r"^(?:\d+(?:\.\d+)?\s*:\s*)?\(([^)]+)\)")


def _mission3_trigger_reasons(trigger_identities: Sequence[str]) -> frozenset[str]:
    reasons = set()
    for trigger in trigger_identities:
        if isinstance(trigger, str) and trigger.startswith("mission3-gate:"):
            reasons.add(trigger.split(":", 2)[1])
    return frozenset(reasons)


def joint34_revision_class(
    environment: Mapping, trigger_identities: Sequence[str] = ()
) -> str:
    """Classify pending search deadlines and fresh inspection evidence."""
    trigger_identities = tuple(
        part
        for trigger in trigger_identities
        for part in re.split(r";(?=mission[34]-gate:)", trigger)
    )
    now = float(environment["mission_time_seconds"])
    world = environment.get("world_model_info", {})
    reasons = _mission3_trigger_reasons(trigger_identities)
    if reasons & {"new_inconclusive_evidence", "target_moved"}:
        return "preemptive"
    # Request urgency survives revisions triggered by the other mission.
    # Trigger identities describe what changed, not all work still pending.
    section = world.get("mission4", {})
    if (
        isinstance(section, Mapping)
        and section.get("status") == "active"
        and section.get("objectives")
    ):
        deadline = float(section.get("deadline_s", math.inf))
        end = world.get("mission_end_time_s")
        if isinstance(end, (int, float)):
            deadline = min(deadline, float(end))
        if 0 < deadline - now <= JOINT34_PREEMPTIVE_DEADLINE_S:
            return "preemptive"
    return "routine"


def _m3_site_position(
    decision: Mapping | None,
    world: Mapping,
    drone: tuple[float, float],
) -> tuple[float, float]:
    if isinstance(decision, Mapping):
        parameters = decision.get("parameters")
        if isinstance(parameters, Mapping):
            if {"x", "y"} <= set(parameters):
                return float(parameters["x"]), float(parameters["y"])
            entity_id = parameters.get("entity_id", decision.get("entity_id"))
            section = world.get("mission3", {})
            observations = (
                section.get("target_observations", ())
                if isinstance(section, Mapping)
                else ()
            )
            if isinstance(observations, (list, tuple)):
                for item in observations:
                    if (
                        isinstance(item, Mapping)
                        and item.get("ship_id") == entity_id
                    ):
                        position = item.get("estimated_position")
                        if isinstance(position, Mapping) and {"x", "y"} <= set(
                            position
                        ):
                            return (
                                float(position["x"]),
                                float(position["y"]),
                            )
    return drone


def _m4_site_position(
    decision: Mapping | None, drone: tuple[float, float]
) -> tuple[float, float]:
    if isinstance(decision, Mapping):
        parameters = decision.get("parameters")
        if isinstance(parameters, Mapping):
            if {"x", "y"} <= set(parameters):
                return float(parameters["x"]), float(parameters["y"])
            target = parameters.get("target")
            if isinstance(target, Mapping) and {"x", "y"} <= set(target):
                return float(target["x"]), float(target["y"])
            polygon = parameters.get("polygon")
            if isinstance(polygon, (list, tuple)) and polygon:
                points = [
                    vertex
                    for vertex in polygon
                    if isinstance(vertex, Mapping) and {"x", "y"} <= set(vertex)
                ]
                if points:
                    return (
                        sum(float(vertex["x"]) for vertex in points) / len(points),
                        sum(float(vertex["y"]) for vertex in points) / len(points),
                    )
    return drone


def _travel_millis(
    first: tuple[float, float], second: tuple[float, float], speed_mps: float
) -> int:
    if speed_mps <= 0:
        raise ValueError("joint34 scheduling requires a positive drone speed")
    return math.ceil(1000.0 * math.dist(first, second) / speed_mps)


def build_joint34_grounding(
    environment: Mapping,
    mission3_decision: object | None = None,
    mission4_decision: object | None = None,
    trigger_identities: Sequence[str] = (),
) -> dict[str, object]:
    """Collect both middle tiers' outputs for one joint34 plan revision."""
    world = environment.get("world_model_info", {})
    if not isinstance(world, Mapping) or world.get("mission_mode") != "joint34":
        raise ValueError("joint34 grounding requires joint34 mission mode")
    now = float(environment["mission_time_seconds"])
    mission_end = float(world.get("mission_end_time_s", now))
    section3 = world.get("mission3")
    if not isinstance(section3, Mapping):
        raise TypeError("joint34 environment has no Mission 3 inspection state")
    ships = section3.get("ships", ())
    unresolved_m3 = [
        item
        for item in (ships if isinstance(ships, (list, tuple)) else ())
        if isinstance(item, Mapping)
        and isinstance(item.get("resolution"), Mapping)
        and item["resolution"].get("status") != "resolved"
    ]
    m3_pending = bool(unresolved_m3) and now < mission_end
    decision3 = mission3_decision
    if decision3 is None and m3_pending:
        decision3 = Mission3AdaptivePlanner().decide(environment)
    section = world.get("mission4", {})
    if not isinstance(section, Mapping):
        raise TypeError("joint34 environment has no Mission 4 search state")
    m4_pending = (
        section.get("status") == "active"
        and bool(section.get("objectives"))
        and now < float(section.get("deadline_s", 0.0))
    )
    decision = mission4_decision
    if decision is None and m4_pending:
        decision = Mission4AdaptivePlanner(str(environment.get("mission_id", "mission4"))).decide(
            environment
        )
    to_dict = getattr(decision3, "to_dict", None)
    to_dict4 = getattr(decision, "to_dict", None)
    return {
        "schema_version": 1,
        "mission_mode": "joint34",
        "revision_class": joint34_revision_class(environment, trigger_identities),
        "mission_time_seconds": now,
        "mission_end_time_s": mission_end,
        "mission3_pending": m3_pending,
        "mission3_decision": (
            decision3.to_dict() if callable(to_dict) else decision3
        ),
        "mission4_pending": m4_pending,
        "mission4_decision": (
            decision.to_dict() if callable(to_dict4) else decision
        ),
        "mission4_deadline_s": section.get("deadline_s"),
        "mission4_request_revision": section["revision"],
        "trigger_identities": [str(trigger) for trigger in trigger_identities],
    }


def _problem_text(schedule: Mapping, costs: Mapping[str, int]) -> str:
    pending = []
    if schedule["mission3_pending"]:
        pending.append("    (pending m3)\n")
    else:
        pending.append("    (completed m3)\n")
    if schedule["mission4_pending"]:
        pending.append("    (pending m4)\n")
    else:
        pending.append("    (completed m4)\n")
    functions = "".join(
        f"    (= ({name}) {value})\n" for name, value in costs.items()
    )
    return (
        "(define (problem joint34-schedule) (:domain joint34-scheduler)\n"
        "  (:init\n"
        "    (at drone)\n"
        "    (= (total-cost) 0)\n"
        f"{''.join(pending)}"
        f"{functions}"
        "  )\n"
        "  (:goal (and (completed m3) (completed m4)))\n"
        "  (:metric minimize (total-cost))\n"
        ")\n"
    )


def materialize_joint34_pddl(
    environment: Mapping,
    mission3_decision: object,
    mission4_decision: object,
    out_dir: Path,
    *,
    trigger_identities: Sequence[str] = (),
) -> tuple[Path, Path, dict[str, object]]:
    """Write the checked-in domain and the per-revision problem for one revision.

    ``mission3_decision``/``mission4_decision`` accept the middle tiers'
    outputs (decision mappings or objects); the grounding is recomputed from
    ``environment`` so the problem numbers always come from current public
    evidence, never hand-invented values.
    """
    schedule = build_joint34_grounding(
        environment, mission3_decision, mission4_decision, trigger_identities
    )
    if mission3_decision is not None:
        to_dict = getattr(mission3_decision, "to_dict", None)
        schedule["mission3_decision"] = (
            mission3_decision.to_dict() if callable(to_dict) else mission3_decision
        )
        section3 = environment.get("world_model_info", {}).get("mission3", {})
        ships = (
            section3.get("ships", ())
            if isinstance(section3, Mapping) and isinstance(section3.get("ships"), (list, tuple))
            else ()
        )
        schedule["mission3_pending"] = schedule["mission3_pending"] and any(
            isinstance(item, Mapping)
            and isinstance(item.get("resolution"), Mapping)
            and item["resolution"].get("status") != "resolved"
            for item in ships
        )
    if mission4_decision is not None:
        to_dict4 = getattr(mission4_decision, "to_dict", None)
        schedule["mission4_decision"] = (
            mission4_decision.to_dict() if callable(to_dict4) else mission4_decision
        )
        schedule["mission4_pending"] = schedule["mission4_pending"] and bool(
            schedule["mission4_decision"]
        )
    vehicle = environment["controlled_vehicle"]
    position = vehicle["position"]
    speed = float(vehicle["max_velocity"])
    drone = (float(position["x"]), float(position["y"]))
    decision3 = schedule["mission3_decision"]
    m3_site = (
        _m3_site_position(decision3, environment.get("world_model_info", {}), drone)
        if schedule["mission3_pending"]
        else drone
    )
    m4_site = (
        _m4_site_position(schedule["mission4_decision"], drone)
        if schedule["mission4_pending"]
        else drone
    )
    defer = (
        DEFER_COST_PREEMPTIVE
        if schedule["revision_class"] == "preemptive"
        else DEFER_COST_ROUTINE
    )
    service_m3 = (
        0
        if not isinstance(decision3, Mapping)
        else _JOINT34_M3_SERVICE_MILLIS.get(str(decision3.get("action")), 0)
    )
    costs = {
        "serve-m3-from-drone-cost": _travel_millis(drone, m3_site, speed) + service_m3,
        "serve-m3-from-m3-site-cost": service_m3,
        "serve-m3-from-m4-site-cost": _travel_millis(m4_site, m3_site, speed) + service_m3,
        "serve-m4-from-drone-cost": _travel_millis(drone, m4_site, speed)
        + JOINT34_M4_SERVICE_MILLIS,
        "serve-m4-from-m3-site-cost": _travel_millis(m3_site, m4_site, speed)
        + JOINT34_M4_SERVICE_MILLIS,
        "serve-m4-from-m4-site-cost": JOINT34_M4_SERVICE_MILLIS,
        "defer-cost m3": defer,
        "defer-cost m4": defer,
    }
    schedule["sites"] = {
        "drone": list(drone),
        "m3-site": list(m3_site),
        "m4-site": list(m4_site),
    }
    schedule["costs"] = costs
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    domain_path = out / "domain.pddl"
    domain_path.write_text(DOMAIN_REFERENCE.read_text(encoding="utf-8"), encoding="utf-8")
    problem_path = out / "problem.pddl"
    problem_path.write_text(_problem_text(schedule, costs), encoding="utf-8")
    (out / _SCHEDULE_FILE).write_text(
        json.dumps(schedule, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return domain_path, problem_path, schedule


def joint34_plan_order(plan_text: str) -> tuple[list[str], list[str]]:
    """Extract the served mission order and deferrals from a validated sas plan."""
    order: list[str] = []
    deferred: list[str] = []
    for raw in plan_text.splitlines():
        line = raw.strip().lower()
        match = _ACTION_RE.match(line)
        if match is None:
            continue
        action = match.group(1).split()[0]
        if action.startswith("serve-m3") and "m3" not in order:
            order.append("m3")
        elif action.startswith("serve-m4") and "m4" not in order:
            order.append("m4")
        elif action == "defer-m3" and "m3" not in deferred:
            deferred.append("m3")
        elif action == "defer-m4" and "m4" not in deferred:
            deferred.append("m4")
    return order, deferred


def _mission3_block_reports(schedule: Mapping) -> bool:
    decision = schedule.get("mission3_decision")
    return isinstance(decision, Mapping) and decision.get("action") == "report"


def create_statechart(schedule: Mapping, plan_text: str) -> dict[str, object]:
    """Build the joint34 Statechart from schedule metadata and the validated plan.

    Per-mission block contexts reuse the checked-in Mission 3 and Mission 4
    Statechart shapes verbatim; a deferred mission's block is omitted. Blocks
    chain in plan order and the run terminates at the shared mission bound.
    """
    order, deferred = joint34_plan_order(plan_text)
    now = float(schedule["mission_time_seconds"])
    mission_end = float(schedule["mission_end_time_s"])
    blocks = [f"mission{mission[1]}-block" for mission in order]
    scheduling_policy = (
        f"This schedule covers Mission 4 worker revision {schedule['mission4_request_revision']}. "
        "A newer accepted worker revision with an effective deadline within "
        "60 seconds is preemptive: Hyper must replan the PDDL mission order, "
        "including while the other mission's maneuver is active. The existing "
        "reconciliation path applies that revision at a maneuver boundary. "
        "An additional viewpoint for the same accepted worker revision is "
        "within-mission progress, not a new priority. After a served maneuver "
        "completes, continue the committed next mission block; do not restart "
        "the served block solely for its next-view gate. Reconsider the mission "
        "order for new urgent work or material route/feasibility invalidation, "
        "not merely because the already-accounted-for deadline remains near. "
        "The M3/M4 gate decisions own within-mission feasibility and search "
        "completion. A non-report M4 gate means unresolved work; do not infer "
        "completion by comparing individual attribute uncertainties with "
        "found_threshold. Only the middle-tier report or terminal ledger "
        "establishes completion."
    )
    contexts: dict[str, object] = {
        "scheduling": {
            "desired_outcome": (
                "The VAL-validated schedule is already committed. plan_order and "
                "deferred are its final decisions, including an empty order; no "
                "planning or commit is in progress. Execute served blocks, or "
                "wait for fresh evidence if both missions were deferred. A "
                "routine deferral must be replanned when an M4 request deadline "
                "is within 60 seconds or M3 reports new_inconclusive_evidence "
                "or target_moved: those triggers change the PDDL defer price "
                "to preemptive, even when no physical route exists yet."
            ),
            "revision_class": schedule["revision_class"],
            "scheduling_policy": scheduling_policy,
            "plan_order": order,
            "deferred": deferred,
            "defer_cost_per_mission_millis": schedule["costs"]["defer-cost m3"],
            "trigger_identities": schedule.get("trigger_identities", []),
        },
        "joint34-complete": {
            "desired_outcome": (
                "report separate Mission 3 inspection and Mission 4 "
                "search-answer results at the run bound"
            )
        },
    }
    if "mission3-block" in blocks:
        decision3 = schedule["mission3_decision"]
        contexts["mission3-block"] = {
            "planner_item": decision3,
            "scheduling_policy": scheduling_policy,
            "desired_outcome": (
                decision3
                if decision3 is not None
                else "serve pending Mission 3 inspection targets with current public evidence"
            ),
        }
    if "mission4-block" in blocks:
        decision = schedule["mission4_decision"]
        contexts["mission4-block"] = {
            "planner_item": decision,
            "scheduling_policy": scheduling_policy,
            "desired_outcome": (
                decision
                if decision is not None
                else "serve pending Mission 4 search requests with current public evidence"
            ),
        }
    transitions = []
    for index, target in enumerate([*blocks, "joint34-complete"]):
        source = "scheduling" if index == 0 else (blocks + ["joint34-complete"])[index - 1]
        if target == "joint34-complete":
            # The run terminates at the shared mission bound (the Mission 3
            # budget), however the schedule ordered the blocks.
            readiness = {
                "not_before": {"seconds": mission_end},
                "mission_time_at_or_after": {"seconds": mission_end},
            }
        elif source == "scheduling":
            # Immediate entry, but never through the Mission 1/2 deterministic
            # entry dispatch (no surveillance_mode on a joint34 block).
            readiness = {"not_before": {"seconds": now}}
        elif source == "mission3-block" and _mission3_block_reports(schedule):
            # A Mission 3 report decision completes its block immediately:
            # there is no maneuver feedback to await.
            readiness = {"not_before": {"seconds": now}}
        else:
            # Block completion follows terminal maneuver feedback (the
            # checked-in per-mission shapes).
            readiness = {"matching_maneuver_lifecycle_terminal": True}
        transitions.append(
            {
                "event": (
                    "schedule-committed" if source == "scheduling" else f"{source}-complete"
                ),
                "source": source,
                "target": target,
                "context": {
                    "readiness": readiness,
                    "desired_outcome": (
                        "the authoritative run bound has been reached"
                        if target == "joint34-complete"
                        else "the validated schedule advances to the next mission block"
                    ),
                },
            }
        )
        if source == "mission3-block" and target == "mission4-block" and not _mission3_block_reports(schedule):
            transitions.append(
                {
                    "event": "mission3-budget-exhausted",
                    "source": source,
                    "target": target,
                    "context": {
                        "readiness": {
                            "not_before": {"seconds": mission_end},
                            "mission_time_at_or_after": {"seconds": mission_end},
                        },
                        "desired_outcome": "issue the remaining Mission 4 result at the run bound",
                    },
                }
            )
    return {
        "entry_state": "scheduling",
        "terminal_states": ["joint34-complete"],
        "states": ["scheduling", *blocks, "joint34-complete"],
        "state_context": contexts,
        "transitions": transitions,
    }


def emit_joint34_statechart(
    schedule_metadata: Mapping, plan_path: Path, out: Path
) -> Path:
    """Write statechart.json for one revision from the VAL-validated plan."""
    chart = create_statechart(
        schedule_metadata, Path(plan_path).read_text(encoding="utf-8")
    )
    output = Path(out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(chart, indent=2) + "\n", encoding="utf-8")
    return output


def main(argv=None) -> int:
    """Materialize joint34 PDDL assets or emit a Statechart from a validated plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", type=Path, nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--emit-statechart", action="store_true")
    parser.add_argument("--plan", type=Path, help="VAL-validated sas_plan")
    parser.add_argument("--schedule", type=Path, help="materialized joint34-schedule.json")
    args = parser.parse_args(argv)
    if args.emit_statechart:
        if args.plan is None or args.schedule is None or args.output is None:
            parser.error("--emit-statechart requires --plan, --schedule and --output")
        schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
        path = emit_joint34_statechart(schedule, args.plan, args.output)
        chart = json.loads(path.read_text(encoding="utf-8"))
        print(json.dumps({"states": len(chart["states"]), "transitions": len(chart["transitions"])}))
        return 0
    if args.environment is None or args.output is None:
        parser.error("materialize mode requires an environment file and --output DIR")
    environment = json.loads(args.environment.read_text(encoding="utf-8"))
    domain_path, problem_path, schedule = materialize_joint34_pddl(
        environment, None, None, args.output
    )
    summary = {
        key: schedule[key]
        for key in (
            "revision_class",
            "mission3_pending",
            "mission4_pending",
            "mission_time_seconds",
            "mission_end_time_s",
        )
    }
    summary.update({"domain": str(domain_path), "problem": str(problem_path)})
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
