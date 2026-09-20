"""Joint24 (Mission 2 + Mission 4) three-tier mission scheduling.

Code-owned combo seam. The per-mission middle tiers stay the within-mission
movement authority: Mission 2's ranked observation candidates (its pick-<=1
selection model's argmax) and Mission 4's adaptive planner decisions are the
grounding inputs. This module encodes both missions' pending work, deadlines,
alert urgency and switch costs as a small STRIPS + action-costs PDDL problem
per plan revision; Fast Downward (astar(lmcut())) produces the mission
ordering and VAL validates it. The checked-in domain under
``conf/skills/hyper/creating-pddl-problem-files/examples/joint24-scheduler/``
is submitted unchanged; only the generated problem file carries numbers.

Scheduling policy: priority classes + bounded preemption. A revision is
preemptive-class when a fresh Mission 2 collision alert fired, a higher
priority risk is predicted to enter 10 m within
``JOINT24_PREEMPTIVE_ENTRY_S``, or a Mission 4 request deadline falls within
``JOINT24_PREEMPTIVE_DEADLINE_S``; otherwise it is routine. Deferring a
preemptive revision's work costs ``DEFER_COST_PREEMPTIVE`` (the Mission 2
selection model's 100000 weight scale), so urgency always outranks travel;
routine deferral costs ``DEFER_COST_ROUTINE``, letting the solver skip a
low-urgency mission when switch travel outweighs it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from onr.application.mission2_planning import (
    collision_observation_candidates,
    mission2_advisory_candidate,
)
from onr.application.mission4_planning import Mission4AdaptivePlanner

JOINT24_PREEMPTIVE_ENTRY_S = 30.0
JOINT24_PREEMPTIVE_DEADLINE_S = 60.0
DEFER_COST_PREEMPTIVE = 100000
DEFER_COST_ROUTINE = 1000
JOINT24_M4_SERVICE_MILLIS = 60000

DOMAIN_REFERENCE = (
    Path(__file__).resolve().parents[3]
    / "conf/skills/hyper/creating-pddl-problem-files/examples/joint24-scheduler/domain.pddl"
)

_SCHEDULE_FILE = "joint24-schedule.json"
_ACTION_RE = re.compile(r"^(?:\d+(?:\.\d+)?\s*:\s*)?\(([^)]+)\)")


def _mission2_trigger_reasons(trigger_identities: Sequence[str]) -> frozenset[str]:
    reasons = set()
    for trigger in trigger_identities:
        if isinstance(trigger, str) and trigger.startswith("mission2-gate:"):
            reasons.add(trigger.split(":", 2)[1])
    return frozenset(reasons)


def joint24_revision_class(
    environment: Mapping, trigger_identities: Sequence[str] = ()
) -> str:
    """Map concatenated gate trigger identities to the revision's priority class."""
    now = float(environment["mission_time_seconds"])
    world = environment.get("world_model_info", {})
    reasons = _mission2_trigger_reasons(trigger_identities)
    if "new_warning" in reasons:
        return "preemptive"
    if "higher_priority_risk" in reasons:
        candidates = collision_observation_candidates(environment)
        advisory = mission2_advisory_candidate(candidates, now) if candidates else None
        if (
            advisory is not None
            and advisory.predicted_contact_at_s - now <= JOINT24_PREEMPTIVE_ENTRY_S
        ):
            return "preemptive"
    if any(
        isinstance(trigger, str) and trigger.startswith("mission4-gate:")
        for trigger in trigger_identities
    ):
        section = world.get("mission4", {})
        if (
            isinstance(section, Mapping)
            and section.get("objectives")
            and float(section.get("deadline_s", math.inf)) - now
            <= JOINT24_PREEMPTIVE_DEADLINE_S
        ):
            return "preemptive"
    return "routine"


def _m4_site_position(
    decision: Mapping | None, drone: tuple[float, float]
) -> tuple[float, float]:
    if isinstance(decision, Mapping):
        parameters = decision.get("parameters")
        if isinstance(parameters, Mapping):
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
        raise ValueError("joint24 scheduling requires a positive drone speed")
    return math.ceil(1000.0 * math.dist(first, second) / speed_mps)


def build_joint24_grounding(
    environment: Mapping,
    mission4_gate_decision: object | None = None,
    trigger_identities: Sequence[str] = (),
) -> dict[str, object]:
    """Collect both middle tiers' outputs for one joint24 plan revision."""
    world = environment.get("world_model_info", {})
    if not isinstance(world, Mapping) or world.get("mission_mode") != "joint24":
        raise ValueError("joint24 grounding requires joint24 mission mode")
    now = float(environment["mission_time_seconds"])
    mission_end = float(world.get("mission_end_time_s", now))
    candidates = collision_observation_candidates(environment)
    selected = mission2_advisory_candidate(candidates, now) if candidates else None
    m2_pending = selected is not None and now < mission_end
    section = world.get("mission4", {})
    if not isinstance(section, Mapping):
        raise TypeError("joint24 environment has no Mission 4 search state")
    m4_pending = (
        section.get("status") == "active"
        and bool(section.get("objectives"))
        and now < float(section.get("deadline_s", 0.0))
    )
    decision = mission4_gate_decision
    if decision is None and m4_pending:
        decision = Mission4AdaptivePlanner(str(environment.get("mission_id", "mission4"))).decide(
            environment
        )
    to_dict = getattr(decision, "to_dict", None)
    return {
        "schema_version": 1,
        "mission_mode": "joint24",
        "revision_class": joint24_revision_class(environment, trigger_identities),
        "mission_time_seconds": now,
        "mission_end_time_s": mission_end,
        "mission2_pending": m2_pending,
        "selected_m2_candidate": None if not m2_pending else selected.to_dict(),
        "mission4_pending": m4_pending,
        "mission4_decision": (
            decision.to_dict() if callable(to_dict) else decision
        ),
        "mission4_deadline_s": section.get("deadline_s"),
        "trigger_identities": [str(trigger) for trigger in trigger_identities],
    }


def _problem_text(schedule: Mapping, costs: Mapping[str, int]) -> str:
    pending = []
    if schedule["mission2_pending"]:
        pending.append("    (pending m2)\n")
    else:
        pending.append("    (completed m2)\n")
    if schedule["mission4_pending"]:
        pending.append("    (pending m4)\n")
    else:
        pending.append("    (completed m4)\n")
    functions = "".join(
        f"    (= ({name}) {value})\n" for name, value in costs.items()
    )
    return (
        "(define (problem joint24-schedule) (:domain joint24-scheduler)\n"
        "  (:init\n"
        "    (at drone)\n"
        "    (= (total-cost) 0)\n"
        f"{''.join(pending)}"
        f"{functions}"
        "  )\n"
        "  (:goal (and (completed m2) (completed m4)))\n"
        "  (:metric minimize (total-cost))\n"
        ")\n"
    )


def materialize_joint24_pddl(
    environment: Mapping,
    mission2_candidates: object,
    mission4_decision: object,
    out_dir: Path,
    *,
    trigger_identities: Sequence[str] = (),
) -> tuple[Path, Path, dict[str, object]]:
    """Write the checked-in domain and the per-revision problem for one revision.

    ``mission2_candidates``/``mission4_decision`` accept the middle tiers'
    outputs (a selected candidate mapping and a decision mapping or object);
    the grounding is recomputed from ``environment`` so the problem numbers
    always come from current public evidence, never hand-invented values.
    """
    schedule = build_joint24_grounding(environment, mission4_decision, trigger_identities)
    if mission2_candidates is not None:
        candidate = mission2_candidates
        to_dict = getattr(candidate, "to_dict", None)
        schedule["selected_m2_candidate"] = (
            candidate.to_dict() if callable(to_dict) else candidate
        )
        schedule["mission2_pending"] = schedule["selected_m2_candidate"] is not None
    vehicle = environment["controlled_vehicle"]
    position = vehicle["position"]
    speed = float(vehicle["max_velocity"])
    drone = (float(position["x"]), float(position["y"]))
    candidate = schedule["selected_m2_candidate"]
    m2_site = (
        (float(candidate["x"]), float(candidate["y"]))
        if isinstance(candidate, Mapping)
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
    service_m2 = (
        0
        if not isinstance(candidate, Mapping)
        else max(0, round(1000.0 * (float(candidate["end_s"]) - float(candidate["start_s"]))))
    )
    costs = {
        "serve-m2-from-drone-cost": _travel_millis(drone, m2_site, speed) + service_m2,
        "serve-m2-from-m2-site-cost": service_m2,
        "serve-m2-from-m4-site-cost": _travel_millis(m4_site, m2_site, speed) + service_m2,
        "serve-m4-from-drone-cost": _travel_millis(drone, m4_site, speed)
        + JOINT24_M4_SERVICE_MILLIS,
        "serve-m4-from-m2-site-cost": _travel_millis(m2_site, m4_site, speed)
        + JOINT24_M4_SERVICE_MILLIS,
        "serve-m4-from-m4-site-cost": JOINT24_M4_SERVICE_MILLIS,
        "defer-cost m2": defer,
        "defer-cost m4": defer,
    }
    schedule["sites"] = {
        "drone": list(drone),
        "m2-site": list(m2_site),
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


def joint24_plan_order(plan_text: str) -> tuple[list[str], list[str]]:
    """Extract the served mission order and deferrals from a validated sas plan."""
    order: list[str] = []
    deferred: list[str] = []
    for raw in plan_text.splitlines():
        line = raw.strip().lower()
        match = _ACTION_RE.match(line)
        if match is None:
            continue
        action = match.group(1).split()[0]
        if action.startswith("serve-m2") and "m2" not in order:
            order.append("m2")
        elif action.startswith("serve-m4") and "m4" not in order:
            order.append("m4")
        elif action == "defer-m2" and "m2" not in deferred:
            deferred.append("m2")
        elif action == "defer-m4" and "m4" not in deferred:
            deferred.append("m4")
    return order, deferred


def create_statechart(schedule: Mapping, plan_text: str) -> dict[str, object]:
    """Build the joint24 Statechart from schedule metadata and the validated plan.

    Per-mission block contexts reuse the checked-in Mission 2 and Mission 4
    Statechart shapes verbatim; a deferred mission's block is omitted. Blocks
    chain in plan order and the run terminates at the Mission 2 recording end.
    """
    order, deferred = joint24_plan_order(plan_text)
    now = float(schedule["mission_time_seconds"])
    mission_end = float(schedule["mission_end_time_s"])
    blocks = [f"mission{mission[1]}-block" for mission in order]
    candidate = schedule.get("selected_m2_candidate")
    candidate_end = (
        float(candidate["end_s"])
        if isinstance(candidate, Mapping) and "end_s" in candidate
        else now
    )
    contexts: dict[str, object] = {
        "scheduling": {
            "desired_outcome": "commit the VAL-validated joint mission order for this revision",
            "revision_class": schedule["revision_class"],
            "plan_order": order,
            "deferred": deferred,
            "defer_cost_per_mission_millis": schedule["costs"]["defer-cost m2"],
            "trigger_identities": schedule.get("trigger_identities", []),
        },
        "joint24-complete": {
            "desired_outcome": (
                "report separate Mission 2 collision-monitoring and Mission 4 "
                "search-answer results after the recording ends"
            )
        },
    }
    if "mission2-block" in blocks:
        contexts["mission2-block"] = {
            "candidate_id": candidate["candidate_id"],
            "surveillance_mode": candidate["mode"],
            "target_entity_id": candidate["target_entity_id"],
            "risk_pair": candidate["ship_ids"],
            "prediction_run_id": candidate["prediction_run_id"],
            "prediction_sequence": candidate["prediction_sequence"],
            "observation_window": {
                "start_s": candidate["start_s"],
                "end_s": candidate["end_s"],
            },
            "desired_outcome": {
                "location": {
                    "x": candidate["x"],
                    "y": candidate["y"],
                    "z": candidate["z"],
                },
                "arrival_deadline": {"seconds": candidate["end_s"]},
                "entity_id": candidate["target_entity_id"],
            },
            "planner_item": candidate,
        }
    if "mission4-block" in blocks:
        decision = schedule["mission4_decision"]
        contexts["mission4-block"] = {
            "planner_item": decision,
            "desired_outcome": (
                decision
                if decision is not None
                else "serve pending Mission 4 search requests with current public evidence"
            ),
        }
    transitions = []
    for index, target in enumerate([*blocks, "joint24-complete"]):
        source = "scheduling" if index == 0 else (blocks + ["joint24-complete"])[index - 1]
        if target == "joint24-complete":
            # The run terminates at the Mission 2 recording end, however the
            # schedule ordered the blocks.
            readiness = {"mission_time_at_or_after": {"seconds": mission_end}}
        elif source == "scheduling":
            readiness = {"mission_time_at_or_after": {"seconds": now}}
        elif source == "mission2-block":
            # Mission 2 block completion follows its observation window (the
            # checked-in Mission 2 shape).
            readiness = {"mission_time_at_or_after": {"seconds": candidate_end}}
        else:
            # Mission 4 block completion follows terminal maneuver feedback
            # (the checked-in Mission 4 shape).
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
                        "the authoritative scenario recording has ended"
                        if target == "joint24-complete"
                        else "the validated schedule advances to the next mission block"
                    ),
                },
            }
        )
    return {
        "entry_state": "scheduling",
        "terminal_states": ["joint24-complete"],
        "states": ["scheduling", *blocks, "joint24-complete"],
        "state_context": contexts,
        "transitions": transitions,
    }


def emit_joint24_statechart(
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
    """Materialize joint24 PDDL assets or emit a Statechart from a validated plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", type=Path, nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--emit-statechart", action="store_true")
    parser.add_argument("--plan", type=Path, help="VAL-validated sas_plan")
    parser.add_argument("--schedule", type=Path, help="materialized joint24-schedule.json")
    args = parser.parse_args(argv)
    if args.emit_statechart:
        if args.plan is None or args.schedule is None or args.output is None:
            parser.error("--emit-statechart requires --plan, --schedule and --output")
        schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
        path = emit_joint24_statechart(schedule, args.plan, args.output)
        chart = json.loads(path.read_text(encoding="utf-8"))
        print(json.dumps({"states": len(chart["states"]), "transitions": len(chart["transitions"])}))
        return 0
    if args.environment is None or args.output is None:
        parser.error("materialize mode requires an environment file and --output DIR")
    environment = json.loads(args.environment.read_text(encoding="utf-8"))
    domain_path, problem_path, schedule = materialize_joint24_pddl(
        environment, None, None, args.output
    )
    summary = {
        key: schedule[key]
        for key in (
            "revision_class",
            "mission2_pending",
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
