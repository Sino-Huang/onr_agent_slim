"""Annotated AirSim front-camera frames for the Operator Console (ADR 0016).

Two AirSim synchronization mechanisms produce the ``camera_front_annotated``
frame source:

* ``world_model_follower`` (perception off): the follower draws ideal instance
  segmentation boxes for the ships visible in the same rendered frame.
* ``scene_clock`` (perception ``ideal``/``yolo``): the Run Worker draws the
  perception module's own output for the frame it captured. A box is drawn
  only when a perception sample belongs to that frame; otherwise the frame
  says so instead of moving stale boxes onto a newer scene.

Every annotated frame carries a disclosure strip naming the box provenance.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont

AnnotationKind = Literal["ideal_segmentation", "perception_ideal", "perception_yolo"]
Tone = Literal["identified", "unidentified"]

ANNOTATED_SIZE = (960, 540)
"""16:9 output size: legible labels without shipping full 1080p JPEGs."""
JPEG_QUALITY = 85
DISCLOSURES: dict[str, str] = {
    "ideal_segmentation": (
        "AirSim follows the world model. Boxes are ideal instance segmentation, "
        "not agent perception."
    ),
    "perception_yolo": (
        "Boxes are this frame's YOLO detections from the perception module."
    ),
    "perception_ideal": (
        "Boxes outline the ships the ideal perception module reported for this frame."
    ),
}
PERCEPTION_TIME_TOLERANCE_S = 0.25
"""The scene clock admits sensor lag in [0, 0.25] s behind its Mission time."""
_TONES: dict[str, tuple[int, int, int]] = {
    "identified": (103, 222, 240),
    "unidentified": (255, 190, 90),
}
_MAX_JSONL_BYTES = 4 * 1024 * 1024
_MAX_EVENT_BYTES = 64 * 1024
_RECENT_SAMPLES = 64


@dataclass(frozen=True, slots=True)
class OverlayBox:
    """One labelled box in source-image pixels (inclusive extents)."""

    label: str
    x0: float
    y0: float
    x1: float
    y1: float
    tone: Tone = "identified"


@dataclass(frozen=True, slots=True)
class Annotation:
    """Boxes plus the ``airsim.annotation`` status for one captured frame."""

    kind: AnnotationKind
    boxes: tuple[OverlayBox, ...]
    match: Literal["exact", "none"]
    perception_mission_time_seconds: float | None
    headline: str

    def status(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "disclosure": DISCLOSURES[self.kind],
            "match": self.match,
            "objects": len(self.boxes),
            "perception_mission_time_seconds": self.perception_mission_time_seconds,
        }


def response_pixels(response: Any) -> NDArray[np.uint8]:
    """Raw (H, W, 3) buffer of an uncompressed AirSim image response."""

    width, height = int(response.width), int(response.height)
    raw = np.frombuffer(bytes(response.image_data_uint8), dtype=np.uint8)
    if width <= 0 or height <= 0 or raw.size % (width * height):
        raise ValueError("airsim_image_buffer_invalid")
    channels = raw.size // (width * height)
    if channels not in (3, 4):
        raise ValueError("airsim_image_buffer_invalid")
    return raw.reshape(height, width, channels)[:, :, :3]


def scene_rgb(response: Any) -> NDArray[np.uint8]:
    """RGB pixels of an AirSim Scene response (AirSim returns BGR)."""

    return np.ascontiguousarray(response_pixels(response)[:, :, ::-1])


def segmentation_ids(response: Any, shape: tuple[int, int] | None = None) -> NDArray[np.int32]:
    """24-bit Harbor instance IDs at the scene image's ``(height, width)``.

    The ID convention is defined on raw byte order. A camera may capture its
    segmentation at another resolution than its Scene image; nearest-neighbour
    alignment keeps boxes in scene pixels.
    """

    from onr.demo.airsim_reconstruction.overlays import (
        align_to_shape,
        decode_instance_ids,
    )

    ids = decode_instance_ids(np.ascontiguousarray(response_pixels(response)))
    if shape is not None and ids.shape != tuple(shape):
        ids = align_to_shape(ids, shape).astype(np.int32, copy=False)
    return ids


def segmentation_boxes(
    id_map: NDArray[np.integer],
    labels_by_object_id: Mapping[int, str],
    *,
    min_pixels: int = 100,
) -> tuple[OverlayBox, ...]:
    """Ideal boxes for every mapped object with enough visible pixels."""

    from onr.demo.airsim_reconstruction.overlays import bboxes_for_ids

    boxes = bboxes_for_ids(id_map, labels_by_object_id, min_pixels=min_pixels)
    return tuple(
        OverlayBox(labels_by_object_id[object_id], box.x0, box.y0, box.x1, box.y1)
        for object_id, box in sorted(boxes.items())
    )


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def render_annotated(
    rgb: NDArray[np.uint8],
    annotation: Annotation,
    *,
    size: tuple[int, int] = ANNOTATED_SIZE,
) -> bytes:
    """Draw boxes, a headline and the disclosure strip; return a JPEG."""

    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("annotated frames need (H, W, 3) uint8 RGB pixels")
    source_height, source_width = rgb.shape[:2]
    image = Image.fromarray(rgb, mode="RGB").resize(size, Image.Resampling.BILINEAR)
    scale_x, scale_y = size[0] / source_width, size[1] / source_height
    draw = ImageDraw.Draw(image, "RGBA")
    label_font = _font(max(12, size[1] // 34))
    for box in annotation.boxes:
        colour = _TONES[box.tone]
        x0, y0 = box.x0 * scale_x, box.y0 * scale_y
        x1, y1 = (box.x1 + 1) * scale_x - 1, (box.y1 + 1) * scale_y - 1
        draw.rectangle((x0, y0, x1, y1), outline=colour, width=max(2, size[0] // 400))
        left, top, right, bottom = draw.textbbox((0, 0), box.label, font=label_font)
        width, height = right - left, bottom - top
        label_x = max(0.0, min(size[0] - width - 8.0, x0))
        label_y = max(0.0, y0 - height - 8.0)
        draw.rectangle(
            (label_x, label_y, label_x + width + 8, label_y + height + 6),
            fill=(9, 19, 33, 220),
        )
        draw.text((label_x + 4, label_y + 2 - top), box.label, font=label_font, fill=colour)
    strip_font = _font(max(11, size[1] // 40))
    for text, at_top in ((annotation.headline, True), (DISCLOSURES[annotation.kind], False)):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=strip_font)
        height = bottom - top
        y = 0 if at_top else size[1] - height - 10
        draw.rectangle((0, y, min(size[0], right - left + 16), y + height + 10), fill=(9, 19, 33, 200))
        draw.text((8, y + 5 - top), text, font=strip_font, fill=(235, 240, 245))
    output = BytesIO()
    image.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()


def ideal_segmentation_annotation(
    id_map: NDArray[np.integer],
    labels_by_object_id: Mapping[int, str],
    *,
    min_pixels: int = 100,
) -> Annotation:
    """The follower's same-frame ideal segmentation boxes."""

    boxes = segmentation_boxes(id_map, labels_by_object_id, min_pixels=min_pixels)
    noun = "object" if len(boxes) == 1 else "objects"
    return Annotation(
        kind="ideal_segmentation",
        boxes=boxes,
        match="exact",
        perception_mission_time_seconds=None,
        headline=f"ideal segmentation · {len(boxes)} {noun}",
    )


@dataclass(frozen=True, slots=True)
class _Identity:
    entity_by_actor: Mapping[str, int]
    entity_by_ship: Mapping[str, int]
    object_by_entity: Mapping[int, int]


def _read_json(path: Path, max_bytes: int = _MAX_EVENT_BYTES) -> Any:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{path.name} exceeds {max_bytes} bytes")
    return json.loads(data)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class _JsonlTail:
    """Complete appended JSON lines from one growing file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = b""

    def read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(_MAX_JSONL_BYTES)
        except OSError:
            return []
        self._offset += len(chunk)
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


class PerceptionAnnotator:
    """Match one captured scene-clock frame to the perception module's output.

    ``yolo`` reads the producer's per-frame ``perception_audit.jsonl``; a row
    belongs to the frame when its ``airsim_image_timestamp_ns`` equals the
    captured image's AirSim sensor stamp (both cameras sample the same paused
    scene), or when its Mission time is within the scene clock's admitted
    sensor lag. ``ideal`` writes no audit, so the ships it reported come from
    its ``entity.observed`` transport events at the frame's Mission time and
    are outlined from the frame's own segmentation image.
    """

    def __init__(self, run_root: Path, *, perception: str, mission_id: str) -> None:
        if perception not in ("ideal", "yolo"):
            raise ValueError("perception annotations need ideal or yolo perception")
        self.perception = perception
        self.kind: AnnotationKind = (
            "perception_yolo" if perception == "yolo" else "perception_ideal"
        )
        self._root = Path(run_root)
        self._events_dir = (
            self._root
            / "transport/topics/environment-perceptions/missions"
            / quote(mission_id, safe="")
        )
        self._identity: _Identity | None = None
        self._audit: _JsonlTail | None = None
        self._rows: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._seen_events: set[str] = set()
        self._observed: OrderedDict[float, set[int]] = OrderedDict()

    @property
    def needs_segmentation(self) -> bool:
        return self.perception == "ideal"

    def annotate(
        self,
        *,
        sensor_timestamp_ns: int,
        mission_time_seconds: float | None,
        id_map: NDArray[np.integer] | None,
    ) -> Annotation:
        identity = self._load_identity()
        if self.perception == "yolo":
            return self._yolo(identity, sensor_timestamp_ns, mission_time_seconds)
        return self._ideal(identity, mission_time_seconds, id_map)

    # -- identity -----------------------------------------------------------

    def _load_identity(self) -> _Identity | None:
        if self._identity is not None:
            return self._identity
        manifests = sorted((self._root / "perception/runs").glob("*/manifest.json"))
        if not manifests:
            return None
        try:
            manifest = _read_json(manifests[-1], 1024 * 1024)
            configuration = manifest.get("configuration", manifest)
            ship_ids = configuration["actor_ship_ids"]
            entity_ids = configuration["actor_entity_ids"]
            object_ids = configuration["actor_object_ids"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        entity_by_ship: dict[str, int] = {}
        object_by_entity: dict[int, int] = {}
        for actor, entity in entity_ids.items():
            if not isinstance(entity, int) or isinstance(entity, bool):
                continue
            if isinstance(ship_ids.get(actor), str):
                entity_by_ship[ship_ids[actor]] = entity
            if isinstance(object_ids.get(actor), int):
                object_by_entity[entity] = object_ids[actor]
        entity_by_actor = {
            actor: entity
            for actor, entity in entity_ids.items()
            if isinstance(entity, int) and not isinstance(entity, bool)
        }
        self._identity = _Identity(entity_by_actor, entity_by_ship, object_by_entity)
        if self.perception == "yolo":
            self._audit = _JsonlTail(manifests[-1].parent / "perception_audit.jsonl")
        return self._identity

    # -- yolo ---------------------------------------------------------------

    def _yolo(
        self,
        identity: _Identity | None,
        stamp: int,
        mission_time: float | None,
    ) -> Annotation:
        if self._audit is not None:
            for row in self._audit.read():
                row_stamp = row.get("airsim_image_timestamp_ns")
                if isinstance(row_stamp, int) and not isinstance(row_stamp, bool):
                    self._rows[row_stamp] = row
                    self._rows.move_to_end(row_stamp)
            while len(self._rows) > _RECENT_SAMPLES:
                self._rows.popitem(last=False)
        row = self._rows.get(stamp) or self._row_at_time(mission_time)
        latest = (
            _finite(next(reversed(self._rows.values())).get("observation_time_s"))
            if self._rows
            else None
        )
        if row is None:
            return self._none(latest)
        boxes: list[OverlayBox] = []
        for detection in row.get("perceived") or ():
            box = _yolo_box(detection, identity)
            if box is not None:
                boxes.append(box)
        identified = sum(box.tone == "identified" for box in boxes)
        return Annotation(
            kind="perception_yolo",
            boxes=tuple(boxes),
            match="exact",
            perception_mission_time_seconds=_finite(row.get("observation_time_s")),
            headline=(
                f"YOLO · {len(boxes)} detection{'s' if len(boxes) != 1 else ''}"
                f" · {identified} identified · same frame"
            ),
        )

    def _row_at_time(self, mission_time: float | None) -> dict[str, Any] | None:
        if mission_time is None:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for row in self._rows.values():
            observed = _finite(row.get("observation_time_s"))
            if observed is None:
                continue
            lag = mission_time - observed
            if 0.0 <= lag <= PERCEPTION_TIME_TOLERANCE_S and (best is None or lag < best[0]):
                best = (lag, row)
        return None if best is None else best[1]

    # -- ideal --------------------------------------------------------------

    def _ideal(
        self,
        identity: _Identity | None,
        mission_time: float | None,
        id_map: NDArray[np.integer] | None,
    ) -> Annotation:
        self._read_observations()
        latest = next(reversed(self._observed)) if self._observed else None
        sample = self._observation_at(mission_time)
        if sample is None or identity is None or id_map is None:
            return self._none(latest)
        observed_time, entities = sample
        labels = {
            identity.object_by_entity[entity]: f"ship {entity}"
            for entity in sorted(entities)
            if entity in identity.object_by_entity
        }
        boxes = segmentation_boxes(id_map, labels, min_pixels=1)
        return Annotation(
            kind="perception_ideal",
            boxes=boxes,
            match="exact",
            perception_mission_time_seconds=observed_time,
            headline=(
                f"ideal perception · {len(entities)} ship"
                f"{'s' if len(entities) != 1 else ''} reported · same frame"
            ),
        )

    def _read_observations(self) -> None:
        try:
            names = sorted(path.name for path in self._events_dir.iterdir())
        except OSError:
            return
        for name in names:
            if name in self._seen_events or not name.endswith(".json"):
                continue
            self._seen_events.add(name)
            try:
                event = _read_json(self._events_dir / name)
                payload = event["payload"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if (
                event.get("event_kind") != "entity.observed"
                or not isinstance(payload, dict)
                or payload.get("observation_kind") != "entity"
            ):
                continue
            observed = _finite(payload.get("observed_time"))
            entity = payload.get("entity_id")
            if observed is None or not isinstance(entity, int) or isinstance(entity, bool):
                continue
            key = round(observed, 6)
            self._observed.setdefault(key, set()).add(entity)
            self._observed.move_to_end(key)
        while len(self._observed) > _RECENT_SAMPLES:
            self._observed.popitem(last=False)

    def _observation_at(
        self, mission_time: float | None
    ) -> tuple[float, set[int]] | None:
        if mission_time is None:
            return None
        best: tuple[float, float] | None = None
        for observed in self._observed:
            lag = mission_time - observed
            if 0.0 <= lag <= PERCEPTION_TIME_TOLERANCE_S and (best is None or lag < best[0]):
                best = (lag, observed)
        return None if best is None else (best[1], self._observed[best[1]])

    def _none(self, latest: float | None) -> Annotation:
        if self.perception == "ideal":
            # Ideal perception publishes only the ships it saw: a frame without a
            # report cannot be told apart from a frame between samples.
            headline = (
                "ideal perception has reported no ships yet"
                if latest is None
                else f"ideal perception reported no ships for this frame (last report {latest:.1f} s)"
            )
        else:
            headline = (
                "no perception sample yet"
                if latest is None
                else f"no perception sample for this frame (last {latest:.1f} s)"
            )
        return Annotation(
            kind=self.kind,
            boxes=(),
            match="none",
            perception_mission_time_seconds=latest,
            headline=headline,
        )


def _yolo_box(detection: object, identity: _Identity | None) -> OverlayBox | None:
    if not isinstance(detection, dict):
        return None
    bbox = detection.get("bbox")
    if not isinstance(bbox, dict):
        return None
    values = [_finite(bbox.get(key)) for key in ("x", "y", "w", "h")]
    if any(value is None for value in values):
        return None
    x, y, width, height = (float(value) for value in values if value is not None)
    if width <= 0 or height <= 0:
        return None
    score = _finite(detection.get("score"))
    ship = detection.get("ship_id")
    association = detection.get("association")
    reason = association.get("reason") if isinstance(association, dict) else None
    if isinstance(ship, str) and reason == "gated":
        identified = True
        actor = detection.get("actor")
        entity = None
        if identity is not None:
            entity = identity.entity_by_actor.get(actor) if isinstance(actor, str) else None
            entity = entity if entity is not None else identity.entity_by_ship.get(ship)
        name = ship if entity is None else f"ship {entity}"
    else:
        identified = False
        name = "unidentified" if not isinstance(reason, str) else f"unidentified ({reason})"
    label = name if score is None else f"{name} · {score:.2f}"
    return OverlayBox(
        label,
        x,
        y,
        x + width - 1,
        y + height - 1,
        "identified" if identified else "unidentified",
    )


def labels_for_mapping(mapping: Mapping[str, Any]) -> dict[int, str]:
    """``object_id -> "ship N"`` from a reconstruction fixture ``mapping.json``."""

    ships = mapping.get("ships")
    if not isinstance(ships, Mapping):
        raise TypeError("fixture mapping has no ships")
    labels: dict[int, str] = {}
    for ship_id, entry in ships.items():
        object_id = entry.get("object_id") if isinstance(entry, Mapping) else None
        if isinstance(object_id, int) and not isinstance(object_id, bool):
            labels[object_id] = f"ship {ship_id}"
    return labels


def boxes_summary(boxes: Iterable[OverlayBox]) -> list[dict[str, object]]:
    """JSON-safe box list for frame metadata."""

    return [
        {
            "label": box.label,
            "bbox": [box.x0, box.y0, box.x1, box.y1],
            "tone": box.tone,
        }
        for box in boxes
    ]


__all__ = [
    "ANNOTATED_SIZE",
    "DISCLOSURES",
    "Annotation",
    "OverlayBox",
    "PerceptionAnnotator",
    "boxes_summary",
    "ideal_segmentation_annotation",
    "labels_for_mapping",
    "render_annotated",
    "response_pixels",
    "scene_rgb",
    "segmentation_boxes",
    "segmentation_ids",
]
