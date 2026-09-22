"""Tests for the offline AirSim reconstruction renderer."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from onr.demo.airsim_reconstruction.render import (
    DISCLOSURE_PERCEPTION,
    DISCLOSURE_RECONSTRUCTION,
    DISCLOSURE_REGION,
    FRONT_INSET,
    HEIGHT,
    MAIN_PANE,
    METRIC_STRIP,
    TEXT_PANE,
    WIDTH,
    WORLD_PANE,
    FrameSpec,
    StoryChapter,
    accepted_command_at,
    annotate_front_frame,
    build_storyboard,
    compose_frame,
    encode_video,
    load_accepted_commands,
    load_capture_ticks,
    parse_tick_range,
)


def _segmentation(shape: tuple[int, int], object_id: int) -> np.ndarray:
    height, width = shape
    result = np.zeros((height, width, 3), dtype=np.uint8)
    result[..., 0] = (object_id >> 16) & 0xFF
    result[..., 1] = (object_id >> 8) & 0xFF
    result[..., 2] = object_id & 0xFF
    return result


def _metric() -> dict[str, object]:
    return {
        "issues_discovered": 9,
        "total_corrupted": 12,
        "issue_discovery_recall": 0.75,
        "balanced_MSE": 0.001133,
        "belief_available_at": 0.0,
    }


def test_overlay_composition_draws_expected_inclusive_bbox_pixels() -> None:
    rgb = np.zeros((12, 14, 3), dtype=np.uint8)
    seg = np.zeros_like(rgb)
    seg[3:8, 4:10] = _segmentation((1, 1), 70_001)[0, 0]

    annotated, result = annotate_front_frame(
        rgb, seg, {"ship 1": 70_001}, min_pixels=1
    )
    pixels = np.asarray(annotated)

    assert result.overlays["ship 1"].bbox.x0 == 4
    assert result.overlays["ship 1"].bbox.y0 == 3
    assert result.overlays["ship 1"].bbox.x1 == 9
    assert result.overlays["ship 1"].bbox.y1 == 7
    np.testing.assert_array_equal(pixels[3, 4], np.array([103, 222, 240]))
    np.testing.assert_array_equal(pixels[7, 9], np.array([103, 222, 240]))
    assert pixels[5, 7].any()


def test_compose_frame_draws_both_required_disclosure_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn_text: list[str] = []
    original = ImageDraw.ImageDraw.text

    def record_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        drawn_text.append(str(text))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    rgb = np.zeros((18, 32, 3), dtype=np.uint8)
    frame = compose_frame(
        rgb,
        np.zeros_like(rgb),
        rgb,
        {"mission_time": 0.0, "visible_ship_ids": [], "checks": 0},
        _metric(),
        {},
        FrameSpec(0, "execution"),
        ending_text="Mission complete.",
        world_frame=Image.new("RGB", (1600, 1600), (10, 20, 30)),
        accepted_command="Navigate to (464, 234)",
    )

    assert frame.size == (WIDTH, HEIGHT)
    assert DISCLOSURE_RECONSTRUCTION in drawn_text
    assert DISCLOSURE_PERCEPTION in drawn_text


def test_pause_frames_freeze_all_rendered_pixels() -> None:
    metadata = [
        {"tick": 0, "mission_time": 0.0},
        {"tick": 1, "mission_time": 0.5},
    ]
    chapter = StoryChapter(0, 0.0, 0.5, "Pause", "Frozen body", 3.0)
    storyboard = build_storyboard(
        metadata,
        [chapter],
        [0],
        (0, 0),
        fps=8,
        playback_speed=4.0,
        ending_seconds=0,
    )
    pause_specs = [spec for spec in storyboard if spec.kind == "pause"]
    rgb = np.zeros((18, 32, 3), dtype=np.uint8)
    frames = [
        compose_frame(
            rgb,
            np.zeros_like(rgb),
            rgb,
            {"mission_time": 0.0, "visible_ship_ids": [], "checks": 0},
            _metric(),
            {},
            spec,
            ending_text="Done",
        ).tobytes()
        for spec in pause_specs
    ]

    assert len(frames) == 4
    assert all(frame == frames[0] for frame in frames[1:])


def test_accepted_commands_join_feedback_with_command_intents(
    tmp_path: Path,
) -> None:
    commands = tmp_path / "physical-state" / "commands"
    feedback = tmp_path / "physical-state" / "feedback"
    commands.mkdir(parents=True)
    feedback.mkdir()
    (commands / "00000001-command.json").write_text(
        json.dumps(
            {
                "command_id": "cmd-1",
                "intent": {
                    "action": "navigate",
                    "parameters": {"target": {"x": 464, "y": 234, "z": -25.0}},
                },
            }
        ),
        encoding="utf-8",
    )
    (commands / "00000002-command.json").write_text(
        json.dumps(
            {
                "command_id": "cmd-2",
                "intent": {"action": "pursue", "parameters": {"entity_id": 5}},
            }
        ),
        encoding="utf-8",
    )
    (feedback / "00000001-feedback.json").write_text(
        json.dumps(
            {"command_id": "cmd-1", "phase": "accepted", "mission_time_s": 0.0}
        ),
        encoding="utf-8",
    )
    (feedback / "00000002-feedback.json").write_text(
        json.dumps(
            {"command_id": "cmd-2", "phase": "accepted", "mission_time_s": 17.5}
        ),
        encoding="utf-8",
    )
    (feedback / "00000003-feedback.json").write_text(
        json.dumps(
            {"command_id": "cmd-2", "phase": "completed", "mission_time_s": 30.0}
        ),
        encoding="utf-8",
    )

    accepted = load_accepted_commands(tmp_path)

    assert accepted == [
        (0.0, "Navigate to (464, 234)"),
        (17.5, "Pursue ship 5"),
    ]
    assert accepted_command_at(accepted, -1.0) == "Awaiting first command"
    assert accepted_command_at(accepted, 10.0) == "Navigate to (464, 234)"
    assert accepted_command_at(accepted, 20.0) == "Pursue ship 5"
    assert load_accepted_commands(tmp_path / "missing") == []


def test_pane_rectangles_stay_inside_the_frame() -> None:
    for pane in (
        MAIN_PANE,
        FRONT_INSET,
        WORLD_PANE,
        TEXT_PANE,
        METRIC_STRIP,
        DISCLOSURE_REGION,
    ):
        left, top, right, bottom = pane
        assert 0 <= left < right <= WIDTH
        assert 0 <= top < bottom <= HEIGHT


def test_capture_manifest_range_selection_skips_incomplete_ticks(
    tmp_path: Path,
) -> None:
    for tick in (1, 2):
        for kind in ("front-rgb", "front-seg", "third-rgb"):
            if tick == 2 and kind == "third-rgb":
                continue
            Image.new("RGB", (4, 3)).save(tmp_path / f"tick-{tick:04}-{kind}.png")
    manifest = {
        "ticks": {
            str(tick): {
                "mission_time_s": tick * 0.5,
                "images": [
                    {"kind": kind, "path": f"tick-{tick:04}-{kind}.png"}
                    for kind in ("front-rgb", "front-seg", "third-rgb")
                ],
            }
            for tick in range(3)
        }
    }
    manifest_path = tmp_path / "capture-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    captures = load_capture_ticks(manifest_path, parse_tick_range("1-2"))

    assert set(captures) == {1}
    assert captures[1].mission_time_s == 0.5


def test_storyboard_frame_count_matches_approved_pause_and_motion_rules() -> None:
    metadata = [
        {"tick": tick, "mission_time": tick * 0.5} for tick in range(4)
    ]
    chapter = StoryChapter(1, 0.5, 2.0, "Pause", "Body", 10.0)

    storyboard = build_storyboard(
        metadata,
        [chapter],
        range(4),
        (0, 3),
        fps=16,
        playback_speed=4.0,
        ending_seconds=1.0,
    )

    assert len(storyboard) == 3 * 2 + 2 * 16 + 16
    assert sum(spec.kind == "pause" for spec in storyboard) == 32
    assert sum(spec.kind == "execution" for spec in storyboard) == 6
    assert sum(spec.kind == "ending" for spec in storyboard) == 16


def test_encode_video_invokes_ffmpeg_mjpeg_pipe(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Sink(io.BytesIO):
        def close(self) -> None:
            self.was_closed = True

    class Process:
        def __init__(self) -> None:
            self.stdin = Sink()

        def wait(self) -> int:
            return 0

    process = Process()

    def popen(arguments: list[str], **kwargs: object) -> Any:
        calls.append((arguments, kwargs))
        return process

    image = Image.new("RGB", (WIDTH, HEIGHT), (1, 2, 3))
    count = encode_video(
        [image],
        tmp_path / "video.webm",
        ffmpeg_path=Path("/mock/ffmpeg"),
        popen_factory=popen,
    )

    arguments, kwargs = calls[0]
    assert count == 1
    assert arguments == [
        "/mock/ffmpeg",
        "-y",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-r",
        "16",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libvpx",
        "-b:v",
        "3000k",
        "-deadline",
        "good",
        "-cpu-used",
        "4",
        str(tmp_path / "video.webm"),
    ]
    assert kwargs["stdin"] == -1
    with Image.open(io.BytesIO(process.stdin.getvalue())) as decoded:
        assert decoded.size == (WIDTH, HEIGHT)


def test_encode_video_uses_custom_bitrate_and_cpu_used(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Sink(io.BytesIO):
        def close(self) -> None:
            pass

    class Process:
        stdin = Sink()

        def wait(self) -> int:
            return 0

    def popen(arguments: list[str], **_kwargs: object) -> Any:
        calls.append(arguments)
        return Process()

    encode_video(
        [Image.new("RGB", (WIDTH, HEIGHT))],
        tmp_path / "custom.webm",
        ffmpeg_path=Path("/mock/ffmpeg"),
        bitrate_kbps=8000,
        cpu_used=1,
        popen_factory=popen,
    )

    arguments = calls[0]
    assert arguments[arguments.index("-b:v") + 1] == "8000k"
    assert arguments[arguments.index("-cpu-used") + 1] == "1"
