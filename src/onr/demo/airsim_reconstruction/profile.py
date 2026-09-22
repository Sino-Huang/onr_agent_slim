"""Mission-specific render and validation profiles.

A ``MissionProfile`` supplies every mission-specific string, metric schema,
and validation constant the AirSim reconstruction renderer and validator
need.  The Mission 1 profile reproduces the accepted issue-65 video's exact
behavior; the Joint34 profile adapts the same composition to the recorded
``run.7jruzl`` joint mission without duplicating the pipeline.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
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


def _joint34_validate_metrics_factory(
    *,
    final_fsm_state: str,
    simulated_duration_seconds: float,
    plan_revisions: Sequence[int],
    replan_activation_times_s: Sequence[float],
    mission4_statuses: Mapping[str, str],
) -> Callable[[Mapping[str, Any]], None]:
    """Metrics gate pinning one recorded joint34 run's outcome."""

    expected_revisions = list(plan_revisions)
    expected_replans = list(replan_activation_times_s)
    expected_statuses = dict(mission4_statuses)

    def validate(metrics: Mapping[str, Any]) -> None:
        if metrics.get("final_fsm_state") != final_fsm_state:
            raise ValueError(f"joint34 metrics must end in {final_fsm_state}")
        if metrics.get("simulated_duration_seconds") != simulated_duration_seconds:
            raise ValueError(
                "joint34 metrics must span "
                f"{simulated_duration_seconds:g} seconds"
            )
        if list(metrics.get("plan_revisions") or []) != expected_revisions:
            raise ValueError(
                f"joint34 metrics must carry plan revisions {expected_revisions}"
            )
        if list(metrics.get("replan_activation_times_s") or []) != expected_replans:
            raise ValueError(
                f"joint34 replan activations must be t={expected_replans}"
            )
        timeline = metrics.get("timeline")
        if not isinstance(timeline, list) or not timeline:
            raise ValueError("metrics timeline must be a non-empty list")
        statuses = {
            task["target_id"]: task["status"]
            for task in metrics.get("mission4_answer_metrics", {}).get("tasks", [])
            if task.get("target_id")
        }
        if statuses != expected_statuses:
            raise ValueError(
                f"unexpected Mission 4 terminal statuses: {statuses}"
            )

    return validate


def _joint34_pause_subtitle(seconds: float, actual_wait_seconds: float) -> str:
    del actual_wait_seconds
    return f"Recorded in-tick decision → {seconds:g}s replay pause"


def _joint34_receipt_fields_factory(
    final_state: str,
) -> Callable[[Mapping[str, Any]], dict[str, str]]:
    def receipt_fields(metrics: Mapping[str, Any]) -> dict[str, str]:
        del metrics
        return {"final_state": final_state}

    return receipt_fields


_JOINT34_STRIP_LINES = (
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
)

_JOINT34_STATUS_LINES = (
    StatusLine("Active block: {mission_block}", 742, 19),
    StatusLine("M3 ships {m3_ids}: {m3_unresolved}/3 unresolved", 778, 19),
    StatusLine("M4 evidence tracks: {m4_track_list}", 814, 19),
    StatusLine("Dock coverage: {dock_coverage}", 850, 17),
)


def joint34_profile(
    *,
    name: str,
    tick_range: tuple[int, int],
    expected_frames: int,
    expected_window_counts: Sequence[int],
    expected_pause_times: Sequence[float],
    final_fsm_state: str,
    simulated_duration_seconds: float,
    plan_revisions: Sequence[int],
    replan_activation_times_s: Sequence[float],
    mission4_statuses: Mapping[str, str],
    receipt_final_state: str,
) -> MissionProfile:
    """Build a joint34 profile from one recorded run's derived expectations.

    Every mission-specific string and pane layout is shared; only the numbers
    and facts of the recorded run are parameters, so a second joint34 run
    validates without duplicating the profile.
    """

    return MissionProfile(
        name=name,
        tick_range=tick_range,
        availability_field="evidence_available_at",
        prepare_row=_joint34_row,
        validate_metrics=_joint34_validate_metrics_factory(
            final_fsm_state=final_fsm_state,
            simulated_duration_seconds=simulated_duration_seconds,
            plan_revisions=plan_revisions,
            replan_activation_times_s=replan_activation_times_s,
            mission4_statuses=mission4_statuses,
        ),
        strip_lines=_JOINT34_STRIP_LINES,
        status_lines=_JOINT34_STATUS_LINES,
        unmapped_ids_y=878,
        execution_title=(
            "Execute the joint schedule. Observe the reconstructed harbor."
        ),
        execution_banner="EXECUTION  /  SIMULATION ADVANCES AT 4x REPLAY SPEED",
        execution_body=(
            "The accepted maneuver executes against the recorded joint34 "
            "timeline. AirSim provides visualization only; canonical "
            "telemetry, mission evidence, and metric availability remain "
            "authoritative."
        ),
        ending_title=f"{final_fsm_state} | outcomes remain honest",
        ending_banner="MISSION FINISHED  /  SIMULATION STOPPED",
        pause_subtitle=_joint34_pause_subtitle,
        receipt_fields=_joint34_receipt_fields_factory(receipt_final_state),
        expected_frames=expected_frames,
        expected_window_counts=tuple(expected_window_counts),
        expected_pause_times=tuple(expected_pause_times),
        retention_receipt_fields={"final_state": receipt_final_state},
        metrics_label="JOINT MISSION EVIDENCE",
    )


JOINT34_RUN_FACTS: Mapping[str, Any] = {
    "tick_range": (0, 240),
    "final_fsm_state": "joint34-complete",
    "simulated_duration_seconds": 120.0,
    "plan_revisions": (1, 2, 3, 4),
    "replan_activation_times_s": (1.5, 40.5, 97.0),
    "mission4_statuses": {
        "worker:1": "incomplete",
        "worker:3": "found",
        "worker:5": "incomplete",
    },
    "receipt_final_state": (
        "joint34-complete; M3 unresolved 3/3; M4 blue found, "
        "red container and truck incomplete"
    ),
}

JOINT34 = joint34_profile(
    name="joint34",
    expected_frames=1408,
    expected_window_counts=(112, 112, 96, 112, 96, 112, 128, 160),
    expected_pause_times=(0.0, 1.5, 40.0, 40.5, 80.0, 97.0, 120.0),
    **JOINT34_RUN_FACTS,
)


def _document_value(document: Mapping[str, Any], key: str, kind: type) -> Any:
    if key not in document:
        raise ValueError(f"derived joint34 profile document is missing {key!r}")
    value = document[key]
    if not isinstance(value, kind):
        expected = getattr(kind, "__name__", str(kind))
        raise TypeError(
            f"derived joint34 profile {key!r} must be {expected}, "
            f"got {type(value).__name__}"
        )
    return value


def joint34_profile_from_document(document: Mapping[str, Any]) -> MissionProfile:
    """Build a joint34 profile from a derived bundle's expectations document.

    The document is written by ``scripts/derive_joint34_video_bundle.py`` from
    the recorded run's own artifacts, so the profile pins that run's frame
    expectations and terminal facts without cloning them by hand.
    """

    if not isinstance(document, Mapping):
        raise TypeError("derived joint34 profile document must be an object")
    tick_range = _document_value(document, "tick_range", list)
    if len(tick_range) != 2:
        raise ValueError("derived joint34 profile tick_range must have two ends")
    statuses = _document_value(document, "mission4_statuses", dict)
    name = str(document.get("name") or "joint34-derived")
    return joint34_profile(
        name=name,
        tick_range=(int(tick_range[0]), int(tick_range[1])),
        expected_frames=int(_document_value(document, "expected_frames", int)),
        expected_window_counts=_document_value(
            document, "expected_window_counts", list
        ),
        expected_pause_times=_document_value(
            document, "expected_pause_times", list
        ),
        final_fsm_state=str(_document_value(document, "final_fsm_state", str)),
        simulated_duration_seconds=float(
            _document_value(document, "simulated_duration_seconds", (int, float))
        ),
        plan_revisions=_document_value(document, "plan_revisions", list),
        replan_activation_times_s=_document_value(
            document, "replan_activation_times_s", list
        ),
        mission4_statuses={str(key): str(value) for key, value in statuses.items()},
        receipt_final_state=str(
            _document_value(document, "receipt_final_state", str)
        ),
    )


def load_profile_from_file(path: str | Path) -> MissionProfile:
    """Load a derived joint34 profile expectations document from disk."""

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("profile") != JOINT34.name:
        raise ValueError(
            "derived profile document must describe the joint34 profile, "
            f"got {document.get('profile')!r}"
        )
    return joint34_profile_from_document(document)


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
