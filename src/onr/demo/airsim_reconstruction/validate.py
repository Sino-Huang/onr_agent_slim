"""Validate the retained 1080p AirSim reconstruction video."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import quote

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

from .render import (
    DEFAULT_METADATA,
    DEFAULT_METRICS,
    DEFAULT_OUTPUT,
    DEFAULT_STORYBOARD,
    DISCLOSURE_PERCEPTION,
    DISCLOSURE_RECONSTRUCTION,
    DISCLOSURE_REGION,
    FPS,
    HEIGHT,
    METRIC_STRIP,
    TEXT_PANE,
    WIDTH,
    FrameSpec,
    _font,
    build_storyboard,
    load_capture_ticks,
    load_storyboard,
)
from .profile import MISSION1, MissionProfile, load_profile, load_profile_from_file

EXPECTED_FRAMES = 1966
EXPECTED_DURATION_SECONDS = EXPECTED_FRAMES / FPS
DEFAULT_CAPTURE_MANIFEST = Path(
    "var/demo-video/mission1-20260916-airsim/capture/capture-manifest.json"
)
DEFAULT_RECEIPT = DEFAULT_OUTPUT.with_suffix(".receipt.json")
ACCEPTED_COMMAND_REGION = (
    TEXT_PANE[0] + 20,
    TEXT_PANE[1] + 420,
    TEXT_PANE[2] - 20,
    TEXT_PANE[1] + 530,
)
DISCLOSURE_MAE_TOLERANCE = 10.0
COMMAND_BRIGHT_PIXEL_MINIMUM = 200
# Calibrated on the color-corrected Phase 4 capture: observed hold MAE band
# tops at 2.755 (uniform VP8 rate-control noise on static holds), while the
# smallest real hold-to-following change measures 5.33. 4.0 sits between.
DECODED_FREEZE_MAE_TOLERANCE = 4.0
# VP8 keyframes can perturb up to 85 of the 31,280 thresholded strip pixels.
# This permits 87 pixels while the state classifier below remains exact.
METRIC_SIGNATURE_MISMATCH_TOLERANCE = 0.0028


@dataclass(frozen=True, slots=True)
class FrameWindow:
    """A half-open output-frame interval with one storyboard purpose."""

    label: str
    kind: str
    start: int
    stop: int
    tick: int
    mission_time_s: float

    @property
    def frame_count(self) -> int:
        return self.stop - self.start


@dataclass(slots=True)
class DecodeArtifacts:
    """Small retained products from the exhaustive video decode."""

    full_decode: dict[str, Any]
    freeze: dict[str, Any]
    metric_signatures: NDArray[np.bool_]
    disclosure_crops: dict[str, NDArray[np.uint8]]
    command_counts: dict[str, int]
    browser_references: dict[int, NDArray[np.uint8]]


def frame_index_at_time(time_s: float, fps: float, frame_count: int) -> int:
    """Map a media time to its zero-based displayed frame index."""

    if fps <= 0 or frame_count <= 0:
        raise ValueError("fps and frame_count must be positive")
    if time_s < 0:
        raise ValueError("time_s must be non-negative")
    return min(int(time_s * fps), frame_count - 1)


def frame_center_time(frame_index: int, fps: float, frame_count: int) -> float:
    """Return a stable seek time at the center of an output frame."""

    if fps <= 0 or not 0 <= frame_index < frame_count:
        raise ValueError("frame index or fps is invalid")
    return (frame_index + 0.5) / fps


def crop_frame(
    frame: NDArray[np.uint8], rows: slice, columns: slice
) -> NDArray[np.uint8]:
    """Return a validated rectangular frame crop."""

    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must have shape (height, width, 3)")
    if any(value is not None for value in (rows.step, columns.step)):
        raise ValueError("crop slices must not have steps")
    row_start, row_stop = rows.start or 0, rows.stop or frame.shape[0]
    col_start, col_stop = columns.start or 0, columns.stop or frame.shape[1]
    if not (0 <= row_start < row_stop <= frame.shape[0]):
        raise ValueError("row crop is outside the frame")
    if not (0 <= col_start < col_stop <= frame.shape[1]):
        raise ValueError("column crop is outside the frame")
    return frame[row_start:row_stop, col_start:col_stop]


def crop_rectangle(
    frame: NDArray[np.uint8], rectangle: tuple[int, int, int, int]
) -> NDArray[np.uint8]:
    """Crop a renderer ``(left, top, right, bottom)`` rectangle."""

    left, top, right, bottom = rectangle
    return crop_frame(frame, slice(top, bottom), slice(left, right))


def storyboard_hold_windows(storyboard: Sequence[FrameSpec]) -> tuple[FrameWindow, ...]:
    """Calculate exact pause and ending intervals from renderer frame specs."""

    windows: list[FrameWindow] = []
    index = 0
    while index < len(storyboard):
        spec = storyboard[index]
        if spec.kind not in {"pause", "ending"}:
            index += 1
            continue
        stop = index + 1
        while stop < len(storyboard) and storyboard[stop] == spec:
            stop += 1
        chapter = spec.chapter
        mission_time = chapter.mission_time_s if chapter else spec.tick / 2.0
        label = f"chapter-{len(windows) + 1}" if spec.kind == "pause" else "ending"
        windows.append(
            FrameWindow(label, spec.kind, index, stop, spec.tick, mission_time)
        )
        index = stop
    return tuple(windows)


def metric_availability_by_frame(
    storyboard: Sequence[FrameSpec],
    metadata: Mapping[int, Mapping[str, Any]],
    timeline: Sequence[Mapping[str, Any]],
    availability_field: str = "belief_available_at",
) -> tuple[float, ...]:
    """Map every output frame to the metric availability rendered on it."""

    rows = sorted(timeline, key=lambda row: float(row["mission_time_seconds"]))
    result: list[float] = []
    for spec in storyboard:
        mission_time = float(metadata[spec.tick]["mission_time"])
        eligible = [
            row
            for row in rows
            if float(row["mission_time_seconds"]) <= mission_time
            and float(row[availability_field]) <= mission_time
        ]
        if not eligible:
            raise ValueError(f"no metric is available at mission time {mission_time}")
        result.append(float(eligible[-1][availability_field]))
    return tuple(result)


def metric_signature(frame: NDArray[np.uint8]) -> NDArray[np.bool_]:
    """Reduce the metric strip to a codec-stable light-text bitmap."""

    crop = crop_rectangle(frame, METRIC_STRIP)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    light = (gray > 100).astype(np.uint8)
    return cv2.resize(light, (920, 34), interpolation=cv2.INTER_AREA) > 0.5


def disclosure_reference_image() -> NDArray[np.uint8]:
    """Render the two required disclosure strings in their exact frame positions."""

    left, top, right, bottom = DISCLOSURE_REGION
    image = Image.new("RGB", (right - left, bottom - top), "#091321")
    draw = ImageDraw.Draw(image)
    draw.text(
        (4, 7),
        DISCLOSURE_RECONSTRUCTION,
        font=_font(18),
        fill="#a2b7cf",
    )
    draw.text(
        (4, 37),
        DISCLOSURE_PERCEPTION,
        font=_font(18),
        fill="#a2b7cf",
    )
    return cv2.cvtColor(np.asarray(image, dtype=np.uint8), cv2.COLOR_RGB2BGR)


def disclosure_text_mae(crop: NDArray[np.uint8]) -> float:
    """Compare decoded disclosure glyph pixels with the exact renderer reference."""

    reference = disclosure_reference_image()
    if crop.shape != reference.shape:
        raise ValueError(
            f"disclosure crop has shape {crop.shape}, expected {reference.shape}"
        )
    background = np.asarray((33, 19, 9), dtype=np.uint8)
    glyph_mask = np.any(reference != background, axis=2)
    return _mean_absolute_error(crop[glyph_mask], reference[glyph_mask])


def accepted_command_text_pixel_count(frame: NDArray[np.uint8]) -> int:
    """Count bright text pixels in the accepted-command block."""

    crop = crop_rectangle(frame, ACCEPTED_COMMAND_REGION)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return int((gray >= 100).sum())


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _mean_absolute_error(
    first: NDArray[np.uint8], second: NDArray[np.uint8]
) -> float:
    return float(
        np.abs(first.astype(np.int16) - second.astype(np.int16)).mean()
    )


def _following_ranges(
    storyboard: Sequence[FrameSpec], windows: Sequence[FrameWindow]
) -> dict[str, tuple[int, int, str]]:
    ranges: dict[str, tuple[int, int, str]] = {}
    for window in windows:
        start = window.stop
        if start >= len(storyboard):
            continue
        kind = storyboard[start].kind
        stop = start + 1
        while stop < len(storyboard) and storyboard[stop].kind == kind:
            stop += 1
        ranges[window.label] = (start, stop, kind)
    return ranges


def _decode_video(
    video_path: Path,
    storyboard: Sequence[FrameSpec],
    windows: Sequence[FrameWindow],
    disclosure_samples: Mapping[str, int],
    command_samples: Mapping[str, int],
    browser_sample_indices: set[int],
    expected_frames: int = EXPECTED_FRAMES,
) -> DecodeArtifacts:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise AssertionError(f"cannot open video: {video_path}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    declared_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    declared_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    declared_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))

    window_at = {
        frame_index: window
        for window in windows
        for frame_index in range(window.start, window.stop)
    }
    following = _following_ranges(storyboard, windows)
    comparison_at: dict[int, FrameWindow] = {}
    for window in windows:
        if window.label not in following:
            continue
        start, stop, _ = following[window.label]
        comparison_at.update({index: window for index in range(start, stop)})

    baselines: dict[str, NDArray[np.uint8]] = {}
    hold_max_mae = {window.label: 0.0 for window in windows}
    following_min_mae = {window.label: float("inf") for window in windows}
    signatures: list[NDArray[np.bool_]] = []
    disclosure_crops: dict[str, NDArray[np.uint8]] = {}
    command_counts: dict[str, int] = {}
    browser_references: dict[int, NDArray[np.uint8]] = {}
    frame_count = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame.shape != (HEIGHT, WIDTH, 3):
                raise AssertionError(
                    f"frame {frame_count} has shape {frame.shape}, expected "
                    f"{(HEIGHT, WIDTH, 3)}"
                )
            window = window_at.get(frame_count)
            if window is not None:
                baseline = baselines.setdefault(window.label, frame.copy())
                hold_max_mae[window.label] = max(
                    hold_max_mae[window.label],
                    _mean_absolute_error(frame, baseline),
                )
            preceding = comparison_at.get(frame_count)
            if preceding is not None:
                following_min_mae[preceding.label] = min(
                    following_min_mae[preceding.label],
                    _mean_absolute_error(frame, baselines[preceding.label]),
                )
            signatures.append(metric_signature(frame))
            for label, sample_index in disclosure_samples.items():
                if frame_count == sample_index:
                    disclosure_crops[label] = crop_rectangle(
                        frame, DISCLOSURE_REGION
                    ).copy()
            for label, sample_index in command_samples.items():
                if frame_count == sample_index:
                    command_counts[label] = accepted_command_text_pixel_count(frame)
            if frame_count in browser_sample_indices:
                browser_references[frame_count] = frame.copy()
            frame_count += 1
    finally:
        capture.release()

    assert frame_count == expected_frames, (
        f"decoded {frame_count} frames, expected {expected_frames}"
    )
    assert len(storyboard) == expected_frames, (
        f"storyboard has {len(storyboard)} frames, expected {expected_frames}"
    )
    assert abs(fps - FPS) < 0.01, f"fps is {fps}, expected {FPS}"
    assert (declared_width, declared_height) == (WIDTH, HEIGHT), (
        "declared video resolution is "
        f"{declared_width}x{declared_height}, expected {WIDTH}x{HEIGHT}"
    )
    assert declared_frames == expected_frames, (
        f"container declares {declared_frames} frames, expected {expected_frames}"
    )
    duration = frame_count / fps
    expected_duration = expected_frames / FPS
    assert abs(duration - expected_duration) < 0.01, (
        f"decoded duration is {duration:.6f}s, expected "
        f"{expected_duration:.6f}s"
    )

    freeze_windows: list[dict[str, Any]] = []
    for window in windows:
        max_mae = hold_max_mae[window.label]
        assert max_mae <= DECODED_FREEZE_MAE_TOLERANCE, (
            f"{window.label} changed during its hold: decoded MAE {max_mae:.4f}"
        )
        compare_start, compare_stop, compare_kind = following.get(
            window.label, (None, None, None)
        )
        minimum_difference = following_min_mae[window.label]
        if compare_start is not None:
            assert minimum_difference > DECODED_FREEZE_MAE_TOLERANCE, (
                f"{window.label} does not differ from following {compare_kind} frames"
            )
        freeze_windows.append(
            {
                "label": window.label,
                "kind": window.kind,
                "mission_time_s": window.mission_time_s,
                "start_frame": window.start,
                "stop_frame_exclusive": window.stop,
                "frame_count": window.frame_count,
                "decoded_max_whole_frame_mae": round(max_mae, 6),
                "following_kind": compare_kind,
                "following_start_frame": compare_start,
                "following_stop_frame_exclusive": compare_stop,
                "decoded_min_following_mae": (
                    round(minimum_difference, 6)
                    if np.isfinite(minimum_difference)
                    else None
                ),
            }
        )

    return DecodeArtifacts(
        full_decode={
            "status": "passed",
            "decoded_frames": frame_count,
            "declared_frames": declared_frames,
            "resolution": [declared_width, declared_height],
            "fps": fps,
            "duration_seconds": duration,
        },
        freeze={
            "status": "passed",
            "comparison": "whole-frame decoded MAE (VP8 tolerance)",
            "decoded_mae_tolerance": DECODED_FREEZE_MAE_TOLERANCE,
            "windows": freeze_windows,
        },
        metric_signatures=np.asarray(signatures),
        disclosure_crops=disclosure_crops,
        command_counts=command_counts,
        browser_references=browser_references,
    )


def _validate_metric_timing(
    signatures: NDArray[np.bool_], availability: Sequence[float]
) -> dict[str, Any]:
    values = np.asarray(availability, dtype=np.float64)
    transitions = np.r_[True, values[1:] != values[:-1]]
    starts = np.flatnonzero(transitions)
    state_ids = np.cumsum(transitions) - 1
    centroids = np.asarray(
        [np.mean(signatures[state_ids == state], axis=0) >= 0.5 for state in range(len(starts))]
    )
    distances = np.mean(
        signatures[:, None, :, :] != centroids[None, :, :, :], axis=(2, 3)
    )
    predicted = distances.argmin(axis=1)
    own_distances = distances[np.arange(len(signatures)), state_ids]
    wrong = np.flatnonzero(predicted != state_ids)
    wrong_details = [
        {
            "frame": int(index),
            "availability": float(values[index]),
            "expected_state": int(state_ids[index]),
            "closest_state": int(predicted[index]),
            "expected_distance": round(float(own_distances[index]), 8),
            "closest_distance": round(float(distances[index, predicted[index]]), 8),
        }
        for index in wrong[:10]
    ]
    assert not len(wrong), (
        "metric timing: strip classified outside its expected availability state "
        f"at {len(wrong)} frame(s); first failures: {wrong_details}"
    )
    worst_frame = int(np.argmax(own_distances))
    maximum_mismatch = float(own_distances[worst_frame])
    assert maximum_mismatch <= METRIC_SIGNATURE_MISMATCH_TOLERANCE, (
        f"metric timing: frame {worst_frame} for belief availability "
        f"{values[worst_frame]:g}s has signature mismatch {maximum_mismatch:.8f}, "
        f"above VP8-noise tolerance {METRIC_SIGNATURE_MISMATCH_TOLERANCE:.8f}"
    )
    if len(centroids) > 1:
        adjacent_changes = np.mean(centroids[1:] != centroids[:-1], axis=(1, 2))
        unchanged = np.flatnonzero(adjacent_changes <= 0)
        unchanged_frames = [int(starts[index + 1]) for index in unchanged]
        assert not len(unchanged), (
            "metric timing: availability crossing did not change the strip at "
            f"frame(s) {unchanged_frames}"
        )

    final_start = int(starts[-1])
    final_mismatches = np.flatnonzero(state_ids[final_start:] != state_ids[-1])
    assert not len(final_mismatches), (
        "metric timing: final availability state did not remain stable after frame "
        f"{final_start}; relative failures: {final_mismatches[:10].tolist()}"
    )
    transition_rows = [
        {"frame": int(index), "belief_available_at": float(values[index])}
        for index in starts
    ]
    return {
        "status": "passed",
        "transition_count": len(starts) - 1,
        "transitions": transition_rows,
        "final_availability_frame": final_start,
        "final_hold_frames": len(signatures) - final_start,
        "maximum_signature_mismatch": round(maximum_mismatch, 8),
        "maximum_signature_mismatch_frame": worst_frame,
        "signature_mismatch_tolerance": METRIC_SIGNATURE_MISMATCH_TOLERANCE,
    }


def _validate_disclosures(
    crops: Mapping[str, NDArray[np.uint8]], samples: Mapping[str, int]
) -> dict[str, Any]:
    assert set(crops) == set(samples), (
        "disclosure strings: decoded samples do not match requested samples; "
        f"decoded={sorted(crops)}, requested={sorted(samples)}"
    )
    errors = {label: disclosure_text_mae(crop) for label, crop in crops.items()}
    failures = {
        label: round(value, 6)
        for label, value in errors.items()
        if value > DISCLOSURE_MAE_TOLERANCE
    }
    assert not failures, (
        "disclosure strings: exact rendered text differs above glyph MAE tolerance "
        f"{DISCLOSURE_MAE_TOLERANCE}: {failures}"
    )
    return {
        "status": "passed",
        "required_strings": [DISCLOSURE_RECONSTRUCTION, DISCLOSURE_PERCEPTION],
        "glyph_mae_tolerance": DISCLOSURE_MAE_TOLERANCE,
        "samples": {
            label: {
                "frame": samples[label],
                "disclosure_glyph_mae": round(errors[label], 6),
            }
            for label in samples
        },
    }


def _validate_accepted_commands(
    counts: Mapping[str, int], samples: Mapping[str, int]
) -> dict[str, Any]:
    assert set(counts) == set(samples), (
        "accepted command: decoded samples do not match requested samples; "
        f"decoded={sorted(counts)}, requested={sorted(samples)}"
    )
    failures = {
        label: value
        for label, value in counts.items()
        if value < COMMAND_BRIGHT_PIXEL_MINIMUM
    }
    assert not failures, (
        "accepted command: bright text pixel count below minimum "
        f"{COMMAND_BRIGHT_PIXEL_MINIMUM}: {failures}"
    )
    return {
        "status": "passed",
        "bright_pixel_minimum": COMMAND_BRIGHT_PIXEL_MINIMUM,
        "samples": {
            label: {"frame": samples[label], "bright_pixels": counts[label]}
            for label in samples
        },
    }


def _validate_retention(
    capture_manifest: Path,
    receipt_path: Path,
    profile: MissionProfile = MISSION1,
) -> dict[str, Any]:
    closeout = capture_manifest.parent / "capture-closeout-receipt.json"
    for path in (capture_manifest, closeout, receipt_path):
        assert path.is_file(), f"required retained artifact is missing: {path}"
    receipt = _read_json(receipt_path)
    command = str(receipt["rerender_command"])
    python = "/home/sukaih/miniconda3/envs/onr/bin/python"
    assert command.startswith(python), (
        f"retention: rerender command does not start with {python!r}: {command!r}"
    )
    assert "--capture-manifest" in command, (
        f"retention: rerender command omits --capture-manifest: {command!r}"
    )
    summary = {}
    for key, expected in profile.retention_receipt_fields.items():
        assert receipt.get(key) == expected, (
            f"retention: video receipt {key} is {receipt.get(key)!r}, "
            f"expected {expected!r}"
        )
        summary[key] = receipt[key]
    return {
        "status": "passed",
        "capture_manifest": str(capture_manifest.resolve()),
        "capture_closeout_receipt": str(closeout.resolve()),
        "video_receipt": str(receipt_path.resolve()),
        "rerender_command": command,
        **summary,
    }


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass


def _validate_browser(
    video_path: Path,
    seek_indices: Sequence[int],
    pause_indices: set[int],
    references: Mapping[int, NDArray[np.uint8]],
    expected_frames: int = EXPECTED_FRAMES,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return {"status": "skipped_unavailable", "reason": str(exc)}

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(_QuietHandler, directory=str(video_path.parent.resolve())),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    screenshots: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="airsim-validate-") as temporary:
            os.environ.setdefault("TMPDIR", temporary)
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(
                        headless=True, args=["--disable-dev-shm-usage"]
                    )
                except (PlaywrightError, OSError) as exc:
                    return {"status": "skipped_unavailable", "reason": str(exc)}
                page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.set_content(
                    '<body style="margin:0;background:#091321;overflow:hidden">'
                    '<video id="v" style="display:block;width:1920px;height:1080px" '
                    "muted preload=\"auto\"></video></body>"
                )
                media_url = "/" + quote(video_path.name)
                page.evaluate(
                    "async (url) => {v.src=URL.createObjectURL(await "
                    "(await fetch(url)).blob())}",
                    media_url,
                )
                page.wait_for_function("v.readyState >= 2", timeout=60_000)
                metadata = page.evaluate(
                    "({duration:v.duration,width:v.videoWidth,height:v.videoHeight,"
                    "error:v.error?.code ?? null})"
                )
                assert metadata["error"] is None, (
                    f"browser playback: media error code {metadata['error']}"
                )
                assert [metadata["width"], metadata["height"]] == [WIDTH, HEIGHT], (
                    "browser playback: decoded resolution is "
                    f"{metadata['width']}x{metadata['height']}, expected {WIDTH}x{HEIGHT}"
                )
                expected_duration = expected_frames / FPS
                assert abs(metadata["duration"] - expected_duration) < 0.2, (
                    f"browser playback: duration is {metadata['duration']:.6f}s, "
                    f"expected {expected_duration:.6f}s"
                )

                for index in seek_indices:
                    time_s = frame_center_time(index, FPS, expected_frames)
                    page.evaluate(
                        "async (t) => {v.pause(); v.currentTime=t; "
                        "await new Promise((resolve,reject) => {"
                        "const timer=setTimeout(()=>reject(Error('seek timeout')),30000);"
                        "v.addEventListener('seeked',()=>{clearTimeout(timer);resolve()},"
                        "{once:true})});}",
                        time_s,
                    )
                    screenshot_path = Path(temporary) / f"seek-{index}.png"
                    page.locator("#v").screenshot(path=str(screenshot_path))
                    screenshot = cv2.imread(str(screenshot_path), cv2.IMREAD_COLOR)
                    assert screenshot is not None, (
                        f"browser seek: screenshot could not be decoded for frame {index}"
                    )
                    mae = _mean_absolute_error(screenshot, references[index])
                    if index in pause_indices:
                        assert mae <= 3.0, (
                            f"browser pause frame {index} differs from decoded frame: {mae:.4f}"
                        )
                    screenshots.append(
                        {
                            "frame": index,
                            "time_seconds": time_s,
                            "kind": "pause" if index in pause_indices else "execution",
                            "opencv_frame_mae": round(mae, 6),
                        }
                    )

                play_start = frame_center_time(
                    seek_indices[-1], FPS, expected_frames
                )
                page.evaluate("(t) => {v.currentTime=t}", play_start)
                page.wait_for_function("(t) => !v.seeking", arg=play_start)
                before = float(page.evaluate("v.currentTime"))
                page.evaluate("v.play()")
                page.wait_for_function(
                    "(t) => v.currentTime > t + 0.25 && !v.paused",
                    arg=before,
                    timeout=30_000,
                )
                after = float(page.evaluate("v.currentTime"))
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    return {
        "status": "passed",
        "metadata": metadata,
        "seeks": screenshots,
        "playback": {
            "start_time_seconds": before,
            "advanced_to_seconds": after,
        },
    }


def validate(
    video_path: Path,
    receipt_path: Path,
    capture_manifest: Path,
    metadata_path: Path,
    metrics_path: Path,
    storyboard_path: Path,
    profile: MissionProfile = MISSION1,
) -> dict[str, Any]:
    """Run the mission-profile acceptance checks and return the receipt."""

    metadata_rows = _read_json(metadata_path)
    if not isinstance(metadata_rows, list):
        raise TypeError("frame metadata must be a list")
    metadata = {int(row["tick"]): row for row in metadata_rows}
    metrics = _read_json(metrics_path)
    timeline = metrics["timeline"]
    chapters, _ = load_storyboard(storyboard_path)
    captures = load_capture_ticks(capture_manifest, profile.tick_range)
    storyboard = build_storyboard(
        metadata_rows, chapters, captures, profile.tick_range
    )
    windows = storyboard_hold_windows(storyboard)
    expected_window_counts = list(profile.expected_window_counts)
    actual_window_counts = [window.frame_count for window in windows]
    assert actual_window_counts == expected_window_counts, (
        f"storyboard holds have frame counts {actual_window_counts}, "
        f"expected {expected_window_counts}"
    )
    expected_pause_times = list(profile.expected_pause_times)
    pause_windows = [window for window in windows if window.kind == "pause"]
    actual_pause_times = [window.mission_time_s for window in pause_windows]
    assert actual_pause_times == expected_pause_times, (
        f"storyboard pauses occur at {actual_pause_times}, expected {expected_pause_times}"
    )

    first_execution = next(
        index for index, spec in enumerate(storyboard) if spec.kind == "execution"
    )
    # Avoid a VP8 keyframe when comparing exact disclosure glyph pixels.
    execution_sample = first_execution + 32
    pause_samples = [windows[0].start + windows[0].frame_count // 2,
                     windows[3].start + windows[3].frame_count // 2]
    ending_sample = windows[-1].stop - 1
    disclosure_samples = {
        "execution": execution_sample,
        "pause": pause_samples[0],
        "ending": ending_sample,
    }
    command_samples = {
        "execution": execution_sample,
        "pause": pause_samples[0],
    }
    seek_indices = [*pause_samples, execution_sample]
    artifacts = _decode_video(
        video_path,
        storyboard,
        windows,
        disclosure_samples,
        command_samples,
        set(seek_indices),
        expected_frames=profile.expected_frames,
    )
    availability = metric_availability_by_frame(
        storyboard, metadata, timeline, profile.availability_field
    )
    checks = {
        "full_decode": artifacts.full_decode,
        "browser_playback_seek": _validate_browser(
            video_path,
            seek_indices,
            set(pause_samples),
            artifacts.browser_references,
            expected_frames=profile.expected_frames,
        ),
        "editorial_pause_freeze": artifacts.freeze,
        "metric_timing": _validate_metric_timing(
            artifacts.metric_signatures, availability
        ),
        "disclosure_strings": _validate_disclosures(
            artifacts.disclosure_crops, disclosure_samples
        ),
        "accepted_command_present": _validate_accepted_commands(
            artifacts.command_counts, command_samples
        ),
        "retention": _validate_retention(
            capture_manifest, receipt_path, profile
        ),
    }
    failed = [name for name, check in checks.items() if check["status"] == "failed"]
    return {
        "video": str(video_path.resolve()),
        "status": "failed" if failed else "passed",
        "checks": checks,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the retained 1080p AirSim reconstruction video."
    )
    parser.add_argument("--video", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument(
        "--capture-manifest", type=Path, default=DEFAULT_CAPTURE_MANIFEST
    )
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--storyboard", type=Path, default=DEFAULT_STORYBOARD)
    parser.add_argument("--profile", default=MISSION1.name)
    parser.add_argument(
        "--profile-file",
        type=Path,
        help=(
            "derived joint34 expectations document written by "
            "scripts/derive_joint34_video_bundle.py"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the validator CLI and write its adjacent JSON receipt."""

    args = _parser().parse_args(argv)
    output_path = args.video.with_suffix(".validation.json")
    try:
        profile = (
            load_profile_from_file(args.profile_file)
            if args.profile_file
            else load_profile(args.profile)
        )
        result = validate(
            args.video,
            args.receipt,
            args.capture_manifest,
            args.metadata,
            args.metrics,
            args.storyboard,
            profile=profile,
        )
    except Exception as exc:  # noqa: BLE001 - serialize all CLI validation failures.
        result = {
            "video": str(args.video.resolve()),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
