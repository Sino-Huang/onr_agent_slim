"""World-model pane overlay layers for the recorded Joint34 demo video.

Visualization only: every layer draws recorded evidence that was available at
or before the tick it appears on, so the compact pane gains the tactical
context a client expects -- dock area of interest, keep-out zones and
obstacles, Mission 4 target potential locations with their recorded
uncertainty, Mission 3 selected-vessel GPS fixes with their age, and the
planned route -- without changing mission evidence, decisions, reports, or
metrics.

The pane is partition-local: a 64x64-cell window that follows the drone, so a
layer whose recorded geometry lies outside the current window is simply not
drawn.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from onr.application.object_search_belief import ObjectSearchBeliefManager
from onr.contracts.object_search import SearchBeliefSnapshot, SearchMatch

TILE_SIZE = 8
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

AOI_COLOR = "#67def0"
KOZ_COLOR = "#ff5d5d"
OBSTACLE_COLOR = "#ffa050"
TARGET_COLORS = {"red": "#ff7b7b", "blue": "#5aa9ff"}
TARGET_DEFAULT_COLOR = "#ffd078"
GPS_COLOR = "#b28dff"
ROUTE_COLOR = "#90ee90"
LEGEND_FILL = (9, 19, 33, 215)

LABEL_SIZE = 20
LEGEND_SIZE = 18
CHIP_PADDING = 6
MARKER_RADIUS = 3
OUTLINE_WIDTH = 2
HATCH_STEP = 14

COUNT_KEYS = (
    "aoi_polygons",
    "koz_polygons",
    "obstacle_polygons",
    "targets",
    "uncertainty_circles",
    "ship_fixes",
    "legend",
)

_LEGEND_ROWS = (
    (("AOI", AOI_COLOR), ("KOZ", KOZ_COLOR), ("obstacle", OBSTACLE_COLOR)),
    (
        ("M4 target ±m", "#f0f5ff"),
        ("GPS fix", GPS_COLOR),
        ("route", ROUTE_COLOR),
        ("(dashed = not found)", "#a2b7cf"),
    ),
)


@lru_cache(maxsize=48)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


@dataclass(frozen=True, slots=True)
class PaneGeometry:
    """NED window of one rendered world-pane frame and its pixel scale."""

    ned_bounds_min: tuple[float, float]
    ned_bounds_max: tuple[float, float]
    resolution_m: float
    tile_size: int = TILE_SIZE

    def __post_init__(self) -> None:
        if self.resolution_m <= 0.0 or self.tile_size <= 0:
            raise ValueError("resolution_m and tile_size must be positive")
        if (
            self.ned_bounds_max[0] <= self.ned_bounds_min[0]
            or self.ned_bounds_max[1] <= self.ned_bounds_min[1]
        ):
            raise ValueError("partition NED bounds must be ordered")

    @classmethod
    def from_partition_metadata(
        cls, metadata: Any, tile_size: int = TILE_SIZE
    ) -> "PaneGeometry":
        """Build the pane transform from a runtime partition metadata object."""

        return cls(
            ned_bounds_min=(
                float(metadata.ned_bounds_min[0]),
                float(metadata.ned_bounds_min[1]),
            ),
            ned_bounds_max=(
                float(metadata.ned_bounds_max[0]),
                float(metadata.ned_bounds_max[1]),
            ),
            resolution_m=float(metadata.multigrid_resolution),
            tile_size=int(tile_size),
        )

    @property
    def px_per_m(self) -> float:
        return self.tile_size / self.resolution_m

    @property
    def size_px(self) -> tuple[int, int]:
        width = round(
            (self.ned_bounds_max[1] - self.ned_bounds_min[1])
            / self.resolution_m
            * self.tile_size
        )
        height = round(
            (self.ned_bounds_max[0] - self.ned_bounds_min[0])
            / self.resolution_m
            * self.tile_size
        )
        return width, height

    def pixel(self, north: float, east: float) -> tuple[float, float]:
        """Continuous NED -> pixel mapping of the rendered pane image.

        The pane's cell ``(0, 0)`` covers pixel ``[0, tile)`` and NED
        ``east in [ned_bounds_min[1], +resolution_m)`` /
        ``north in (ned_bounds_max[0] - resolution_m, ned_bounds_max[0]]``,
        so the mapping is linear with ``tile_size / resolution_m`` pixels per
        metre and no half-cell offset.
        """

        return (
            (east - self.ned_bounds_min[1]) / self.resolution_m * self.tile_size,
            (self.ned_bounds_max[0] - north) / self.resolution_m * self.tile_size,
        )

    def cell(self, north: float, east: float) -> tuple[int, int]:
        """Pane grid cell ``(column, row)`` containing a NED point.

        Truncating division matches ``HeightmapMultigridConverter.ned_to_grid``
        and therefore the engine's own ``agents[0].state.pos``.
        """

        return (
            int((east - self.ned_bounds_min[1]) / self.resolution_m),
            int((self.ned_bounds_max[0] - north) / self.resolution_m),
        )

    def contains(self, north: float, east: float) -> bool:
        return (
            self.ned_bounds_min[0] <= north <= self.ned_bounds_max[0]
            and self.ned_bounds_min[1] <= east <= self.ned_bounds_max[1]
        )


@dataclass(frozen=True, slots=True)
class TargetMarker:
    """One Mission 4 objective's best recorded potential location."""

    target_id: str
    label: str
    north: float
    east: float
    uncertainty_m: float | None
    probability: float | None
    found: bool
    color: str


@dataclass(frozen=True, slots=True)
class ShipFix:
    """One Mission 3 selected vessel's latest public GPS fix."""

    ship_id: int
    north: float
    east: float
    age_s: float


@dataclass(frozen=True, slots=True)
class PaneState:
    """Recorded, time-gated evidence for exactly one pane frame."""

    areas: tuple[Sequence[Sequence[float]], ...] = ()
    keep_out_zones: tuple[Sequence[Sequence[float]], ...] = ()
    obstacles: tuple[Sequence[Sequence[float]], ...] = ()
    targets: tuple[TargetMarker, ...] = ()
    ship_fixes: tuple[ShipFix, ...] = ()


def _world_info(row: Mapping[str, Any]) -> Mapping[str, Any]:
    info = row.get("world_model_info")
    return info if isinstance(info, Mapping) else row


def section_at(
    worlds: Mapping[float, Mapping[str, Any]], mission_time_s: float
) -> Mapping[str, Any] | None:
    """Newest recorded world model published at or before ``mission_time_s``.

    A section published at mission time *t* already carries only evidence dated
    at or before *t*, so the pane frame for that tick may draw it.
    """

    times = [time for time in worlds if time <= mission_time_s]
    if not times:
        return None
    return worlds[max(times)]


def mission4_sections(
    worlds: Mapping[float, Mapping[str, Any]],
) -> list[tuple[float, Mapping[str, Any]]]:
    """Recorded Mission 4 sections in publication order."""

    sections: list[tuple[float, Mapping[str, Any]]] = []
    for time_s in sorted(worlds):
        section = _world_info(worlds[time_s]).get("mission4")
        if isinstance(section, Mapping):
            sections.append((float(time_s), section))
    return sections


def belief_snapshots(
    sections: Sequence[tuple[float, Mapping[str, Any]]],
    mission_id: str,
) -> dict[float, SearchBeliefSnapshot]:
    """Replay the agent-side public-evidence belief over recorded sections.

    Deterministic and future-evidence-safe: the manager ingests observations
    in publication order at the section's own time and raises on any
    observation whose ``acquired_at_s`` lies after that time, so a future
    observation cannot be drawn early.
    """

    if not sections:
        return {}
    manager = ObjectSearchBeliefManager(mission_id, sections[0][1]["package"])
    snapshots: dict[float, SearchBeliefSnapshot] = {}
    for time_s, section in sorted(sections, key=lambda item: item[0]):
        snapshots[float(time_s)] = manager.ingest(section, float(time_s))
    return snapshots


def _objective_order(target_id: str) -> tuple[int, str]:
    _, _, suffix = target_id.partition(":")
    return (int(suffix), target_id) if suffix.isdigit() else (1 << 30, target_id)


def best_match(
    snapshot: SearchBeliefSnapshot, target_id: str
) -> SearchMatch | None:
    """The manager's own best candidate for one objective at one tick."""

    candidates = [match for match in snapshot.matches if match.target_id == target_id]
    if not candidates:
        return None
    candidates.sort(key=lambda match: (match.found, match.probability or 0.0), reverse=True)
    return candidates[0]


def target_label(name: str, match: SearchMatch) -> str:
    probability = "n/a" if match.probability is None else f"{match.probability:.2f}"
    uncertainty = (
        ""
        if match.position_uncertainty_m is None
        else f" ±{match.position_uncertainty_m:g} m"
    )
    return f"{name} {probability}{uncertainty}"


def target_markers(
    snapshot: SearchBeliefSnapshot | None,
    objectives: Mapping[str, Mapping[str, Any]],
) -> tuple[TargetMarker, ...]:
    """Per-objective potential locations carried by one belief snapshot."""

    if snapshot is None:
        return ()
    markers: list[TargetMarker] = []
    for target_id in sorted(objectives, key=_objective_order):
        match = best_match(snapshot, target_id)
        if match is None:
            continue
        objective = objectives[target_id]
        attributes = objective.get("attributes") or {}
        name = (
            attributes.get("color")
            or attributes.get("type")
            or objective.get("description")
            or target_id
        )
        markers.append(
            TargetMarker(
                target_id=target_id,
                label=target_label(str(name), match),
                north=float(match.position[0]),
                east=float(match.position[1]),
                uncertainty_m=(
                    None
                    if match.position_uncertainty_m is None
                    else float(match.position_uncertainty_m)
                ),
                probability=match.probability,
                found=bool(match.found),
                color=TARGET_COLORS.get(str(attributes.get("color")), TARGET_DEFAULT_COLOR),
            )
        )
    return tuple(markers)


def ship_fixes(
    fixes: Iterable[Mapping[str, Any]],
    ship_ids: Iterable[int],
    mission_time_s: float,
) -> tuple[ShipFix, ...]:
    """Latest public GPS fix per selected vessel, gated on ``sampled_at_s``."""

    selected = {int(ship_id) for ship_id in ship_ids}
    latest: dict[int, tuple[float, Mapping[str, Any]]] = {}
    for fix in fixes:
        ship_id = int(fix["entity_id"])
        if ship_id not in selected:
            continue
        sampled_at_s = float(fix["sampled_at_s"])
        if sampled_at_s > mission_time_s:
            continue
        if ship_id not in latest or sampled_at_s > latest[ship_id][0]:
            latest[ship_id] = (sampled_at_s, fix)
    return tuple(
        ShipFix(
            ship_id=ship_id,
            north=float(latest[ship_id][1]["position"]["x"]),
            east=float(latest[ship_id][1]["position"]["y"]),
            age_s=mission_time_s - latest[ship_id][0],
        )
        for ship_id in sorted(latest)
    )


def pane_state(
    row: Mapping[str, Any] | None,
    snapshots: Mapping[float, SearchBeliefSnapshot],
    mission_time_s: float,
) -> PaneState:
    """Assemble the time-gated layer state for one pane frame."""

    if row is None:
        return PaneState()
    info = _world_info(row)
    mission4 = info.get("mission4") or {}
    package = mission4.get("package") or {}
    areas = tuple(
        area["polygon"]
        for area in (package.get("areas") or {}).values()
        if area.get("polygon")
    )
    published = [time for time in snapshots if time <= mission_time_s]
    snapshot = snapshots[max(published)] if published else None
    return PaneState(
        areas=areas,
        keep_out_zones=tuple(package.get("keep_out_zones") or ()),
        obstacles=tuple(package.get("obstacles") or ()),
        targets=target_markers(snapshot, mission4.get("objectives") or {}),
        ship_fixes=ship_fixes(
            info.get("public_position_fixes") or (),
            (info.get("mission3") or {}).get("selected_ship_ids") or (),
            mission_time_s,
        ),
    )


def dock_coverage_pct(row: Mapping[str, Any] | None) -> float | None:
    """Recorded dock coverage: observed cells over the area polygon's cells."""

    if row is None:
        return None
    mission4 = _world_info(row).get("mission4") or {}
    package = mission4.get("package") or {}
    dock = (package.get("areas") or {}).get("dock") or {}
    polygon = dock.get("polygon") or []
    coverage = (mission4.get("coverage") or {}).get("dock") or {}
    observed = coverage.get("observed_cells") or []
    resolution = float(coverage.get("grid_resolution_m") or 0.0)
    if len(polygon) < 3 or resolution <= 0.0:
        return None
    span = (float(polygon[2][0]) - float(polygon[0][0])) * (
        float(polygon[2][1]) - float(polygon[0][1])
    )
    cells = span / (resolution * resolution)
    if cells <= 0.0:
        return None
    return round(min(1.0, len(observed) / cells) * 100.0, 1)


def _polygon_pixels(
    geometry: PaneGeometry, polygon: Sequence[Sequence[float]]
) -> list[tuple[float, float]]:
    return [geometry.pixel(float(point[0]), float(point[1])) for point in polygon]


def _dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    color: str,
    *,
    width: int = OUTLINE_WIDTH,
    dash: float = 9.0,
    gap: float = 6.0,
) -> None:
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    if length <= 0.0:
        return
    direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    offset = 0.0
    while offset < length:
        stop = min(offset + dash, length)
        draw.line(
            (
                (start[0] + direction[0] * offset, start[1] + direction[1] * offset),
                (start[0] + direction[0] * stop, start[1] + direction[1] * stop),
            ),
            fill=color,
            width=width,
        )
        offset = stop + gap


def _hatch(
    layer: Image.Image,
    points: Sequence[tuple[float, float]],
    color: str,
    alpha: int,
    *,
    width: int = 2,
    step: int = HATCH_STEP,
) -> None:
    """Diagonal hatch clipped to one polygon on the translucent layer."""

    mask = Image.new("L", layer.size, 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    hatch = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(hatch)
    span = max(layer.size)
    for offset in range(-span, 2 * span, step):
        draw.line(
            ((offset, 0), (offset + span, span)),
            fill=(*_rgb(color), alpha),
            width=width,
        )
    layer.paste(hatch, (0, 0), mask)


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _symbol(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: float,
    y: float,
    color: str,
    *,
    size: int = LEGEND_SIZE,
) -> float:
    """Draw one legend glyph run and return its width."""

    draw.text((x, y), text, font=_font(size), fill=color)
    return draw.textlength(text, font=_font(size))


def _chip(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    text: str,
    color: str,
    size: int,
) -> None:
    """Draw one dark rounded label chip at a pre-computed box."""

    draw.rounded_rectangle(box, radius=4, fill=LEGEND_FILL)
    draw.text((box[0] + CHIP_PADDING, box[1] + 3), text, font=_font(size), fill=color)


def _overlaps(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def _label_near(
    draw: ImageDraw.ImageDraw,
    geometry: PaneGeometry,
    center: tuple[float, float],
    text: str,
    color: str,
    placed: list[tuple[float, float, float, float]],
) -> None:
    """Place one marker label beside its marker, inside and free of collisions."""

    width = draw.textlength(text, font=_font(LABEL_SIZE)) + 2 * CHIP_PADDING
    height = LABEL_SIZE + 8
    pane_width, pane_height = geometry.size_px
    anchors = (
        (center[0] - width - 6, center[1] - height / 2),
        (center[0] + 8, center[1] - height / 2),
        (center[0] - width / 2, center[1] - height - 8),
        (center[0] - width / 2, center[1] + 8),
    )
    fallback: tuple[float, float, float, float] | None = None
    for anchor_x, anchor_y in anchors:
        for step in (0, 1, -1, 2, -2, 3, -3):
            y = anchor_y + step * (height + 3)
            box = (anchor_x, y, anchor_x + width, y + height)
            if (
                box[0] < 2
                or box[2] > pane_width - 2
                or box[1] < 2
                or box[3] > pane_height - 2
            ):
                continue
            if fallback is None:
                fallback = box
            if not any(_overlaps(box, other) for other in placed):
                placed.append(box)
                _chip(draw, box, text, color, LABEL_SIZE)
                return
    if fallback is not None:
        placed.append(fallback)
        _chip(draw, fallback, text, color, LABEL_SIZE)


def _region_visible(
    geometry: PaneGeometry, polygon: Sequence[Sequence[float]]
) -> bool:
    """Bounding-box test for a recorded polygon against the pane window."""

    points = _polygon_pixels(geometry, polygon)
    width, height = geometry.size_px
    return (
        max(point[0] for point in points) >= 0.0
        and min(point[0] for point in points) <= width
        and max(point[1] for point in points) >= 0.0
        and min(point[1] for point in points) <= height
    )


def _draw_region(
    layer: Image.Image,
    draw: ImageDraw.ImageDraw,
    geometry: PaneGeometry,
    polygon: Sequence[Sequence[float]],
    label: str,
    color: str,
    *,
    fill_alpha: int,
    hatched: bool,
    placed: list[tuple[float, float, float, float]],
) -> bool:
    points = _polygon_pixels(geometry, polygon)
    if len(points) < 3 or not _region_visible(geometry, polygon):
        return False
    if hatched:
        _hatch(layer, points, color, fill_alpha)
    else:
        draw.polygon(points, fill=(*_rgb(color), fill_alpha))
    for start, end in zip(points, points[1:] + points[:1]):
        if hatched:
            _dashed_line(draw, start, end, color, width=3)
        else:
            draw.line((start, end), fill=color, width=OUTLINE_WIDTH)
    if any(geometry.contains(float(p[0]), float(p[1])) for p in polygon):
        corner = min(
            (point for point in points if 0 <= point[0] <= geometry.size_px[0]),
            key=lambda point: (point[1], point[0]),
            default=points[0],
        )
        _label_near(
            draw,
            geometry,
            (corner[0] + 40.0, corner[1] + 14.0),
            label,
            color,
            placed,
        )
    return True


def _draw_target(
    draw: ImageDraw.ImageDraw,
    geometry: PaneGeometry,
    marker: TargetMarker,
    placed: list[tuple[float, float, float, float]],
) -> tuple[bool, bool]:
    if not geometry.contains(marker.north, marker.east):
        return False, False
    center = geometry.pixel(marker.north, marker.east)
    radius = (
        0.0 if marker.uncertainty_m is None else marker.uncertainty_m * geometry.px_per_m
    )
    circle = radius >= 2.0
    if circle:
        box = (
            center[0] - radius,
            center[1] - radius,
            center[0] + radius,
            center[1] + radius,
        )
        if marker.found:
            draw.ellipse(box, outline=marker.color, width=OUTLINE_WIDTH)
        else:
            for start in range(0, 360, 20):
                draw.arc(
                    box,
                    start=start,
                    end=start + 12,
                    fill=marker.color,
                    width=OUTLINE_WIDTH,
                )
    draw.ellipse(
        (
            center[0] - MARKER_RADIUS,
            center[1] - MARKER_RADIUS,
            center[0] + MARKER_RADIUS,
            center[1] + MARKER_RADIUS,
        ),
        fill=marker.color if marker.found else None,
        outline="#f0f5ff",
        width=1,
    )
    _label_near(draw, geometry, center, marker.label, marker.color, placed)
    return True, circle


def _draw_ship_fix(
    draw: ImageDraw.ImageDraw,
    geometry: PaneGeometry,
    fix: ShipFix,
    placed: list[tuple[float, float, float, float]],
) -> bool:
    if not geometry.contains(fix.north, fix.east):
        return False
    center = geometry.pixel(fix.north, fix.east)
    draw.polygon(
        (
            (center[0], center[1] - 6.0),
            (center[0] + 6.0, center[1] + 5.0),
            (center[0] - 6.0, center[1] + 5.0),
        ),
        fill=GPS_COLOR,
        outline="#f0f5ff",
    )
    _label_near(
        draw,
        geometry,
        center,
        f"GPS {fix.ship_id} · {fix.age_s:.1f} s old",
        GPS_COLOR,
        placed,
    )
    return True


def _legend_box(draw: ImageDraw.ImageDraw) -> tuple[float, float, float, float]:
    """Rectangle the pane key occupies, so marker labels can avoid it."""

    rows = _LEGEND_ROWS
    width = 0
    for row in rows:
        width = max(
            width,
            sum(
                draw.textlength(text, font=_font(LEGEND_SIZE)) + 16
                for text, _ in row
            ),
        )
    height = len(rows) * (LEGEND_SIZE + 8) + 8
    return (8.0, 8.0, 8.0 + width + 12.0, 8.0 + height)


def _draw_legend(
    draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float]
) -> None:
    draw.rounded_rectangle(box, radius=6, fill=LEGEND_FILL)
    y = box[1] + 4
    for row in _LEGEND_ROWS:
        x = box[0] + 8
        for text, color in row:
            x += _symbol(draw, text, x, y, color) + 16
        y += LEGEND_SIZE + 8


def draw_overlays(
    frame: Image.Image, geometry: PaneGeometry, state: PaneState
) -> dict[str, int]:
    """Draw every overlay layer onto one rendered world-pane frame.

    Returns per-layer draw counts for the reconstruction receipt. The frame is
    modified in place; the legend is always drawn so the pane stays readable.
    """

    if frame.size != geometry.size_px:
        raise ValueError(
            f"pane frame is {frame.size}, expected {geometry.size_px}"
        )
    counts = dict.fromkeys(COUNT_KEYS, 0)
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    legend_box = _legend_box(draw)
    placed: list[tuple[float, float, float, float]] = [legend_box]
    _draw_legend(draw, legend_box)
    counts["legend"] = 1
    for polygon in state.areas:
        counts["aoi_polygons"] += _draw_region(
            layer,
            draw,
            geometry,
            polygon,
            "AOI dock",
            AOI_COLOR,
            fill_alpha=38,
            hatched=False,
            placed=placed,
        )
    for polygon in state.keep_out_zones:
        counts["koz_polygons"] += _draw_region(
            layer,
            draw,
            geometry,
            polygon,
            "KOZ",
            KOZ_COLOR,
            fill_alpha=70,
            hatched=True,
            placed=placed,
        )
    for polygon in state.obstacles:
        counts["obstacle_polygons"] += _draw_region(
            layer,
            draw,
            geometry,
            polygon,
            "obstacle",
            OBSTACLE_COLOR,
            fill_alpha=60,
            hatched=False,
            placed=placed,
        )
    for marker in state.targets:
        drawn, circle = _draw_target(draw, geometry, marker, placed)
        counts["targets"] += drawn
        counts["uncertainty_circles"] += circle
    for fix in state.ship_fixes:
        counts["ship_fixes"] += _draw_ship_fix(draw, geometry, fix, placed)
    composited = Image.alpha_composite(frame.convert("RGBA"), layer)
    frame.paste(composited.convert("RGB"))
    return counts
