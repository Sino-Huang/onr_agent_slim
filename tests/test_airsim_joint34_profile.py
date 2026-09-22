"""Behavior tests for the mission-profile seam and the Joint34 bundle."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from onr.demo.airsim_reconstruction.profile import (
    JOINT34,
    MISSION1,
    load_profile,
)
from onr.demo.airsim_reconstruction.render import (
    FrameSpec,
    StoryChapter,
    _metric_at_time,
    build_storyboard,
    compose_frame,
    load_dynamic_object_ids,
    load_metrics,
)
from onr.demo.airsim_reconstruction.validate import (
    metric_availability_by_frame,
    storyboard_hold_windows,
)

_REPO = Path(__file__).resolve().parents[1]
_BUNDLE = _REPO / "var/demo-video/joint34-20260922-airsim/bundle"


def _load_derivation_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "derive_joint34_video_bundle",
        _REPO / "scripts/derive_joint34_video_bundle.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _joint34_timeline_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "mission_time_seconds": 0.0,
        "evidence_available_at": 0.0,
        "mission_block": "scheduling",
        "plan_revision": 1,
        "plan_order": "defer M3+M4",
        "replan_count": 0,
        "worker_revision": 1,
        "worker_deadline_s": 300.0,
        "m3_unresolved": 3,
        "m3_selected_ship_ids": [7, 15, 16],
        "m4_tracks": ["red", "blue"],
    }
    row.update(overrides)
    return row


def _frame_input(
    rgb: np.ndarray, metric_row: dict[str, Any], profile: Any = JOINT34
) -> tuple[Any, ...]:
    return (
        rgb,
        np.zeros_like(rgb),
        rgb,
        {"mission_time": 0.0, "visible_ship_ids": [], "checks": 0},
        metric_row,
        {},
        FrameSpec(0, "execution"),
    )


class TestProfileSeam:
    def test_load_profile_rejects_unknown_names(self) -> None:
        with pytest.raises(ValueError, match="unknown mission profile"):
            load_profile("mission2")

    def test_built_in_profiles_cover_both_missions(self) -> None:
        assert load_profile("mission1") is MISSION1
        assert load_profile("joint34") is JOINT34
        assert MISSION1.tick_range == (0, 599)
        assert JOINT34.tick_range == (0, 240)

    def test_mission1_strip_templates_render_the_accepted_fields(self) -> None:
        row = {
            "issues_discovered": 9,
            "total_corrupted": 12,
            "issue_discovery_recall": 0.75,
            "balanced_MSE": 0.0011334847,
            "belief_available_at": 17.0,
        }
        fields = MISSION1.prepare_row(row)
        texts = [line.template.format(**fields) for line in MISSION1.strip_lines]
        assert texts[0] == "OFFLINE METRICS  /  Recall: 9/12 (75.0%)"
        assert texts[1] == "Balanced MSE: 0.001133"
        assert texts[2] == "Belief available: 17.0 s"

    def test_joint34_strip_and_status_templates_render(self) -> None:
        fields = JOINT34.prepare_row(_joint34_timeline_row())
        strip = [line.template.format(**fields) for line in JOINT34.strip_lines]
        assert strip[0] == "JOINT MISSION  /  SCHEDULING · rev 1 · defer M3+M4"
        assert "due 300s" in strip[1]
        assert "replans 0" in strip[1]
        assert strip[2] == "M3 unresolved 3/3 · M4 tracks red blue"
        status = {
            **fields,
            "visible_ships": "none",
            "checks": 0,
        }
        rendered = [line.template.format(**status) for line in JOINT34.status_lines]
        assert rendered[0] == "Active block: SCHEDULING"
        assert rendered[1] == "M3 ships 7/15/16: 3/3 unresolved"
        assert rendered[2] == "M4 evidence tracks: red blue"

    def test_joint34_metrics_gate_rejects_wrong_outcomes(self) -> None:
        base = {
            "final_fsm_state": "joint34-complete",
            "simulated_duration_seconds": 120.0,
            "plan_revisions": [1, 2, 3, 4],
            "replan_activation_times_s": [1.5, 40.5, 97.0],
            "timeline": [ _joint34_timeline_row() ],
            "mission4_answer_metrics": {
                "tasks": [
                    {"target_id": "worker:1", "status": "incomplete"},
                    {"target_id": "worker:3", "status": "found"},
                    {"target_id": "worker:5", "status": "incomplete"},
                ]
            },
        }
        JOINT34.validate_metrics(base)
        wrong_state = {**base, "final_fsm_state": "mission3-block"}
        with pytest.raises(ValueError, match="joint34-complete"):
            JOINT34.validate_metrics(wrong_state)
        wrong_replans = {**base, "replan_activation_times_s": [1.5]}
        with pytest.raises(ValueError, match="replan"):
            JOINT34.validate_metrics(wrong_replans)
        wrong_status = json.loads(json.dumps(base))
        wrong_status["mission4_answer_metrics"]["tasks"][0]["status"] = "found"
        with pytest.raises(ValueError, match="Mission 4 terminal statuses"):
            JOINT34.validate_metrics(wrong_status)

    def test_mission1_gate_still_pins_the_approved_recall(self, tmp_path: Path) -> None:
        metrics = {
            "issue_discovery_recall": {"discovered": 8, "total_corrupted": 12},
            "timeline": [{"mission_time_seconds": 0.0}],
        }
        path = tmp_path / "mission-metrics.json"
        path.write_text(json.dumps(metrics))
        with pytest.raises(ValueError, match="9/12"):
            load_metrics(path)
        metrics["issue_discovery_recall"]["discovered"] = 9
        path.write_text(json.dumps(metrics))
        assert load_metrics(path) == metrics

    def test_compose_frame_draws_joint34_block_and_mission1_recall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drawn: list[str] = []
        original = ImageDraw.ImageDraw.text

        def record_text(
            draw: ImageDraw.ImageDraw,
            xy: Any,
            text: str,
            *args: Any,
            **kwargs: Any
        ) -> Any:
            drawn.append(str(text))
            return original(draw, xy, text, *args, **kwargs)

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
        rgb = np.zeros((18, 32, 3), dtype=np.uint8)

        compose_frame(
            *_frame_input(rgb, _joint34_timeline_row()),
            ending_text="done",
            profile=JOINT34,
        )
        assert any("JOINT MISSION" in text for text in drawn)
        assert any("Active block: SCHEDULING" in text for text in drawn)

        drawn.clear()
        mission1_row = {
            "issues_discovered": 9,
            "total_corrupted": 12,
            "issue_discovery_recall": 0.75,
            "balanced_MSE": 0.25,
            "belief_available_at": 0.0,
        }
        compose_frame(
            rgb,
            np.zeros_like(rgb),
            rgb,
            {"mission_time": 0.0, "visible_ship_ids": [], "checks": 3},
            mission1_row,
            {},
            FrameSpec(0, "execution"),
            ending_text="done",
        )
        assert any("OFFLINE METRICS" in text for text in drawn)
        assert any("Report checks: 3" in text for text in drawn)
        assert any("World-model visible ships: none" in text for text in drawn)

    def test_static_objects_map_to_actor_labels(self, tmp_path: Path) -> None:
        mapping = {
            "ships": {"7": {"object_id": 7}},
            "passengers": {"21_Cone": {"object_id": 21}},
            "static_objects": {
                "M4 red container": {"object_id": 47},
                "M4 blue container": {"object_id": 48},
            },
        }
        path = tmp_path / "mapping.json"
        path.write_text(json.dumps(mapping))
        ids = load_dynamic_object_ids(path)
        assert ids["ship 7"] == 7
        assert ids["passenger 21_Cone"] == 21
        assert ids["M4 red container"] == 47
        assert ids["M4 blue container"] == 48


class TestJoint34StoryboardAndTiming:
    def _storyboard(self) -> tuple[Any, ...]:
        metadata = [
            {"tick": tick, "mission_time": tick * 0.5} for tick in range(241)
        ]
        chapters = [
            StoryChapter(
                tick=round(time_s * 2),
                mission_time_s=time_s,
                seconds=seconds,
                title=f"Chapter {index}",
                body="Body",
                actual_wait_seconds=0.0,
            )
            for index, (time_s, seconds) in enumerate(
                [(0.0, 7), (1.5, 7), (40.0, 6), (40.5, 7), (80.0, 6), (97.0, 7), (120.0, 8)]
            )
        ]
        return build_storyboard(
            metadata, chapters, range(241), (0, 240), ending_seconds=10.0
        )

    def test_storyboard_frame_count_matches_expected_profile_value(self) -> None:
        storyboard = self._storyboard()
        assert len(storyboard) == JOINT34.expected_frames
        assert len(storyboard) == 1408

    def test_hold_windows_match_expected_counts_and_pause_times(self) -> None:
        windows = storyboard_hold_windows(self._storyboard())
        assert [window.frame_count for window in windows] == list(
            JOINT34.expected_window_counts
        )
        pauses = [w for w in windows if w.kind == "pause"]
        assert [w.mission_time_s for w in pauses] == list(
            JOINT34.expected_pause_times
        )

    def test_metric_availability_transitions_only_at_boundaries(self) -> None:
        storyboard = self._storyboard()
        metadata = {spec.tick: {"mission_time": spec.tick * 0.5} for spec in storyboard}
        timeline = [
            _joint34_timeline_row(
                mission_time_seconds=t, evidence_available_at=t
            )
            for t in (0.0, 1.5, 40.5, 97.0, 120.0)
        ]
        availability = metric_availability_by_frame(
            storyboard, metadata, timeline, JOINT34.availability_field
        )
        assert len(availability) == len(storyboard)
        # The strip may only change when the evidence boundary is crossed:
        # every distinct availability value must be one of the recorded times.
        assert set(availability) <= {0.0, 1.5, 40.5, 97.0, 120.0}
        # Frame 0 carries the initial evidence; the final chapter freezes the
        # terminal state until the ending.
        assert availability[0] == 0.0
        assert availability[-1] == 120.0
        changes = [
            index
            for index, (before, after) in enumerate(
                zip(availability, availability[1:]), start=1
            )
            if before != after
        ]
        assert changes, "availability must advance across the video"
        for index in changes:
            assert availability[index] in {1.5, 40.5, 97.0, 120.0}

    def test_metric_at_time_honors_joint34_availability_field(self) -> None:
        metrics = {
            "timeline": [
                _joint34_timeline_row(
                    mission_time_seconds=t,
                    evidence_available_at=t,
                    plan_revision=2 if t >= 1.5 else 1,
                )
                for t in (0.0, 1.5, 40.5)
            ]
        }
        assert (
            _metric_at_time(metrics, 1.0, JOINT34.availability_field)[
                "plan_revision"
            ]
            == 1
        )
        assert (
            _metric_at_time(metrics, 2.0, JOINT34.availability_field)[
                "plan_revision"
            ]
            == 2
        )
        with pytest.raises(ValueError):
            _metric_at_time(metrics, -1.0, JOINT34.availability_field)


class TestBundleDerivation:
    def test_replan_times_filters_replan_windows(self) -> None:
        module = _load_derivation_module()
        windows = [
            {"role": "hyper", "completion_time_seconds": 0.5},
            {"role": "replan", "completion_time_seconds": 40.5},
            {"role": "maneuver", "completion_time_seconds": 1.5},
            {"role": "replan", "completion_time_seconds": 1.5},
        ]
        assert module.replan_times(windows) == [1.5, 40.5]

    def test_block_at_follows_active_maneuver_intervals(self) -> None:
        module = _load_derivation_module()
        intervals = [
            (1.5, 5.5, "mission4-block"),
            (5.5, 40.5, "mission3-block"),
            (100.5, float("inf"), "mission3-block"),
        ]
        assert module.block_at(intervals, "joint34-complete", 0.0) == "scheduling"
        assert module.block_at(intervals, "joint34-complete", 2.0) == "mission4-block"
        assert module.block_at(intervals, "joint34-complete", 20.0) == "mission3-block"
        assert module.block_at(intervals, "joint34-complete", 101.0) == "mission3-block"
        assert (
            module.block_at(intervals, "joint34-complete", 120.0)
            == "joint34-complete"
        )

    def test_display_state_never_uses_future_evidence(self) -> None:
        module = _load_derivation_module()

        def world(worker_revision: int) -> dict[str, Any]:
            return {
                "world_model_info": {
                    "mission3": {"ships": [], "selected_ship_ids": [7, 15, 16]},
                    "mission4": {"revision": worker_revision, "observations": []},
                }
            }

        worlds = {0.0: world(1), 40.0: world(3)}
        state = module.display_state(
            worlds, [], [], {1: "defer"}, "joint34-complete", 10.0
        )
        assert state["worker_revision"] == 1
        state = module.display_state(
            worlds, [], [], {1: "defer"}, "joint34-complete", 40.0
        )
        assert state["worker_revision"] == 3

    def test_build_timeline_collapses_unchanged_rows(self) -> None:
        module = _load_derivation_module()
        worlds = {
            0.0: {
                "world_model_info": {
                    "mission3": {"ships": [], "selected_ship_ids": []},
                    "mission4": {"revision": 1, "observations": []},
                }
            },
            0.5: {
                "world_model_info": {
                    "mission3": {"ships": [], "selected_ship_ids": []},
                    "mission4": {"revision": 1, "observations": []},
                }
            },
            1.0: {
                "world_model_info": {
                    "mission3": {"ships": [], "selected_ship_ids": []},
                    "mission4": {"revision": 2, "observations": []},
                }
            },
        }
        timeline = module.build_timeline(worlds, [], [], {1: "defer"}, "joint34-complete", {})
        assert [row["evidence_available_at"] for row in timeline] == [
            0.0,
            1.0,
            120.0,
        ]
        assert timeline[-1]["mission_time_seconds"] == 120.0

    @pytest.mark.skipif(not (_BUNDLE / "mission-metrics.json").is_file(), reason="derived bundle not present")
    def test_derived_bundle_matches_profile_expectations(self) -> None:
        metrics = json.loads((_BUNDLE / "mission-metrics.json").read_text())
        JOINT34.validate_metrics(metrics)
        story = json.loads((_BUNDLE / "story.json").read_text())
        assert [c["time"] for c in story["reasoning_chapters"]] == list(
            JOINT34.expected_pause_times
        )
        receipt = json.loads(
            (_BUNDLE / "reconstruction-receipt.json").read_text()
        )
        assert receipt["state_comparisons"] == 240
        assert receipt["world_snapshot_comparisons"] == 240
        assert receipt["maximum_pose_error_m"] == 0.0
        metadata = json.loads((_BUNDLE / "frame-metadata.json").read_text())
        assert [row["tick"] for row in metadata] == list(range(241))
