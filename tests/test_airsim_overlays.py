"""Tests for AirSim segmentation overlay utilities."""

from __future__ import annotations

import numpy as np

from onr.demo.airsim_reconstruction.overlays import (
    BBox,
    align_to_shape,
    bboxes_for_ids,
    compute_object_overlays,
    decode_instance_ids,
    masks_for_ids,
)


def test_decode_instance_ids_uses_first_channel_as_most_significant() -> None:
    image = np.array([[[0, 0, 0], [1, 2, 3], [255, 0, 1]]], dtype=np.uint8)

    decoded = decode_instance_ids(image)

    np.testing.assert_array_equal(
        decoded,
        np.array([[0, 65_536 + 512 + 3, 16_711_681]], dtype=np.int32),
    )
    assert decoded.dtype == np.int32


def test_masks_include_absent_id_and_small_objects_are_omitted() -> None:
    id_map = np.zeros((4, 5), dtype=np.int32)
    id_map[1, 2] = 7

    masks = masks_for_ids(id_map, [7, 99])
    boxes = bboxes_for_ids(id_map, [7, 99], min_pixels=2)

    assert masks[7].dtype == np.bool_
    assert int(masks[7].sum()) == 1
    assert not masks[99].any()
    assert boxes == {}


def test_bbox_uses_inclusive_extents_and_unions_split_components() -> None:
    id_map = np.zeros((9, 10), dtype=np.int32)
    id_map[2:6, 3:6] = 12
    id_map[1, 1:3] = 42
    id_map[7, 8] = 42

    boxes = bboxes_for_ids(id_map, [12, 42], min_pixels=1)

    assert boxes[12] == BBox(x0=3, y0=2, x1=5, y1=5)
    assert boxes[12].width == 3
    assert boxes[12].height == 4
    assert boxes[42] == BBox(x0=1, y0=1, x1=8, y1=7)


def test_compute_object_overlays_reports_unknown_ids() -> None:
    id_map = np.zeros((5, 6), dtype=np.int32)
    id_map[1:3, 2:5] = 21_016
    id_map[4, 0] = 21_017
    id_map[0, 5] = 70_001

    result = compute_object_overlays(
        id_map,
        {"ship 1": 21_016, "passenger 21": 21_017},
        min_pixels=2,
    )

    assert result.unknown_ids == frozenset({70_001})
    assert set(result.overlays) == {"ship 1"}
    overlay = result.overlays["ship 1"]
    assert overlay.name == "ship 1"
    assert overlay.object_id == 21_016
    assert overlay.pixel_count == 6
    assert overlay.bbox == BBox(x0=2, y0=1, x1=4, y1=2)
    np.testing.assert_array_equal(overlay.mask, id_map == 21_016)


def test_align_to_shape_upsamples_masks_and_bbox_extents() -> None:
    mask = np.zeros((3, 4), dtype=np.bool_)
    mask[1:3, 1:3] = True

    aligned = align_to_shape(mask, (6, 8))
    boxes = bboxes_for_ids(aligned.astype(np.int32), [1], min_pixels=1)

    assert aligned.dtype == np.bool_
    assert aligned.shape == (6, 8)
    assert boxes[1] == BBox(x0=2, y0=2, x1=5, y1=5)
