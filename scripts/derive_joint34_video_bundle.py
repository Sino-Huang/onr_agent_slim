#!/usr/bin/env python
"""Derive the Joint34 demo-video source bundle from the recorded run.

Replays ``run.7jruzl`` through the PhysicalRuntime without any agent, LLM, or
AirSim component: recorded worker requests and accepted maneuver commands are
replayed at their recorded mission times, every authoritative state is
verified tick-by-tick, and the recorded world snapshots are compared against
the replay.  Emits the frame metadata, world-model frames, evidence-timed
metric timeline, storyboard, and reconstruction receipt consumed by the
AirSim reconstruction renderer and validator.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

TICK_S = 0.5

# Chapters are pinned to recorded event times (see derive_storyboard); the
# pause lengths are editorial only.
CHAPTERS: tuple[tuple[float, int, str, str], ...] = (
    (
        0.0,
        7,
        "Two missions, one routine deferral",
        "The operator selects harbor vessels 7, 15 and 16 for inspection and "
        "asks for a red container in the dock. The VAL-validated schedule "
        "(revision 1) defers both missions: the container deadline is 300 s "
        "out and no ship has been observed, so the planner waits for "
        "evidence instead of burning the budget.",
    ),
    (
        1.5,
        7,
        "An urgent deadline reorders the plan",
        "At t=1.0 s the worker tightens the Mission 4 deadline to 60 s. "
        "That deadline is now inside the 60 s preemptive window, so the "
        "supervisor activates a replan at t=1.5 s: revision 2 serves "
        "Mission 4 first, then Mission 3. The drone flies the first "
        "red-container view while the ships keep moving.",
    ),
    (
        40.0,
        6,
        "A new objective and a longer deadline",
        "At t=40 s the worker adds a blue container and extends the "
        "deadline to 100 s. Both requests are accepted immediately as "
        "worker revisions 3 and 4; the currently committed revision 2 "
        "covers neither, so a replan is due.",
    ),
    (
        40.5,
        7,
        "Replan activated while Mission 3 is in flight",
        "Revision 3 is committed at t=40.5 s with order serve_m3 then "
        "serve_m4. The in-flight ship-16 screening maneuver is cancelled "
        "and re-targeted to ship 7 - the third replan activation of the "
        "run reverses the mission order mid-flight.",
    ),
    (
        80.0,
        6,
        "A truck request without a fixture target",
        "At t=80 s the worker asks for a truck in the dock (worker "
        "revision 5). No truck exists in the recorded fixture, so the "
        "objective can never resolve; the search continues honestly "
        "without inventing a detection.",
    ),
    (
        97.0,
        7,
        "Revision 4 returns to Mission 4",
        "The Mission 4 view maneuver completes at t=97 s. The supervisor "
        "commits revision 4 (Mission 4 first) and the drone takes another "
        "dock view; it fails at t=100.5 s and the final ship-15 screening "
        "maneuver takes over.",
    ),
    (
        120.0,
        8,
        "Final report at the mission budget",
        "The 120 s Mission 3 budget ends the run in joint34-complete. All "
        "three selected vessels remain unresolved with no visual "
        "inspection evidence. Mission 4 found the blue container; the red "
        "container and the truck remain incomplete, and no location, "
        "direction, or type question was answered.",
    ),
)

ENDING = (
    "Joint34 run run.7jruzl: joint34-complete at 120.0 s. Four plan "
    "revisions, three replan activations (t=1.5, 40.5, 97.0 s). Vessels "
    "7, 15 and 16 end unresolved with no inspection evidence. Mission 4: "
    "blue container found; red container and truck incomplete; all "
    "answers unanswered."
)


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.json"))]


def _load_run(run_root: Path) -> dict[str, Any]:
    result = json.loads((run_root / "closed-loop-result.json").read_text(encoding="utf-8"))
    if not result.get("terminal"):
        raise ValueError("source run is not terminal")
    observations = _records(run_root / "physical-state" / "observations")
    states = {
        float(row["mission_time_s"]): row
        for row in observations
        if row.get("observation_kind") == "state"
    }
    worlds = {
        float(row["observation_time_s"]): row
        for row in observations
        if row.get("observation_kind") == "world_model"
    }
    commands = {
        row["command_id"]: row
        for row in _records(run_root / "physical-state" / "commands")
    }
    accepted = {}
    for row in _records(run_root / "physical-state" / "feedback"):
        if row.get("phase") == "accepted":
            time_s = float(row["mission_time_s"])
            if time_s in accepted:
                raise ValueError(f"two commands accepted at t={time_s}")
            accepted[time_s] = commands[row["command_id"]]
    worker = json.loads((run_root / "mission4-worker-session.json").read_text(encoding="utf-8"))
    maneuvers = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (run_root / "transport" / "topics" / "maneuver-feedback").rglob(
                "*.json"
            )
        )
    ]
    acceptance = json.loads((run_root / "live-acceptance.json").read_text(encoding="utf-8"))
    return {
        "result": result,
        "states": states,
        "worlds": worlds,
        "accepted": accepted,
        "worker": worker,
        "maneuvers": maneuvers,
        "acceptance": acceptance,
    }


def replan_times(inference_windows: list[Mapping[str, Any]]) -> list[float]:
    """Recorded replan-activation completion times, in order."""
    return sorted(
        float(window["completion_time_seconds"])
        for window in inference_windows
        if window.get("role") == "replan"
    )


def maneuver_blocks(maneuvers: list[Mapping[str, Any]]) -> list[tuple[float, float, str]]:
    """Active-maneuver intervals with their mission block, from feedback."""
    intervals: list[tuple[float, float, str]] = []
    for event in maneuvers:
        payload = event["payload"]
        if payload.get("lifecycle") != "active":
            continue
        started = float(payload["payload"]["mission_time_seconds"])
        maneuver_id = str(payload["maneuver_id"])
        ended = None
        for other in maneuvers:
            other_payload = other["payload"]
            if (
                other_payload["maneuver_id"] == maneuver_id
                and other_payload.get("lifecycle") in {"completed", "cancelled", "failed"}
                and float(other_payload["payload"]["mission_time_seconds"]) >= started
            ):
                ended = float(other_payload["payload"]["mission_time_seconds"])
                break
        if ended is None:
            ended = float("inf")
        prefix = "mission4" if maneuver_id.startswith("m4") else "mission3"
        intervals.append((started, ended, f"{prefix}-block"))
    intervals.sort()
    return intervals


def block_at(
    intervals: list[tuple[float, float, str]], terminal_state: str, time_s: float
) -> str:
    if time_s >= 120.0:
        return terminal_state
    for started, ended, block in intervals:
        if started <= time_s < ended:
            return block
    return "scheduling"


def plan_orders(run: Mapping[str, Any]) -> dict[int, str]:
    """Committed plan revision -> validated mission order label."""

    status_dir = (
        Path(run["run_root"]) / "transport" / "topics" / "fsm-status"
    )
    schedules: dict[int, dict[str, Any]] = {}
    for path in sorted(status_dir.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
        if payload.get("active_state") != "scheduling":
            continue
        context = payload.get("active_state_context") or {}
        schedules[int(payload["plan_revision"])] = {
            "plan_order": list(context.get("plan_order") or []),
            "deferred": list(context.get("deferred") or []),
        }
    orders: dict[int, str] = {}
    for revision, schedule in sorted(schedules.items()):
        order = schedule["plan_order"]
        deferred = schedule["deferred"]
        if order:
            label = "→".join(
                "M4" if "m4" in step else "M3" if "m3" in step else step
                for step in order
            )
        elif deferred:
            label = "defer " + "+".join(
                "M4" if "m4" in step else "M3" for step in sorted(deferred)
            )
        else:
            label = "none"
        orders[revision] = label
    return orders


def committed_revision_at(replans: list[float], time_s: float) -> int:
    """The plan revision committed at or before ``time_s``."""
    return 1 + sum(1 for time in replans if time <= time_s)


def _deadline_label(deadline: Any) -> str:
    if deadline is None:
        return "none"
    return f"{float(deadline):g}s"


def display_state(
    worlds: Mapping[float, Mapping[str, Any]],
    intervals: list[tuple[float, float, str]],
    replans: list[float],
    orders: Mapping[int, str],
    terminal_state: str,
    time_s: float,
) -> dict[str, Any]:
    """Evidence available at or before ``time_s``, never later evidence."""
    available = [t for t in worlds if t <= time_s]
    if not available:
        raise ValueError(f"no recorded world model at or before t={time_s}")
    info = worlds[max(available)]["world_model_info"]
    mission4 = info.get("mission4") or {}
    observations = mission4.get("observations") or []
    dock = ((mission4.get("package") or {}).get("areas") or {}).get("dock") or {}
    polygon = dock.get("polygon") or []
    track_labels: dict[str, str] = {}
    for observation in observations:
        if float(observation["acquired_at_s"]) > time_s:
            continue
        track_id = str(observation["track_id"])
        position = observation.get("position") or [0.0, 0.0]
        color = (observation.get("attributes") or {}).get("color") or {}
        label = str(color.get("value") or "track")
        if polygon and not (
            polygon[0][0] <= float(position[0]) <= polygon[1][0]
            and polygon[0][1] <= float(position[1]) <= polygon[2][1]
        ):
            label += "-outside"
        track_labels[track_id] = label
    tracks = [
        track_labels[track_id]
        for track_id in sorted(track_labels, key=lambda value: int(value.split(":")[1]))
    ]
    mission3 = info.get("mission3") or {}
    ships = mission3.get("ships") or []
    unresolved = sum(
        1
        for ship in ships
        if (ship.get("resolution") or {}).get("status") != "resolved"
    )
    revision = committed_revision_at(replans, time_s)
    requests = mission4.get("requests") or []
    request_label = "none"
    if requests:
        latest = requests[-1]["request"]
        if latest.get("operation") == "deadline":
            deadline_value = float(latest["deadline_s"])
            previous = next(
                (
                    float(entry["request"]["deadline_s"])
                    for entry in reversed(requests[:-1])
                    if entry["request"].get("operation") == "deadline"
                ),
                None,
            )
            if previous is None:
                package_budget = (mission4.get("package") or {}).get(
                    "mission_time_budget_s"
                )
                previous = (
                    float(package_budget) if package_budget is not None else None
                )
            verb = "extend to" if previous is None or deadline_value > previous else "tighten to"
            request_label = f"{verb} {deadline_value:g}s"
        else:
            objective = latest.get("objective") or {}
            attributes = objective.get("attributes") or {}
            request_label = " ".join(
                part
                for part in (attributes.get("color"), attributes.get("type"))
                if part
            ) or objective.get("description", "objective")
    return {
        "worker_request_label": request_label,
        "mission_block": block_at(intervals, terminal_state, time_s),
        "plan_revision": revision,
        "plan_order": orders[revision],
        "replan_count": sum(1 for time in replans if time <= time_s),
        "worker_revision": int(mission4.get("revision") or 0),
        "worker_deadline_s": mission4.get("deadline_s"),
        "m3_unresolved": unresolved,
        "m3_selected_ship_ids": sorted(int(s) for s in mission3.get("selected_ship_ids") or []),
        "m4_tracks": tracks,
    }


def build_timeline(
    worlds: Mapping[float, Mapping[str, Any]],
    intervals: list[tuple[float, float, str]],
    replans: list[float],
    orders: Mapping[int, str],
    terminal_state: str,
    final_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Collapse per-observation display states into change-point rows.

    Each row carries the mission time it becomes available; identical
    consecutive display states collapse so the rendered strip changes only
    at recorded availability boundaries.
    """
    display_fields = (
        "mission_block",
        "plan_revision",
        "plan_order",
        "replan_count",
        "worker_revision",
        "worker_request_label",
        "worker_deadline_s",
        "m3_unresolved",
        "m4_tracks",
    )
    timeline: list[dict[str, Any]] = []
    last_key = None
    for time_s in sorted(worlds):
        state = display_state(worlds, intervals, replans, orders, terminal_state, time_s)
        key = tuple(state[field] for field in display_fields)
        if key == last_key:
            continue
        timeline.append(
            {
                "mission_time_seconds": time_s,
                "evidence_available_at": time_s,
                **state,
            }
        )
        last_key = key
    timeline.append(
        {
            "mission_time_seconds": 120.0,
            "evidence_available_at": 120.0,
            **final_state,
        }
    )
    return timeline


def derive_storyboard() -> dict[str, Any]:
    return {
        "source_run": None,  # filled by main()
        "scope": (
            "Offline reconstruction of the accepted joint34 live run "
            "run.7jruzl; recorded LLM/tool decisions replayed exactly, no "
            "new live mission, model call, or AirSim recording."
        ),
        "reasoning_chapters": [
            {
                "time": time_s,
                "seconds": seconds,
                "actual_wait_seconds": 0.0,
                "title": title,
                "body": body,
            }
            for time_s, seconds, title, body in CHAPTERS
        ],
        "ending": ENDING,
    }


def _verify_world_info(recorded: Mapping[str, Any], replayed: Mapping[str, Any]) -> None:
    for key in (
        "visible_ship_ids",
        "event_report_checks",
        "public_position_fixes",
        "mission3",
        "mission4",
    ):
        if json.dumps(replayed.get(key), sort_keys=True) != json.dumps(
            recorded.get(key), sort_keys=True
        ):
            raise AssertionError(f"world model mismatch for {key!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--scenario-config", type=Path, required=True)
    parser.add_argument("--mission3-selection", type=Path, required=True)
    parser.add_argument("--mission4-package", type=Path, required=True)
    parser.add_argument("--mission4-fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    import numpy as np
    from PIL import Image
    from onr_physical_runtime.runtime import PhysicalRuntime
    from onr_physical_runtime.scenario import ScenarioConfig
    from onr_physical_runtime.service import (
        _load_mission3_selection,
        _load_mission3_time_budget,
        _load_mission4_package,
    )

    run_root = args.run.resolve()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    run = _load_run(run_root)
    run["run_root"] = str(run_root)

    duration = float(run["result"]["simulated_duration_seconds"])
    terminal_state = str(run["result"]["final_fsm_state"])
    if duration != 120.0 or terminal_state != "joint34-complete":
        raise ValueError(f"unexpected source run terminal state: {terminal_state}")
    last_tick = round(duration / TICK_S)

    replans = replan_times(run["result"]["inference_windows"])
    if replans != [1.5, 40.5, 97.0]:
        raise ValueError(f"unexpected replan activation times: {replans}")
    intervals = maneuver_blocks(run["maneuvers"])
    orders = plan_orders(run)
    acceptance = run["acceptance"]
    if acceptance.get("status") != "PASS":
        raise ValueError("source run acceptance is not PASS")

    # Recorded worker request envelopes, republished at their recorded times.
    requests = [
        (float(entry["at_s"]), entry["result"]["request"])
        for entry in run["worker"]["history"]
        if entry.get("result", {}).get("kind") == "request"
    ]

    scenario = ScenarioConfig.from_yaml(args.scenario_config)
    _, env, config = scenario.build()
    runtime = PhysicalRuntime(
        env,
        output / "replay-state",
        mission_id=str(run["result"]["mission_id"]),
        vehicle_id="drone-1",
        runtime_config=config,
        mission_mode="joint34",
        mission4_package=_load_mission4_package(args.mission4_package),
        mission4_fixture=_load_mission4_package(args.mission4_fixture),
        mission3_selection=_load_mission3_selection(args.mission3_selection),
        mission3_time_budget_s=_load_mission3_time_budget(args.mission3_selection),
    )
    step = float(config.time_step_duration)
    if step != TICK_S:
        raise ValueError(f"unexpected time step duration {step}")

    frames = output / "world-frames"
    frames.mkdir()
    metadata: list[dict[str, Any]] = []
    pose_errors: list[float] = []
    try:
        for tick in range(last_tick + 1):
            now = tick * step
            # The runtime swaps in a new environment object on partition
            # migration, so always read and render through runtime.env.
            actual = [float(value) for value in runtime.env.get_agent_ned_position(0)]
            heading = float(runtime._heading_degrees())
            if now in run["states"]:
                recorded = run["states"][now]["controlled_vehicle"]
                expected = recorded["position"]
                expected_heading = float(recorded["heading_degrees"])
                error = max(
                    abs(a - float(expected[axis]))
                    for a, axis in zip(actual, ("x", "y", "z"))
                )
                pose_errors.append(error)
                heading_error = abs(
                    (heading - expected_heading + 180.0) % 360.0 - 180.0
                )
                if error > 1e-6 or heading_error > 1e-6:
                    raise AssertionError(
                        f"pose mismatch at t={now}: replay={actual} heading="
                        f"{heading} vs recorded={expected} heading="
                        f"{expected_heading}"
                    )
            runtime.env.tile_size = 8
            frame = Image.fromarray(
                runtime.env.render(show_fog=True, fog_unseen_brightness=0.42)
            ).convert("RGB")
            frame.save(frames / f"{tick:04d}.png")
            available = [t for t in run["worlds"] if t <= now - step]
            info = (
                run["worlds"][max(available)]["world_model_info"]
                if available
                else {"visible_ship_ids": [], "event_report_checks": []}
            )
            grid = list(map(int, runtime.env.agents[0].state.pos))
            metadata.append(
                {
                    "tick": tick,
                    "mission_time": now,
                    "position_ned": actual,
                    "drone_pixel": [(grid[0] + 0.5) * 8, (grid[1] + 0.5) * 8],
                    "visible_ship_ids": info["visible_ship_ids"],
                    "checks": len(info["event_report_checks"]),
                    "sensor_time": max(available) if available else 0.0,
                }
            )
            (output / "replay-state" / "search_requests").mkdir(parents=True, exist_ok=True)
            for at_s, request in requests:
                if at_s <= now:
                    revision = int(request["base_revision"]) + 1
                    path = (
                        output
                        / "replay-state"
                        / "search_requests"
                        / f"{revision:08d}.json"
                    )
                    if not path.exists():
                        path.write_text(
                            json.dumps(
                                {
                                    "mission_id": run["result"]["mission_id"],
                                    "request": request,
                                }
                            )
                        )
            if now in run["accepted"]:
                (output / "replay-state" / "commands").mkdir(parents=True, exist_ok=True)
                (output / "replay-state" / "commands" / f"{tick:04d}.json").write_text(
                    json.dumps(run["accepted"][now])
                )
            if tick < last_tick:
                runtime.tick()
            if tick % 60 == 0:
                print(f"replayed {now:.1f}/{duration:.1f} s", flush=True)
    finally:
        runtime.close()

    replayed_worlds = {
        float(row["observation_time_s"]): row
        for row in _records(output / "replay-state" / "observations")
        if row.get("observation_kind") == "world_model"
    }
    comparisons = 0
    for time_s, recorded in run["worlds"].items():
        replayed = replayed_worlds.get(time_s)
        if replayed is None:
            raise AssertionError(f"replay is missing world model at t={time_s}")
        _verify_world_info(recorded["world_model_info"], replayed["world_model_info"])
        comparisons += 1
    if comparisons != len(run["worlds"]):
        raise AssertionError("replay produced unrecorded world models")
    if len(pose_errors) != len(run["states"]):
        raise AssertionError("replay did not verify every recorded state")

    (output / "frame-metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    answer_metrics = json.loads(
        (run_root / "mission4-answer-metrics.json").read_text(encoding="utf-8")
    )
    final_state = display_state(
        run["worlds"], intervals, replans, orders, terminal_state, 119.5
    )
    final_state.update(
        {
            "mission_block": terminal_state,
            "m4_target_status": {
                task["target_id"]: task["status"] for task in answer_metrics["tasks"]
            },
            "m4_answer_metrics": answer_metrics["aggregate"],
            "m3_terminal_outcome": {
                "selected_ship_ids": sorted(
                    int(ship_id)
                    for ship_id in acceptance["mission3_evidence"]["inspection"][
                        "selected_ship_ids"
                    ]
                ),
                "inspection_evidence_count": len(
                    acceptance["mission3_evidence"]["inspection"]["evidence"]
                ),
            },
        }
    )
    timeline = build_timeline(
        run["worlds"], intervals, replans, orders, terminal_state, final_state
    )
    metrics = {
        "source_run": str(run_root),
        "mission_mode": "joint34",
        "final_fsm_state": terminal_state,
        "simulated_duration_seconds": duration,
        "plan_revisions": list(run["result"]["plan_revisions"]),
        "replan_activation_times_s": replans,
        "plan_orders": {str(k): v for k, v in orders.items()},
        "worker_request_acceptances_s": [
            float(entry["at_s"])
            for entry in run["worker"]["history"]
            if entry.get("result", {}).get("kind") == "request"
        ],
        "mission4_answer_metrics": answer_metrics,
        "timeline_rule": (
            "Stepwise by recorded world-model publication time; displayed "
            "fields change only at recorded availability boundaries and "
            "never use future evidence."
        ),
        "timeline": timeline,
    }
    (output / "mission-metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )

    story = derive_storyboard()
    story["source_run"] = str(run_root)
    (output / "story.json").write_text(
        json.dumps(story, indent=2) + "\n", encoding="utf-8"
    )

    receipt = {
        "source_run": str(run_root),
        "mission_end_seconds": last_tick * step,
        "frames": len(metadata),
        "state_comparisons": len(pose_errors),
        "world_snapshot_comparisons": comparisons,
        "maximum_pose_error_m": max(pose_errors),
        "visibility_ledger_gps_exact": True,
        "live_model_calls": 0,
        "mission_mode": "joint34",
        "final_fsm_state": terminal_state,
        "scope": "Reconstructed from recorded accepted commands and worker requests; no new live mission.",
    }
    (output / "reconstruction-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
