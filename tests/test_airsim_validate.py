"""Pure-logic tests for the AirSim reconstruction validator."""

from __future__ import annotations

import numpy as np
import pytest

from onr.demo.airsim_reconstruction.render import (
    DISCLOSURE_REGION,
    METRIC_STRIP,
    FrameSpec,
    StoryChapter,
)
from onr.demo.airsim_reconstruction.validate import (
    ACCEPTED_COMMAND_REGION,
    DISCLOSURE_MAE_TOLERANCE,
    accepted_command_text_pixel_count,
    crop_frame,
    crop_rectangle,
    disclosure_reference_image,
    disclosure_text_mae,
    frame_center_time,
    frame_index_at_time,
    metric_availability_by_frame,
    storyboard_hold_windows,
)
from onr.demo.airsim_reconstruction.validate import (
    _validate_metric_timing as validate_metric_timing,
)


def _signature(bits: tuple[int, ...], width: int = 2000) -> np.ndarray:
    """A 1x2000 strip signature tiled from a short light/dark pattern."""

    tiled = np.tile(np.asarray(bits, dtype=bool), width // len(bits) + 1)[:width]
    return tiled[None, :]


def test_metric_timing_accepts_states_that_render_the_same_strip() -> None:
    # A run may re-enter a display state (scheduling before and after a mission
    # block); those frames render the same strip up to codec noise, so
    # classification must be against distinct strip contents.
    rng = np.random.default_rng(7)
    base = _signature((1, 0, 1, 0, 0, 1, 0, 0))
    other = _signature((0, 1, 0, 1, 1, 0, 1, 0))
    repeated = np.repeat(base[None, :, :], 5, axis=0).copy()
    # The repeated state carries a few flipped pixels per frame, as VP8 does.
    for frame in repeated:
        frame[0, rng.choice(repeated.shape[2], 3, replace=False)] ^= True
    signatures = np.concatenate(
        [
            np.repeat(base[None, :, :], 5, axis=0),
            np.repeat(other[None, :, :], 5, axis=0),
            repeated,
        ]
    )
    availability = [0.0] * 5 + [10.0] * 5 + [20.0] * 5
    report = validate_metric_timing(signatures, availability)
    assert report["status"] == "passed"
    assert report["distinct_strip_states"] == 2
    assert report["repeated_strip_states"] == 1


def test_metric_timing_still_rejects_a_frame_showing_another_strip() -> None:
    base = _signature((1, 0, 1, 0, 0, 1, 0, 0))
    other = _signature((0, 1, 0, 1, 1, 0, 1, 0))
    signatures = np.concatenate(
        [
            np.repeat(base[None, :, :], 4, axis=0),
            np.repeat(other[None, :, :], 4, axis=0),
            np.repeat(base[None, :, :], 3, axis=0),
            other[None, :, :],
        ]
    )
    availability = [0.0] * 4 + [10.0] * 4 + [20.0] * 4
    with pytest.raises(AssertionError, match="classified outside"):
        validate_metric_timing(signatures, availability)


def test_storyboard_hold_windows_use_half_open_output_frame_ranges() -> None:
    first = StoryChapter(0, 0.0, 2.0, "First", "Body", 4.0)
    second = StoryChapter(4, 2.0, 1.0, "Second", "Body", 3.0)
    storyboard = (
        *(FrameSpec(0, "pause", first) for _ in range(3)),
        FrameSpec(0, "execution"),
        FrameSpec(1, "execution"),
        *(FrameSpec(4, "pause", second) for _ in range(2)),
        *(FrameSpec(4, "ending") for _ in range(4)),
    )

    windows = storyboard_hold_windows(storyboard)

    assert [(window.kind, window.start, window.stop) for window in windows] == [
        ("pause", 0, 3),
        ("pause", 5, 7),
        ("ending", 7, 11),
    ]
    assert [window.frame_count for window in windows] == [3, 2, 4]
    assert [window.mission_time_s for window in windows] == [0.0, 2.0, 2.0]


def test_frame_time_mapping_uses_frame_centers_and_clamps_duration() -> None:
    assert frame_index_at_time(0.0, 16, 10) == 0
    assert frame_index_at_time(0.0624, 16, 10) == 0
    assert frame_index_at_time(0.0625, 16, 10) == 1
    assert frame_index_at_time(99.0, 16, 10) == 9
    assert frame_center_time(0, 16, 10) == pytest.approx(0.03125)
    assert frame_center_time(9, 16, 10) == pytest.approx(0.59375)


def test_frame_time_mapping_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        frame_index_at_time(-0.1, 16, 10)
    with pytest.raises(ValueError):
        frame_center_time(10, 16, 10)


def test_metric_availability_maps_pause_and_execution_frames() -> None:
    storyboard = (
        FrameSpec(0, "pause"),
        FrameSpec(0, "execution"),
        FrameSpec(1, "execution"),
        FrameSpec(2, "execution"),
    )
    metadata = {
        0: {"mission_time": 0.0},
        1: {"mission_time": 0.5},
        2: {"mission_time": 1.0},
    }
    timeline = [
        {"mission_time_seconds": 0.0, "belief_available_at": 0.0},
        {"mission_time_seconds": 0.5, "belief_available_at": 0.0},
        {"mission_time_seconds": 1.0, "belief_available_at": 1.0},
    ]

    assert metric_availability_by_frame(storyboard, metadata, timeline) == (
        0.0,
        0.0,
        0.0,
        1.0,
    )


def test_crop_helpers_use_renderer_rectangles_on_synthetic_frame() -> None:
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    metric_left, metric_top, metric_right, metric_bottom = METRIC_STRIP
    frame[metric_top:metric_bottom, metric_left:metric_right] = (11, 22, 33)

    metric = crop_rectangle(frame, METRIC_STRIP)

    assert metric.shape == (
        metric_bottom - metric_top,
        metric_right - metric_left,
        3,
    )
    np.testing.assert_array_equal(metric[0, 0], np.array([11, 22, 33]))


def test_disclosure_reference_requires_exact_rendered_strings() -> None:
    reference = disclosure_reference_image()
    left, top, right, bottom = DISCLOSURE_REGION
    frame = np.full((1080, 1920, 3), (33, 19, 9), dtype=np.uint8)
    frame[top:bottom, left:right] = reference

    assert disclosure_text_mae(crop_rectangle(frame, DISCLOSURE_REGION)) == 0.0

    blank = np.full_like(reference, (33, 19, 9))
    assert disclosure_text_mae(blank) > DISCLOSURE_MAE_TOLERANCE

    truncated = reference.copy()
    truncated[30:, 600:] = (33, 19, 9)
    assert disclosure_text_mae(truncated) > DISCLOSURE_MAE_TOLERANCE


def test_accepted_command_pixel_count_uses_text_pane_block() -> None:
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    left, top, right, bottom = ACCEPTED_COMMAND_REGION
    frame[top:bottom, left:right] = (180, 180, 180)

    assert accepted_command_text_pixel_count(frame) == (right - left) * (bottom - top)
    assert accepted_command_text_pixel_count(np.zeros_like(frame)) == 0


def test_crop_frame_rejects_out_of_bounds_or_non_image_arrays() -> None:
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    with pytest.raises(ValueError):
        crop_frame(frame, slice(0, 11), slice(0, 20))
    with pytest.raises(ValueError):
        crop_frame(frame[..., 0], slice(0, 10), slice(0, 20))
