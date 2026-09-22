"""Mission-specific render and validation profiles.

A ``MissionProfile`` supplies every mission-specific string, metric schema,
and validation constant the AirSim reconstruction renderer and validator
need.  The Mission 1 profile reproduces the accepted issue-65 video's exact
behavior; the Joint34 profile adapts the same composition to the recorded
``run.7jruzl`` joint mission without duplicating the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StripLine:
    """One evidence-timed line of the metric strip."""

    template: str
    xy: tuple[int, int]
    size: int
    fill: str


@dataclass(frozen=True, slots=True)
class StatusLine:
    """One right-pane status line rendered below the accepted command."""

    template: str
    y: int
    size: int
    fill: str = "muted"


@dataclass(frozen=True, slots=True)
class MissionProfile:
    """Everything mission-specific about composing and validating a video."""

    name: str
    tick_range: tuple[int, int]
    availability_field: str
    prepare_row: Callable[[Mapping[str, Any]], dict[str, Any]]
    validate_metrics: Callable[[Mapping[str, Any]], None]
    strip_lines: tuple[StripLine, ...]
    status_lines: tuple[StatusLine, ...]
    unmapped_ids_y: int
    execution_title: str
    execution_banner: str
    execution_body: str
    ending_title: str
    ending_banner: str
    pause_subtitle: Callable[[float, float], str]
    receipt_fields: Callable[[Mapping[str, Any]], dict[str, str]]
    expected_frames: int
    expected_window_counts: Sequence[int]
    expected_pause_times: Sequence[float]
    retention_receipt_fields: Mapping[str, str]
    metrics_label: str = "OFFLINE METRICS"


MUTED = "#a2b7cf"
CYAN = "#67def0"
AMBER = "#ffd078"


def _mission1_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "recall_discovered": int(row["issues_discovered"]),
        "recall_total": int(row["total_corrupted"]),
        "recall_pct": 100.0 * float(row["issue_discovery_recall"]),
        "balanced_MSE": float(row["balanced_MSE"]),
        "belief_available_at": float(row["belief_available_at"]),
    }


def _mission1_validate_metrics(metrics: Mapping[str, Any]) -> None:
    recall = metrics.get("issue_discovery_recall", {})
    if recall.get("discovered") != 9 or recall.get("total_corrupted") != 12:
        raise ValueError("approved Mission 1 recall must remain exactly 9/12")
    timeline = metrics.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        raise ValueError("metrics timeline must be a non-empty list")


def _mission1_pause_subtitle(seconds: float, actual_wait_seconds: float) -> str:
    return f"Recorded wait {actual_wait_seconds:.1f}s → {seconds:g}s replay"


def _mission1_receipt_fields(metrics: Mapping[str, Any]) -> dict[str, str]:
    del metrics
    return {"final_recall": "9/12 (75%)"}


MISSION1 = MissionProfile(
    name="mission1",
    tick_range=(0, 599),
    availability_field="belief_available_at",
    prepare_row=_mission1_row,
    validate_metrics=_mission1_validate_metrics,
    strip_lines=(
        StripLine(
            "OFFLINE METRICS  /  Recall: {recall_discovered}/{recall_total} "
            "({recall_pct:.1f}%)",
            (62, 927),
            25,
            AMBER,
        ),
        StripLine("Balanced MSE: {balanced_MSE:.6f}", (820, 929), 23, CYAN),
        StripLine(
            "Belief available: {belief_available_at:.1f} s", (1340, 932), 18, MUTED
        ),
    ),
    status_lines=(
        StatusLine(
            "World-model visible ships: {visible_ships}", 742, 19
        ),
        StatusLine("Report checks: {checks}", 778, 19),
    ),
    unmapped_ids_y=814,
    execution_title="Execute the decision. Observe the reconstructed harbor.",
    execution_banner="EXECUTION  /  SIMULATION ADVANCES AT 4x REPLAY SPEED",
    execution_body=(
        "The accepted maneuver executes against the recorded Mission 1 "
        "timeline. AirSim provides visualization only; canonical telemetry "
        "and metric availability remain authoritative."
    ),
    ending_title="Mission complete | uncertainty remains explicit",
    ending_banner="MISSION FINISHED  /  SIMULATION STOPPED",
    pause_subtitle=_mission1_pause_subtitle,
    receipt_fields=_mission1_receipt_fields,
    expected_frames=1966,
    expected_window_counts=(128, 128, 96, 128, 128, 160),
    expected_pause_times=(0.0, 174.0, 187.0, 210.5, 299.5),
    retention_receipt_fields={"final_recall": "9/12 (75%)"},
)


_BLOCK_LABELS = {
    "scheduling": "SCHEDULING",
    "mission4-block": "M4 BLOCK · dock views",
    "mission3-block": "M3 BLOCK · ship screening",
    "joint34-complete": "JOINT34-COMPLETE",
}


def _joint34_row(row: Mapping[str, Any]) -> dict[str, Any]:
    deadline = row.get("worker_deadline_s")
    tracks = list(row.get("m4_tracks") or [])
    block = str(row["mission_block"])
    return {
        "mission_block": _BLOCK_LABELS.get(block, block),
        "plan_revision": int(row["plan_revision"]),
        "plan_order": str(row["plan_order"]),
        "replan_count": int(row["replan_count"]),
        "worker_revision": int(row["worker_revision"]),
        "request_label": str(row.get("worker_request_label", "none")),
        "deadline_label": "none" if deadline is None else f"{float(deadline):g}s",
        "m3_unresolved": int(row["m3_unresolved"]),
        "m3_ids": "/".join(str(v) for v in row.get("m3_selected_ship_ids") or []),
        "m4_track_count": len(tracks),
        "m4_track_list": " ".join(tracks) if tracks else "none",
    }


def _joint34_validate_metrics(metrics: Mapping[str, Any]) -> None:
    if metrics.get("final_fsm_state") != "joint34-complete":
        raise ValueError("joint34 metrics must end in joint34-complete")
    if metrics.get("simulated_duration_seconds") != 120.0:
        raise ValueError("joint34 metrics must span 120.0 seconds")
    if list(metrics.get("plan_revisions") or []) != [1, 2, 3, 4]:
        raise ValueError("joint34 metrics must carry plan revisions 1-4")
    if list(metrics.get("replan_activation_times_s") or []) != [1.5, 40.5, 97.0]:
        raise ValueError("joint34 replan activations must be t=1.5, 40.5, 97.0")
    timeline = metrics.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        raise ValueError("metrics timeline must be a non-empty list")
    statuses = {
        task["target_id"]: task["status"]
        for task in metrics.get("mission4_answer_metrics", {}).get("tasks", [])
    }
    if statuses != {
        "worker:1": "incomplete",
        "worker:3": "found",
        "worker:5": "incomplete",
    }:
        raise ValueError(f"unexpected Mission 4 terminal statuses: {statuses}")


def _joint34_pause_subtitle(seconds: float, actual_wait_seconds: float) -> str:
    del actual_wait_seconds
    return f"Recorded in-tick decision → {seconds:g}s replay pause"


def _joint34_receipt_fields(metrics: Mapping[str, Any]) -> dict[str, str]:
    del metrics
    return {
        "final_state": (
            "joint34-complete; M3 unresolved 3/3; M4 blue found, "
            "red container and truck incomplete"
        )
    }


JOINT34 = MissionProfile(
    name="joint34",
    tick_range=(0, 240),
    availability_field="evidence_available_at",
    prepare_row=_joint34_row,
    validate_metrics=_joint34_validate_metrics,
    strip_lines=(
        StripLine(
            "JOINT MISSION  /  {mission_block} · rev {plan_revision} · "
            "{plan_order}",
            (62, 927),
            21,
            AMBER,
        ),
        StripLine(
            "WORKER rev {worker_revision} ({request_label}) · due "
            "{deadline_label} · replans {replan_count}",
            (795, 929),
            19,
            CYAN,
        ),
        StripLine(
            "M3 unresolved {m3_unresolved}/3 · M4 tracks {m4_track_list}",
            (1430, 932),
            15,
            MUTED,
        ),
    ),
    status_lines=(
        StatusLine("Active block: {mission_block}", 742, 19),
        StatusLine(
            "M3 ships {m3_ids}: {m3_unresolved}/3 unresolved", 778, 19
        ),
        StatusLine(
            "M4 evidence tracks: {m4_track_list}", 814, 19
        ),
    ),
    unmapped_ids_y=846,
    execution_title=(
        "Execute the joint schedule. Observe the reconstructed harbor."
    ),
    execution_banner="EXECUTION  /  SIMULATION ADVANCES AT 4x REPLAY SPEED",
    execution_body=(
        "The accepted maneuver executes against the recorded joint34 "
        "timeline. AirSim provides visualization only; canonical telemetry, "
        "mission evidence, and metric availability remain authoritative."
    ),
    ending_title="joint34-complete | outcomes remain honest",
    ending_banner="MISSION FINISHED  /  SIMULATION STOPPED",
    pause_subtitle=_joint34_pause_subtitle,
    receipt_fields=_joint34_receipt_fields,
    expected_frames=1408,
    expected_window_counts=(112, 112, 96, 112, 96, 112, 128, 160),
    expected_pause_times=(0.0, 1.5, 40.0, 40.5, 80.0, 97.0, 120.0),
    retention_receipt_fields={
        "final_state": (
            "joint34-complete; M3 unresolved 3/3; M4 blue found, "
            "red container and truck incomplete"
        )
    },
    metrics_label="JOINT MISSION EVIDENCE",
)

PROFILES: Mapping[str, MissionProfile] = {
    MISSION1.name: MISSION1,
    JOINT34.name: JOINT34,
}


def load_profile(name: str) -> MissionProfile:
    """Return the named built-in mission profile."""
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown mission profile {name!r}; expected one of "
            f"{sorted(PROFILES)}"
        ) from exc
