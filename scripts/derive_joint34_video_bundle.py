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
from dataclasses import replace

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TICK_S = 0.5
# Editorial pause lengths only; chapter times come from the recorded run.
CHAPTER_SECONDS = 7
MID_CHAPTER_SECONDS = 6
FINAL_CHAPTER_SECONDS = 8
WORLD_OVERVIEW_SIZE_M = 2000.0
WORLD_OVERVIEW_WINDOW_CELLS = 256
WORLD_OVERVIEW_RESOLUTION_M = WORLD_OVERVIEW_SIZE_M / WORLD_OVERVIEW_WINDOW_CELLS
WORLD_OVERVIEW_OVERLAP_CELLS = 32
WORLD_OVERVIEW_TILE_SIZE = 2



def objective_phrase(objective: Mapping[str, Any]) -> str:
    """Public description of one Mission 4 objective."""

    attributes = objective.get("attributes") or {}
    described = " ".join(
        str(attributes[key]) for key in ("color", "type") if attributes.get(key)
    )
    return described or str(objective.get("description") or "an objective")


def worker_request_chapter(
    at_s: float,
    request: Mapping[str, Any],
    committed_revision: int,
    order_label: str,
) -> tuple[float, int, str, str]:
    """One chapter for a recorded worker request acceptance."""

    operation = str(request.get("operation"))
    revision = int(request.get("base_revision", 0)) + 1
    if operation == "deadline":
        deadline = float(request["deadline_s"])
        title = f"Worker sets the Mission 4 deadline to {deadline:g} s"
        body = (
            f"At t={at_s:g} s the worker revises the container deadline to "
            f"{deadline:g} s (worker revision {revision}). The committed plan "
            f"revision {committed_revision} ({order_label}) was scheduled "
            "against the earlier deadline, so the supervisor re-scores it."
        )
    elif operation == "add":
        objective = request.get("objective") or {}
        title = f"A new objective: {objective_phrase(objective)}"
        body = (
            f"At t={at_s:g} s the worker adds {objective_phrase(objective)} in "
            f"{'/'.join(objective.get('area_ids') or [])} as worker revision "
            f"{revision}. Accepted immediately; the committed plan revision "
            f"{committed_revision} ({order_label}) covers neither it nor its "
            "deadline, so a replan is due."
        )
    elif operation in {"remove", "cancel"}:
        title = "The worker drops an objective"
        body = (
            f"At t={at_s:g} s the worker {operation}s "
            f"{request.get('target_id', 'an objective')} as worker revision "
            f"{revision}; the committed plan revision {committed_revision} "
            f"({order_label}) is re-scored against the reduced objective set."
        )
    else:
        title = f"Worker {operation} request"
        body = (
            f"At t={at_s:g} s the worker publishes revision {revision} while "
            f"plan revision {committed_revision} ({order_label}) is committed."
        )
    return (at_s, MID_CHAPTER_SECONDS, title, body)


def replan_chapter(
    time_s: float,
    revision: int,
    order_label: str,
    previous_order: str | None,
    worker_revision: int,
    deadline_s: Any,
) -> tuple[float, int, str, str]:
    """One chapter for a recorded replan activation."""

    deadline_text = (
        "no Mission 4 deadline" if deadline_s is None else f"a {float(deadline_s):g} s deadline"
    )
    reversal = (
        ""
        if previous_order is None or previous_order == order_label
        else (
            f" The order changes from {previous_order} to {order_label}, "
            "reversing which mission is served first."
        )
    )
    body = (
        f"Revision {revision} is committed at t={time_s:g} s with order "
        f"{order_label}. The worker revision in force is {worker_revision} "
        f"with {deadline_text}.{reversal} The in-flight maneuver is "
        "re-evaluated against the new order while the drone keeps flying."
    )
    return (
        time_s,
        MID_CHAPTER_SECONDS,
        f"Revision {revision} committed with order {order_label}",
        body,
    )


def maneuver_chapter(
    time_s: float, maneuver_id: str, lifecycle: str, reason: Any
) -> tuple[float, int, str, str]:
    """One chapter for a recorded dock-view maneuver that ended short."""

    block = "Mission 4 dock view" if maneuver_id.startswith("m4") else "Mission 3 screening"
    outcome = {"failed": "ends without a result", "cancelled": "is cancelled"}.get(
        lifecycle, lifecycle
    )
    reason_text = "" if reason is None else f" Reason: {reason}."
    return (
        time_s,
        MID_CHAPTER_SECONDS,
        f"A {block} {outcome}",
        (
            f"At t={time_s:g} s the {block} {outcome} ({maneuver_id})."
            f"{reason_text} The recorded run keeps its outcome: no detection, "
            "no inspection evidence, and no answer is invented for it."
        ),
    )


def closing_chapter(
    mission_end_s: float,
    terminal_state: str,
    ship_ids: Sequence[int],
    inspection_evidence_count: int,
    mission4_statuses: Mapping[str, str],
    mission4_labels: Mapping[str, str],
    replans: Sequence[float],
) -> tuple[float, int, str, str]:
    """The final chapter at the recorded mission budget."""

    status_text = ", ".join(
        f"{mission4_labels.get(target, target)} {status}"
        for target, status in sorted(mission4_statuses.items())
    ) or "no objectives"
    vessel_text = (
        f"Vessels {'/'.join(str(ship) for ship in ship_ids)} end unresolved "
        "with no visual inspection evidence"
        if not inspection_evidence_count
        else (
            f"Vessels {'/'.join(str(ship) for ship in ship_ids)} leave "
            f"{inspection_evidence_count} inspection evidence record(s)"
        )
    )
    body = (
        f"The {mission_end_s:g} s budget ends the run in {terminal_state} "
        f"after {len(replans)} replan activation(s). {vessel_text}. "
        f"Mission 4: {status_text}. Every reported outcome stays as recorded."
    )
    return (
        mission_end_s,
        FINAL_CHAPTER_SECONDS,
        "Final report at the mission budget",
        body,
    )


def derive_chapters(
    *,
    mission_end_s: float,
    terminal_state: str,
    ship_ids: Sequence[int],
    inspection_evidence_count: int,
    mission4_statuses: Mapping[str, str],
    mission4_labels: Mapping[str, str],
    request_default_objective: str,
    requests: Sequence[tuple[float, Mapping[str, Any]]],
    replans: Sequence[float],
    orders: Mapping[int, str],
    worker_revisions: Sequence[tuple[float, int, Any]],
    maneuver_outcomes: Sequence[tuple[float, str, str, Any]],
) -> tuple[tuple[float, int, str, str], ...]:
    """Editorial chapters timed to this run's recorded events.

    Every time is a recorded event (a worker acceptance, a replan completion
    time, a maneuver outcome, or the mission budget) so the storyboard pauses
    land exactly where the run made a decision.
    """

    def committed_at(time_s: float) -> int:
        return committed_revision_at(list(replans), time_s)

    def order_at(time_s: float) -> str:
        return orders.get(committed_at(time_s), "none")

    chapters: list[tuple[float, int, str, str]] = [
        (
            0.0,
            CHAPTER_SECONDS,
            "Two missions, one schedule",
            (
                f"The operator selects harbor vessels "
                f"{'/'.join(str(ship) for ship in ship_ids)} for inspection "
                f"and asks the worker for {request_default_objective}. The "
                f"first VAL-validated schedule (revision 1) is "
                f"{orders.get(1, 'none')}: the search objectives are not yet "
                "imminent, so the planner waits for evidence instead of "
                "burning the budget."
            ),
        )
    ]
    for at_s, request in requests:
        chapters.append(
            worker_request_chapter(
                at_s,
                request,
                committed_at(at_s),
                order_at(at_s),
            )
        )
    previous_order: str | None = None
    for time_s in replans:
        revision = committed_revision_at(list(replans), time_s)
        deadline = next(
            (
                entry[2]
                for entry in reversed(worker_revisions)
                if entry[0] <= time_s
            ),
            None,
        )
        worker_revision = next(
            (entry[1] for entry in reversed(worker_revisions) if entry[0] <= time_s),
            0,
        )
        label = orders.get(revision, "none")
        chapters.append(
            replan_chapter(
                time_s,
                revision,
                label,
                previous_order,
                worker_revision,
                deadline,
            )
        )
        previous_order = label
    for time_s, maneuver_id, lifecycle, reason in maneuver_outcomes:
        chapters.append(maneuver_chapter(time_s, maneuver_id, lifecycle, reason))
    chapters.append(
        closing_chapter(
            mission_end_s,
            terminal_state,
            ship_ids,
            inspection_evidence_count,
            mission4_statuses,
            mission4_labels,
            replans,
        )
    )
    unique: dict[float, tuple[float, int, str, str]] = {}
    for chapter in chapters:
        unique.setdefault(chapter[0], chapter)
    return tuple(unique[time] for time in sorted(unique))


def derive_ending(
    *,
    run_label: str,
    mission_end_s: float,
    terminal_state: str,
    ship_ids: Sequence[int],
    inspection_evidence_count: int,
    plan_revision_count: int,
    replans: Sequence[float],
    mission4_statuses: Mapping[str, str],
    mission4_labels: Mapping[str, str],
) -> str:
    """Closing copy for this run's recorded outcome."""

    status_text = "; ".join(
        f"{mission4_labels.get(target, target)} {status}"
        for target, status in sorted(mission4_statuses.items())
    ) or "no objectives"
    vessel_text = (
        "no visual inspection evidence"
        if not inspection_evidence_count
        else f"{inspection_evidence_count} inspection evidence record(s)"
    )
    return (
        f"Joint34 run {run_label}: {terminal_state} at {mission_end_s:g} s. "
        f"{plan_revision_count} plan revisions, {len(replans)} replan "
        f"activation(s) (t={', '.join(f'{time:g}' for time in replans)} s). "
        f"Vessels {'/'.join(str(ship) for ship in ship_ids)}: {vessel_text}. "
        f"Mission 4: {status_text}."
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


OVERLAY_LAYER_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "dock_aoi",
        "aoi_polygons",
        "world_model_info.mission4.package.areas[*].polygon",
        "recorded package; drawn from the section that first publishes it",
    ),
    (
        "keep_out_zones",
        "koz_polygons",
        "world_model_info.mission4.package.keep_out_zones",
        "recorded package; drawn where the polygon intersects the pane window",
    ),
    (
        "obstacles",
        "obstacle_polygons",
        "world_model_info.mission4.package.obstacles",
        "recorded package; drawn where the polygon intersects the pane window",
    ),
    (
        "target_potential_locations",
        "targets",
        (
            "world_model_info.mission4.objectives with observations replayed "
            "through "
            "onr.application.object_search_belief.ObjectSearchBeliefManager"
        ),
        (
            "best match per objective; observations gated on acquired_at_s <= "
            "their section publication time"
        ),
    ),
    (
        "selected_vessel_gps_fixes",
        "ship_fixes",
        "world_model_info.public_position_fixes",
        (
            "latest fix per selected vessel with sampled_at_s <= tick time, "
            "drawn inside the pane window only"
        ),
    ),
    (
        "planned_route",
        "route_cells",
        "runtime.env.current_planned_paths['navigation']",
        (
            "planner state at the tick, rendered engine-natively under the "
            "overlay pass"
        ),
    ),
    (
        "active_search_boundary",
        "active_search_polygons",
        "accepted search_area command intent polygon (replay active maneuver)",
        (
            "only while the recorded search_area maneuver is the active "
            "maneuver; distinct from the static package AOI layer"
        ),
    ),
    (
        "area_search_plan",
        "search_path_points",
        "runtime.env.current_planned_paths['area_search'] reprojected to NED",
        "only while the recorded search_area maneuver is the active maneuver",
    ),
    (
        "drone_track",
        "track_points",
        (
            "recorded controlled-vehicle NED positions with breadcrumbs "
            "accumulated during the active search_area maneuver"
        ),
        "only while the recorded search_area maneuver is the active maneuver",
    ),
    (
        "search_progress",
        "progress_labels",
        "recorded maneuver phase and cleared/total cell progress",
        "chip drawn only while the recorded search_area maneuver is active",
    ),
    ("legend", "legend", "static pane key", "always drawn"),
)


def overlay_layer_receipt(
    per_tick: list[Mapping[str, Any]],
    recorded_polygons: Mapping[str, int],
    pane: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-layer draw record for the reconstruction receipt."""

    receipt: dict[str, Any] = {"world_pane": dict(pane)}
    for name, key, source, gating in OVERLAY_LAYER_SPECS:
        ticks = [int(row["tick"]) for row in per_tick if row.get(key)]
        entry: dict[str, Any] = {
            "source_field": source,
            "gating_rule": gating,
            "total_draws": sum(int(row.get(key, 0)) for row in per_tick),
            "frames_with_draws": len(ticks),
            "first_tick": ticks[0] if ticks else None,
            "last_tick": ticks[-1] if ticks else None,
        }
        if key in recorded_polygons:
            entry["recorded_polygons"] = recorded_polygons[key]
            if recorded_polygons[key] == 0:
                entry["note"] = (
                    "the recorded Mission 4 package carries no "
                    f"{name.replace('_', ' ')}; the layer is honestly empty"
                )
        receipt[name] = entry
    receipt["target_potential_locations"]["uncertainty_circles_total"] = sum(
        int(row.get("uncertainty_circles", 0)) for row in per_tick
    )
    return receipt


def recorded_polygon_counts(worlds: Mapping[float, Mapping[str, Any]]) -> dict[str, int]:
    """Package polygon counts of the run's recorded Mission 4 package."""

    latest = worlds[max(worlds)]["world_model_info"]["mission4"]["package"]
    return {
        "aoi_polygons": len(latest.get("areas") or {}),
        "koz_polygons": len(latest.get("keep_out_zones") or []),
        "obstacle_polygons": len(latest.get("obstacles") or []),
    }


def worker_revisions(
    run: Mapping[str, Any], package_budget: Any
) -> list[tuple[float, int, Any]]:
    """(acceptance time, worker revision, deadline in force) per recorded request."""

    revisions: list[tuple[float, int, Any]] = []
    deadline = package_budget
    for entry in run["worker"]["history"]:
        result = entry.get("result") or {}
        if result.get("kind") != "request":
            continue
        request = result["request"]
        if request.get("operation") == "deadline":
            deadline = float(request["deadline_s"])
        revisions.append(
            (
                float(entry["at_s"]),
                int(request["base_revision"]) + 1,
                deadline,
            )
        )
    return revisions


def dock_view_outcomes(run: Mapping[str, Any]) -> list[tuple[float, str, str, Any]]:
    """The first recorded Mission 4 dock view that ended failed or cancelled."""

    outcomes: list[tuple[float, str, str, Any]] = []
    for event in run["maneuvers"]:
        payload = event["payload"]
        maneuver_id = str(payload.get("maneuver_id") or "")
        if not maneuver_id.startswith("m4"):
            continue
        if payload.get("lifecycle") not in {"failed", "cancelled"}:
            continue
        outcomes.append(
            (
                float(payload["payload"]["mission_time_seconds"]),
                maneuver_id,
                str(payload["lifecycle"]),
                payload["payload"].get("reason"),
            )
        )
    return sorted(outcomes, key=lambda row: row[0])[:1]


def video_expectations(
    story_path: Path,
    metadata: Sequence[Mapping[str, Any]],
    tick_range: tuple[int, int],
) -> dict[str, Any]:
    """Profile expectations derived from this bundle's own storyboard.

    Frames, hold windows and pause times are computed with the renderer's own
    storyboard builder over the bundle's metadata and chapters, so a second
    joint34 run needs no cloned constants; the validator still compares the
    decoded video against these numbers.
    """

    from onr.demo.airsim_reconstruction.render import build_storyboard, load_storyboard
    from onr.demo.airsim_reconstruction.validate import storyboard_hold_windows

    chapters, _ = load_storyboard(story_path)
    storyboard = build_storyboard(
        metadata, chapters, range(tick_range[0], tick_range[1] + 1), tick_range
    )
    windows = storyboard_hold_windows(storyboard)
    return {
        "expected_frames": len(storyboard),
        "expected_window_counts": [window.frame_count for window in windows],
        "expected_pause_times": [
            window.mission_time_s for window in windows if window.kind == "pause"
        ],
    }


def receipt_final_state(
    terminal_state: str,
    unresolved_ships: int,
    ship_count: int,
    mission4_statuses: Mapping[str, str],
    objective_labels: Mapping[str, str],
) -> str:
    """One-line terminal outcome string carried by the receipt and the profile."""

    statuses = ", ".join(
        f"{objective_labels.get(target, target)} {status}"
        for target, status in sorted(mission4_statuses.items())
    )
    return (
        f"{terminal_state}; M3 unresolved {unresolved_ships}/{ship_count}; "
        f"M4 {statuses}"
    )


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
    intervals: list[tuple[float, float, str]],
    terminal_state: str,
    time_s: float,
    mission_end_s: float,
) -> str:
    if time_s >= mission_end_s:
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
    """The plan revision committed at or before ``time_s``.

    Revisions start at 1.  A replan activation at t=0.0 is the run's initial
    schedule (revision 1, published as a window by some runs); every later
    activation commits the next revision.
    """
    return 1 + sum(1 for time in replans if 0.0 < time <= time_s)


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
    *,
    mission_end_s: float,
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
        "mission_block": block_at(
            intervals, terminal_state, time_s, mission_end_s
        ),
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
    *,
    mission_end_s: float,
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
        state = display_state(
            worlds,
            intervals,
            replans,
            orders,
            terminal_state,
            time_s,
            mission_end_s=mission_end_s,
        )
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
            "mission_time_seconds": mission_end_s,
            "evidence_available_at": mission_end_s,
            **final_state,
        }
    )
    return timeline


def derive_storyboard(
    chapters: Sequence[tuple[float, int, str, str]],
    ending: str,
    *,
    source_run: str,
    scope: str,
) -> dict[str, Any]:
    return {
        "source_run": source_run,
        "scope": scope,
        "reasoning_chapters": [
            {
                "time": time_s,
                "seconds": seconds,
                "actual_wait_seconds": 0.0,
                "title": title,
                "body": body,
            }
            for time_s, seconds, title, body in chapters
        ],
        "ending": ending,
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
    parser.add_argument("--pane", choices=("overview", "local"), default="overview", help=(
        "world-pane projection: fixed 2 km north-up overview or the "
        "partition-local window that follows the drone"
    ))
    parser.add_argument("--surface-alignment", type=Path, help=(
        "paired pose/surface alignment audit JSON to embed in the receipts"
    ))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--scenario-config", type=Path, required=True)
    parser.add_argument("--mission3-selection", type=Path, required=True)
    parser.add_argument("--mission4-package", type=Path, required=True)
    parser.add_argument("--mission4-fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    from PIL import Image
    from onr.application.live_demo_audit import aoi_trajectory_audit
    from onr.demo.airsim_reconstruction import world_pane
    from onr_physical_runtime.runtime import PhysicalRuntime
    from onr_physical_runtime.scenario import ScenarioConfig
    from onr_physical_runtime.service import (
        _load_mission3_selection,
        _load_mission3_time_budget,
        _load_mission4_package,
    )
    import numpy as np


    run_root = args.run.resolve()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    run = _load_run(run_root)
    run["run_root"] = str(run_root)

    duration = float(run["result"]["simulated_duration_seconds"])
    terminal_state = str(run["result"]["final_fsm_state"])
    if terminal_state != "joint34-complete":
        raise ValueError(f"unexpected source run terminal state: {terminal_state}")
    if duration <= 0.0:
        raise ValueError(f"source run has no simulated duration: {duration}")
    last_tick = round(duration / TICK_S)

    replans = replan_times(run["result"]["inference_windows"])
    if not replans or sorted(set(replans)) != replans:
        raise ValueError(f"unexpected replan activation times: {replans}")
    intervals = maneuver_blocks(run["maneuvers"])
    orders = plan_orders(run)
    acceptance = run["acceptance"]
    if acceptance.get("status") != "PASS":
        raise ValueError("source run acceptance is not PASS")

    # Deterministic replay of the agent-side public-evidence belief: the
    # manager raises on any observation dated after its section, so a
    # future-dated potential location cannot be drawn early.
    snapshots = world_pane.belief_snapshots(
        world_pane.mission4_sections(run["worlds"]),
        str(run["result"]["mission_id"]),
    )

    # Recorded worker request envelopes, republished at their recorded times.
    # A terminal agent report (all_found) is part of the recorded ledger
    # history but never appears in the worker session, so append any finish
    # request from the recorded Mission 4 requests list.
    requests = [
        (float(entry["at_s"]), entry["result"]["request"])
        for entry in run["worker"]["history"]
        if entry.get("result", {}).get("kind") == "request"
    ]
    worker_request_ids = {
        entry["result"]["request"].get("request_id")
        for entry in run["worker"]["history"]
        if entry.get("result", {}).get("kind") == "request"
    }
    latest_mission4 = run["worlds"][max(run["worlds"])]["world_model_info"]["mission4"]
    for entry in latest_mission4.get("requests") or ():
        request = entry.get("request") or {}
        if (
            request.get("operation") == "finish"
            and request.get("request_id") not in worker_request_ids
        ):
            requests.append((float(entry["accepted_at_s"]), request))
    requests.sort(key=lambda item: item[0])

    scenario = ScenarioConfig.from_yaml(args.scenario_config)
    _, env, config = scenario.build()
    pane_overview = args.pane == "overview"
    overview_converter = overview_env = overview_geometry = None
    overview_prev_direction = None
    if pane_overview:
        overview_scenario = replace(
            scenario,
            world_model=replace(
                scenario.world_model,
                grid_resolution_m=WORLD_OVERVIEW_RESOLUTION_M,
                partition_size_cells=WORLD_OVERVIEW_WINDOW_CELLS,
                partition_overlap_cells=WORLD_OVERVIEW_OVERLAP_CELLS,
            ),
        )
        overview_converter, overview_env, _ = overview_scenario.build(
            load_event_reports=False
        )
        for hidden_agent in overview_env.agents[1:]:
            hidden_agent.state.terminated = True
        overview_env.tile_size = WORLD_OVERVIEW_TILE_SIZE
        overview_geometry = world_pane.PaneGeometry.from_partition_metadata(
            overview_env.partition_metadata, WORLD_OVERVIEW_TILE_SIZE
        )
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
    if pane_overview:
        overview_bounds = overview_env.partition_metadata
        north_min, east_min = overview_bounds.ned_bounds_min
        north_max, east_max = overview_bounds.ned_bounds_max
        pane_summary = {
            "tile_size": WORLD_OVERVIEW_TILE_SIZE,
            "resolution_m": float(overview_converter.multigrid_resolution),
            "window_cells": int(overview_bounds.grid_width),
            "coverage_m": [float(north_max - north_min), float(east_max - east_min)],
            "ned_bounds": {
                "north": [float(north_min), float(north_max)],
                "east": [float(east_min), float(east_max)],
            },
            "frames": last_tick + 1,
            "note": (
                "fixed 2 km north-up overview; fog, drone, route, and evidence "
                "overlays follow the recorded run"
            ),
        }
    else:
        pane_summary = {
            "tile_size": world_pane.TILE_SIZE,
            "resolution_m": float(runtime.env.converter.multigrid_resolution),
            "window_cells": int(runtime.env.partition_metadata.grid_width),
            "frames": last_tick + 1,
            "note": (
                "partition-local window that follows the drone; fog, drone, "
                "route, and evidence overlays follow the recorded run"
            ),
        }
    metadata: list[dict[str, Any]] = []
    overlay_counts: list[dict[str, Any]] = []
    pose_errors: list[float] = []
    search_track: list[tuple[float, float]] = []
    track_collecting = False
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
            # Reproject the authoritative NED pose and path into a fixed,
            # coarse partition rather than cropping around the drone.
            north, east, down = actual
            if pane_overview:
                overview_x, overview_y = overview_converter.ned_to_grid(
                    north, east, overview_env.partition_metadata
                )
                if not (
                    0 <= overview_x < overview_env.width
                    and 0 <= overview_y < overview_env.height
                ):
                    raise AssertionError(
                        f"recorded pose is outside the overview at t={now}: {actual}"
                    )
            if pane_overview:
                overview_env.agents[0].state.pos = np.asarray(
                    [overview_x, overview_y], dtype=int
                )
                direction = int(runtime.env.agents[0].state.dir)
                overview_env.agents[0].state.dir = direction
                overview_env.agent_ned_positions[0] = tuple(actual)
                overview_env.set_agent_ned_down(0, down)
                overview_env.step_count = tick
                overview_env._run_fog_update(
                    agent_id=0,
                    curr_ned_north=north,
                    curr_ned_east=east,
                    curr_ned_down=down,
                    curr_direction=direction,
                    prev_direction=overview_prev_direction,
                )
                overview_prev_direction = direction

            row = world_pane.section_at(run["worlds"], now)
            available = [t for t in run["worlds"] if t <= now - step]
            info = (
                row["world_model_info"]
                if row is not None
                else {"visible_ship_ids": [], "event_report_checks": []}
            )
            # Time-gated active-search layers: only the maneuver that is the
            # recorded active maneuver at this tick contributes, and the
            # breadcrumb track restarts with every new search activation.
            active = runtime._active
            active_action = (
                str(active.command.intent.action.value)
                if hasattr(active.command.intent.action, "value")
                else str(active.command.intent.action)
            ) if active is not None else None
            search_active = active_action == "search_area"
            search_polygon = None
            search_path_ned: list[tuple[float, float]] = []
            search_progress_text = None
            if search_active:
                parameters = active.command.intent.parameters
                search_polygon = [
                    [float(vertex["x"]), float(vertex["y"])]
                    for vertex in parameters["polygon"]
                ]
                for area_row, area_col in (
                    runtime.env.get_current_planned_paths().get("area_search", [])
                ):
                    path_north, path_east = runtime.env.converter.grid_to_ned(
                        area_col, area_row, runtime.env.partition_metadata
                    )
                    search_path_ned.append((path_north, path_east))
                cleared = active.progress.get("cleared_cells")
                total = active.progress.get("total_cells")
                search_progress_text = (
                    f"M4 SEARCH {active.phase} · {cleared}/{total} cells"
                    if isinstance(cleared, int) and isinstance(total, int)
                    else f"M4 SEARCH {active.phase}"
                )
                if not track_collecting:
                    search_track = []
                    track_collecting = True
                search_track.append((north, east))
            else:
                track_collecting = False
                search_track = []
            planned = list(
                runtime.env.get_current_planned_paths().get("navigation", [])
            )
            if pane_overview:
                overview_planned = []
                for row_index, col_index in planned:
                    path_north, path_east = runtime.env.converter.grid_to_ned(
                        col_index, row_index, runtime.env.partition_metadata
                    )
                    path_x, path_y = overview_converter.ned_to_grid(
                        path_north, path_east, overview_env.partition_metadata
                    )
                    if (
                        0 <= path_x < overview_env.width
                        and 0 <= path_y < overview_env.height
                    ):
                        overview_planned.append((path_y, path_x))
                if len(overview_planned) != len(planned):
                    raise AssertionError(
                        f"planned route leaves the fixed overview at t={now}"
                    )
                frame = Image.fromarray(
                    overview_env.render(
                        show_fog=True,
                        fog_unseen_brightness=0.42,
                        path_visualize=bool(overview_planned),
                        planned_grid_path=overview_planned,
                    )
                ).convert("RGB")
                pane_geometry = overview_geometry
                route_cells = len(overview_planned)
            else:
                runtime.env.tile_size = world_pane.TILE_SIZE
                frame = Image.fromarray(
                    runtime.env.render(
                        show_fog=True,
                        path_visualize=bool(planned),
                        planned_grid_path=planned,
                    )
                ).convert("RGB")
                pane_geometry = world_pane.PaneGeometry.from_partition_metadata(
                    runtime.env.partition_metadata, world_pane.TILE_SIZE
                )
                route_cells = len(planned)
            state = world_pane.pane_state(row, snapshots, now)
            if search_active:
                state = replace(
                    state,
                    active_search_polygon=search_polygon,
                    search_path=tuple(search_path_ned),
                    drone_track=tuple(search_track),
                    drone_position=(north, east),
                    search_progress=search_progress_text,
                )
            counts = world_pane.draw_overlays(frame, pane_geometry, state)
            counts["route_cells"] = route_cells
            overlay_counts.append({"tick": tick, "mission_time": now, **counts})
            frame.save(frames / f"{tick:04d}.png")
            if pane_overview:
                drone_pixel = [
                    (overview_x + 0.5) * WORLD_OVERVIEW_TILE_SIZE,
                    (overview_y + 0.5) * WORLD_OVERVIEW_TILE_SIZE,
                ]
            else:
                grid = list(map(int, runtime.env.agents[0].state.pos))
                drone_pixel = [
                    (grid[0] + 0.5) * world_pane.TILE_SIZE,
                    (grid[1] + 0.5) * world_pane.TILE_SIZE,
                ]
            metadata.append(
                {
                    "tick": tick,
                    "mission_time": now,
                    "position_ned": actual,
                    "drone_pixel": drone_pixel,
                    "visible_ship_ids": info["visible_ship_ids"],
                    "checks": len(info["event_report_checks"]),
                    "sensor_time": max(available) if available else 0.0,
                    "m4_coverage_pct": world_pane.dock_coverage_pct(row),
                    "m4_search_phase": active.phase if search_active else None,
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
                if pane_overview:
                    overview_converter.advance_fog_of_war_step()
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
    inspection = acceptance["mission3_evidence"]["inspection"]
    ship_ids = sorted(int(ship_id) for ship_id in inspection["selected_ship_ids"])
    evidence_count = len(inspection["evidence"])
    mission4_statuses = {
        task["target_id"]: task["status"]
        for task in answer_metrics["tasks"]
        if task.get("target_id")
    }
    latest_info = run["worlds"][max(run["worlds"])]["world_model_info"]
    objective_labels = {
        str(target): objective_phrase(objective)
        for target, objective in (
            latest_info["mission4"].get("objectives") or {}
        ).items()
    }
    final_state = display_state(
        run["worlds"],
        intervals,
        replans,
        orders,
        terminal_state,
        duration - step,
        mission_end_s=duration,
    )
    final_state.update(
        {
            "mission_block": terminal_state,
            "m4_target_status": mission4_statuses,
            "m4_answer_metrics": answer_metrics["aggregate"],
            "m3_terminal_outcome": {
                "selected_ship_ids": ship_ids,
                "inspection_evidence_count": evidence_count,
            },
        }
    )
    timeline = build_timeline(
        run["worlds"],
        intervals,
        replans,
        orders,
        terminal_state,
        final_state,
        mission_end_s=duration,
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
        "aoi_trajectory": aoi_trajectory_audit(run_root, args.mission4_package),
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

    chapters = derive_chapters(
        mission_end_s=duration,
        terminal_state=terminal_state,
        ship_ids=ship_ids,
        inspection_evidence_count=evidence_count,
        mission4_statuses=mission4_statuses,
        mission4_labels=objective_labels,
        request_default_objective=objective_phrase(
            requests[0][1].get("objective") or {}
        ),
        requests=requests,
        replans=replans,
        orders=orders,
        worker_revisions=worker_revisions(
            run,
            (latest_info["mission4"].get("package") or {}).get(
                "mission_time_budget_s"
            ),
        ),
        maneuver_outcomes=dock_view_outcomes(run),
    )
    ending = derive_ending(
        run_label=run_root.name,
        mission_end_s=duration,
        terminal_state=terminal_state,
        ship_ids=ship_ids,
        inspection_evidence_count=evidence_count,
        plan_revision_count=len(run["result"]["plan_revisions"]),
        replans=replans,
        mission4_statuses=mission4_statuses,
        mission4_labels=objective_labels,
    )
    story = derive_storyboard(
        chapters,
        ending,
        source_run=str(run_root),
        scope=(
            "Offline reconstruction of the accepted joint34 live run "
            f"{run_root.name}; recorded LLM/tool decisions replayed exactly, "
            "no new live mission, model call, or AirSim recording."
        ),
    )
    (output / "story.json").write_text(
        json.dumps(story, indent=2) + "\n", encoding="utf-8"
    )

    terminal_receipt_state = receipt_final_state(
        terminal_state,
        int(final_state["m3_unresolved"]),
        len(ship_ids),
        mission4_statuses,
        objective_labels,
    )
    profile_document = {
        "profile": "joint34",
        "name": f"joint34-{run_root.name}",
        "source_run": str(run_root),
        "tick_range": [0, last_tick],
        "final_fsm_state": terminal_state,
        "simulated_duration_seconds": duration,
        "plan_revisions": list(run["result"]["plan_revisions"]),
        "replan_activation_times_s": replans,
        "mission4_statuses": mission4_statuses,
        "receipt_final_state": terminal_receipt_state,
        **video_expectations(
            output / "story.json", metadata, (0, last_tick)
        ),
    }
    (output / "video-profile.json").write_text(
        json.dumps(profile_document, indent=2) + "\n", encoding="utf-8"
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
        "overlay_layers": overlay_layer_receipt(
            overlay_counts,
            recorded_polygon_counts(run["worlds"]),
            pane_summary,
        ),
        "aoi_trajectory": metrics["aoi_trajectory"],
        "surface_alignment": (
            json.loads(args.surface_alignment.read_text(encoding="utf-8"))
            if args.surface_alignment is not None
            else None
        ),
        "scope": "Reconstructed from recorded accepted commands and worker requests; no new live mission.",
    }
    (output / "reconstruction-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
