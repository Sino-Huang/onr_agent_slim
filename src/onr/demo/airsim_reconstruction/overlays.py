"""NumPy utilities for overlays derived from AirSim instance segmentation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class BBox:
    """Inclusive pixel extents for one visible object."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        """Return the inclusive bounding-box width in pixels."""

        return self.x1 - self.x0 + 1

    @property
    def height(self) -> int:
        """Return the inclusive bounding-box height in pixels."""

        return self.y1 - self.y0 + 1


@dataclass(frozen=True, slots=True)
class ObjectOverlay:
    """Mask and visible extent for one named engine object."""

    name: str
    object_id: int
    bbox: BBox
    pixel_count: int
    mask: NDArray[np.bool_]


@dataclass(frozen=True, slots=True)
class OverlayResult:
    """Named overlays plus decoded instance IDs absent from the mapping."""

    overlays: dict[str, ObjectOverlay]
    unknown_ids: frozenset[int]


def _validate_id_map(id_map: NDArray[np.integer]) -> None:
    if not isinstance(id_map, np.ndarray):
        raise TypeError("id_map must be a NumPy array")
    if id_map.ndim != 2:
        raise ValueError(f"id_map must have shape (H, W), got {id_map.shape}")
    if not np.issubdtype(id_map.dtype, np.integer):
        raise TypeError(f"id_map must have an integer dtype, got {id_map.dtype}")


def _validate_min_pixels(min_pixels: int) -> None:
    if isinstance(min_pixels, bool) or not isinstance(min_pixels, int):
        raise TypeError("min_pixels must be an integer")
    if min_pixels < 0:
        raise ValueError("min_pixels must be non-negative")


def decode_instance_ids(image: NDArray[np.uint8]) -> NDArray[np.int32]:
    """Decode an AirSim RGB segmentation image into 24-bit instance IDs."""

    if not isinstance(image, np.ndarray):
        raise TypeError("image must be a NumPy array")
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"image must have shape (H, W, 3), got {image.shape}")
    if image.dtype != np.uint8:
        raise TypeError(f"image must have dtype uint8, got {image.dtype}")

    channels = image.astype(np.int32)
    return (
        channels[..., 0] * 65_536
        + channels[..., 1] * 256
        + channels[..., 2]
    ).astype(np.int32, copy=False)


def masks_for_ids(
    id_map: NDArray[np.integer], ids: Iterable[int]
) -> dict[int, NDArray[np.bool_]]:
    """Return a boolean visibility mask for every requested instance ID."""

    _validate_id_map(id_map)
    return {object_id: id_map == object_id for object_id in map(int, ids)}


def bboxes_for_ids(
    id_map: NDArray[np.integer],
    ids: Iterable[int],
    min_pixels: int = 100,
) -> dict[int, BBox]:
    """Return inclusive union bounding boxes for sufficiently visible IDs."""

    _validate_id_map(id_map)
    _validate_min_pixels(min_pixels)
    boxes: dict[int, BBox] = {}
    for object_id, mask in masks_for_ids(id_map, ids).items():
        pixel_count = int(np.count_nonzero(mask))
        if pixel_count == 0 or pixel_count < min_pixels:
            continue
        ys, xs = np.nonzero(mask)
        boxes[object_id] = BBox(
            x0=int(xs.min()),
            y0=int(ys.min()),
            x1=int(xs.max()),
            y1=int(ys.max()),
        )
    return boxes


def compute_object_overlays(
    id_map: NDArray[np.integer],
    ids_by_name: Mapping[str, int],
    min_pixels: int = 100,
) -> OverlayResult:
    """Build named overlays and report decoded IDs missing from the mapping."""

    _validate_id_map(id_map)
    _validate_min_pixels(min_pixels)
    provided_ids = {int(object_id) for object_id in ids_by_name.values()}
    decoded_ids = {int(object_id) for object_id in np.unique(id_map)}
    unknown_ids = frozenset(decoded_ids - provided_ids - {0})

    overlays: dict[str, ObjectOverlay] = {}
    for name, raw_object_id in ids_by_name.items():
        object_id = int(raw_object_id)
        mask = id_map == object_id
        pixel_count = int(np.count_nonzero(mask))
        if pixel_count == 0 or pixel_count < min_pixels:
            continue
        ys, xs = np.nonzero(mask)
        overlays[name] = ObjectOverlay(
            name=name,
            object_id=object_id,
            bbox=BBox(
                x0=int(xs.min()),
                y0=int(ys.min()),
                x1=int(xs.max()),
                y1=int(ys.max()),
            ),
            pixel_count=pixel_count,
            mask=mask,
        )
    return OverlayResult(overlays=overlays, unknown_ids=unknown_ids)


def align_to_shape(
    id_map_or_mask: NDArray[np.generic], target_hw: tuple[int, int]
) -> NDArray[np.generic]:
    """Resize a two-dimensional ID map or mask with nearest-neighbor sampling."""

    if not isinstance(id_map_or_mask, np.ndarray):
        raise TypeError("id_map_or_mask must be a NumPy array")
    if id_map_or_mask.ndim != 2:
        raise ValueError(
            f"id_map_or_mask must have shape (H, W), got {id_map_or_mask.shape}"
        )
    source_height, source_width = id_map_or_mask.shape
    if source_height == 0 or source_width == 0:
        raise ValueError("id_map_or_mask dimensions must be non-zero")
    target_height, target_width = target_hw
    if target_height <= 0 or target_width <= 0:
        raise ValueError("target_hw dimensions must be positive")

    y_indices = (
        np.arange(target_height, dtype=np.int64) * source_height // target_height
    )
    x_indices = np.arange(target_width, dtype=np.int64) * source_width // target_width
    return id_map_or_mask[y_indices[:, None], x_indices[None, :]]
