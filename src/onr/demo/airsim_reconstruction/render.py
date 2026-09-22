"""Render recorded AirSim reconstruction frames into the Mission 1 demo layout."""

from __future__ import annotations

import argparse
import io
import json
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont

from .certify import INSTANCE_OBJECT_IDS, parse_static_instance_ids
from .overlays import (
    OverlayResult,
    align_to_shape,
    compute_object_overlays,
    decode_instance_ids,
)
from .profile import MISSION1, MissionProfile, load_profile, load_profile_from_file

WIDTH = 1920
HEIGHT = 1080
FPS = 16
PLAYBACK_SPEED = 4.0
DEFAULT_FFMPEG = Path(
    "/home/sukaih/.cache/ms-playwright/ffmpeg-1011/ffmpeg-linux"
)
DEFAULT_CAPTURE_DIR = Path(
    "var/demo-video/mission1-20260916-airsim/capture-smoke"
)
DEFAULT_MAPPING = Path(
    "var/demo-video/mission1-20260916-airsim/fixture/mapping.json"
)
DEFAULT_METADATA = Path(
    "var/demo-video/mission1-20260916-prior-guided/frame-metadata.json"
)
DEFAULT_METRICS = Path(
    "var/demo-video/mission1-20260916-prior-guided/mission-metrics.json"
)
DEFAULT_STORYBOARD = Path(
    "var/demo-video/mission1-20260916-prior-guided/story.json"
)
DEFAULT_OUTPUT = Path(
    "var/demo-video/mission1-20260916-airsim/output/"
    "mission1-airsim-augmented.webm"
)
DEFAULT_WORLD_FRAMES = Path(
    "var/demo-video/mission1-20260916-prior-guided/world-frames"
)
DEFAULT_RUN_DIR = Path("var/live_demo_with_wm/run.LYXubI")
DISCLOSURE_RECONSTRUCTION = "AirSim visualization reconstruction—not original live capture"
DISCLOSURE_PERCEPTION = (
    "Ideal instance-segmentation visualization; not a learned detector "
    "or original agent perception."
)

# Pane rectangles as (left, top, right, bottom) frame coordinates. The
# validator imports these to crop-check each required region.
MAIN_PANE = (40, 207, 1300, 900)
FRONT_INSET = (804, 586, 1292, 892)
WORLD_PANE = (44, 586, 318, 890)
TEXT_PANE = (1320, 207, 1880, 900)
METRIC_STRIP = (40, 915, 1880, 982)
DISCLOSURE_REGION = (40, 990, 1880, 1050)

_MAIN_IMAGE_SIZE = (1240, 672)
_MAIN_IMAGE_ORIGIN = (50, 216)
_FRONT_IMAGE_SIZE = (480, 270)
_FRONT_IMAGE_ORIGIN = (808, 618)
_WORLD_IMAGE_SIZE = (264, 264)
_WORLD_IMAGE_ORIGIN = (48, 620)


@dataclass(frozen=True, slots=True)
class CapturedTick:
    """Paths and mission time for one complete captured AirSim tick."""

    tick: int
    mission_time_s: float
    front_rgb: Path
    front_seg: Path
    third_rgb: Path


@dataclass(frozen=True, slots=True)
class StoryChapter:
    """One recorded editorial pause from the approved run storyboard."""

    tick: int
    mission_time_s: float
    seconds: float
    title: str
    body: str
    actual_wait_seconds: float


@dataclass(frozen=True, slots=True)
class FrameSpec:
    """Deterministic instruction for one output video frame."""

    tick: int
    kind: str
    chapter: StoryChapter | None = None


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Summary of one completed render."""

    output_path: Path
    frame_count: int
    storyboard_total: int
    selected_ticks: tuple[int, ...]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=24)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size
    )


def _paragraph(
    draw: ImageDraw.ImageDraw,
    value: str,
    x: int,
    y: int,
    width: int,
    *,
    size: int = 25,
    color: str = "#f0f5ff",
    leading: float = 1.35,
) -> int:
    for paragraph in value.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if line and draw.textlength(candidate, font=_font(size)) > width:
                draw.text((x, y), line, font=_font(size), fill=color)
                y += round(size * leading)
                line = word
            else:
                line = candidate
        draw.text((x, y), line, font=_font(size), fill=color)
        y += round(size * leading)
    return y


def parse_tick_range(value: str) -> tuple[int, int]:
    """Parse an inclusive ``START-END`` tick range."""

    try:
        start_text, end_text = value.split("-", 1)
        start, end = int(start_text), int(end_text)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("ticks must use START-END") from exc
    if start < 0 or end < start:
        raise argparse.ArgumentTypeError("ticks must be non-negative and ordered")
    return start, end


def load_capture_ticks(
    manifest_path: str | Path, tick_range: tuple[int, int]
) -> dict[int, CapturedTick]:
    """Load complete captured ticks in the requested inclusive range."""

    path = Path(manifest_path)
    manifest = _read_json(path)
    records = manifest.get("ticks", {})
    if not isinstance(records, Mapping):
        raise TypeError("capture manifest ticks must be an object")
    start, end = tick_range
    captures: dict[int, CapturedTick] = {}
    for tick in range(start, end + 1):
        record = records.get(str(tick))
        if not isinstance(record, Mapping):
            continue
        images = record.get("images")
        if not isinstance(images, list):
            continue
        by_kind = {
            str(image["kind"]): path.parent / str(image["path"])
            for image in images
            if isinstance(image, Mapping) and "kind" in image and "path" in image
        }
        required = ("front-rgb", "front-seg", "third-rgb")
        if any(kind not in by_kind or not by_kind[kind].is_file() for kind in required):
            continue
        captures[tick] = CapturedTick(
            tick=tick,
            mission_time_s=float(record["mission_time_s"]),
            front_rgb=by_kind["front-rgb"],
            front_seg=by_kind["front-seg"],
            third_rgb=by_kind["third-rgb"],
        )
    return captures


def load_dynamic_object_ids(mapping_path: str | Path) -> dict[str, int]:
    """Load display labels mapped to engine segmentation instance IDs."""

    mapping = _read_json(Path(mapping_path))
    dynamic = mapping.get("dynamic_object_ids")
    if isinstance(dynamic, Mapping):
        result: dict[str, int] = {}
        for name, value in dynamic.items():
            if isinstance(value, Mapping):
                value = value.get("instance_id", value.get("object_id"))
            result[str(name)] = int(value)
        return result

    result = {}
    for section_name, singular in (("ships", "ship"), ("passengers", "passenger")):
        section = mapping.get(section_name, {})
        if not isinstance(section, Mapping):
            raise TypeError(f"mapping {section_name} must be an object")
        for identifier, row in section.items():
            if not isinstance(row, Mapping):
                raise TypeError(
                    f"mapping {section_name}/{identifier} must be an object"
                )
            instance_id = row.get("dynamic_object_id", row.get("object_id"))
            result[f"{singular} {identifier}"] = int(instance_id)
    statics = mapping.get("static_objects", {})
    if isinstance(statics, Mapping):
        for name, row in statics.items():
            result[str(name)] = int(row["object_id"])
    return result


def load_storyboard(path: str | Path) -> tuple[list[StoryChapter], str]:
    """Load the approved run's editorial pause chapters and ending copy."""

    story = _read_json(Path(path))
    rows = story.get("reasoning_chapters", [])
    if not isinstance(rows, list):
        raise TypeError("storyboard reasoning_chapters must be a list")
    chapters = [
        StoryChapter(
            tick=round(float(row["time"]) * 2),
            mission_time_s=float(row["time"]),
            seconds=float(row["seconds"]),
            title=str(row["title"]),
            body=str(row["body"]),
            actual_wait_seconds=float(row["actual_wait_seconds"]),
        )
        for row in rows
    ]
    return chapters, str(story["ending"])


def load_metrics(
    path: str | Path, profile: MissionProfile = MISSION1
) -> dict[str, Any]:
    """Load metrics and enforce the approved run's final outcome gate."""

    metrics = _read_json(Path(path))
    profile.validate_metrics(metrics)
    return metrics


def _format_intent(intent: Mapping[str, Any]) -> str:
    """Format one recorded command intent as a public summary line."""

    action = str(intent.get("action", "command"))
    parameters = intent.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {}
    if action == "pursue":
        return f"Pursue ship {parameters.get('entity_id', '?')}"
    if action == "navigate":
        target = parameters.get("target")
        if isinstance(target, Mapping):
            return (
                f"Navigate to ({float(target.get('x', 0.0)):g}, "
                f"{float(target.get('y', 0.0)):g})"
            )
    return action.replace("_", " ").capitalize()


def load_accepted_commands(run_dir: str | Path) -> list[tuple[float, str]]:
    """Join recorded commands with accepted feedback, sorted by mission time."""

    run = Path(run_dir)
    commands_dir = run / "physical-state" / "commands"
    feedback_dir = run / "physical-state" / "feedback"
    if not commands_dir.is_dir() or not feedback_dir.is_dir():
        return []
    commands: dict[str, Mapping[str, Any]] = {}
    for path in sorted(commands_dir.glob("*.json")):
        data = _read_json(path)
        if isinstance(data, Mapping) and "command_id" in data:
            intent = data.get("intent")
            if isinstance(intent, Mapping):
                commands[str(data["command_id"])] = intent
    accepted: list[tuple[float, str]] = []
    for path in sorted(feedback_dir.glob("*.json")):
        data = _read_json(path)
        if not isinstance(data, Mapping) or data.get("phase") != "accepted":
            continue
        intent = commands.get(str(data.get("command_id")))
        if intent is not None:
            accepted.append(
                (float(data["mission_time_s"]), _format_intent(intent))
            )
    accepted.sort(key=lambda row: row[0])
    return accepted


def accepted_command_at(
    accepted: Sequence[tuple[float, str]], mission_time_s: float
) -> str:
    """Return the latest accepted command summary at or before mission time."""

    current = "Awaiting first command"
    for at, text in accepted:
        if at > mission_time_s:
            break
        current = text
    return current


def build_storyboard(
    metadata: Sequence[Mapping[str, Any]],
    chapters: Sequence[StoryChapter],
    available_ticks: Iterable[int],
    tick_range: tuple[int, int],
    *,
    fps: int = FPS,
    playback_speed: float = PLAYBACK_SPEED,
    ending_seconds: float = 10.0,
) -> tuple[FrameSpec, ...]:
    """Expand captured ticks and frozen chapters into output frame instructions."""

    if fps <= 0 or playback_speed <= 0:
        raise ValueError("fps and playback_speed must be positive")
    if ending_seconds < 0:
        raise ValueError("ending_seconds must be non-negative")
    rows = {int(row["tick"]): row for row in metadata}
    if not rows:
        raise ValueError("frame metadata must not be empty")
    maximum_tick = max(rows)
    start, end = tick_range
    selected = sorted(
        tick
        for tick in {int(value) for value in available_ticks}
        if start <= tick <= end and tick in rows
    )
    chapters_by_tick: dict[int, list[StoryChapter]] = {}
    for chapter in chapters:
        chapters_by_tick.setdefault(chapter.tick, []).append(chapter)

    frames: list[FrameSpec] = []
    for tick in selected:
        for chapter in chapters_by_tick.get(tick, []):
            pause_frames = round(chapter.seconds * fps)
            if pause_frames <= 0:
                raise ValueError(f"chapter at tick {tick} has no output frames")
            frames.extend(
                FrameSpec(tick=tick, kind="pause", chapter=chapter)
                for _ in range(pause_frames)
            )
        if tick < maximum_tick:
            next_row = rows.get(tick + 1)
            if next_row is None:
                raise ValueError(f"frame metadata is missing tick {tick + 1}")
            duration = float(next_row["mission_time"]) - float(
                rows[tick]["mission_time"]
            )
            repeats = round(duration * fps / playback_speed)
            if repeats <= 0:
                raise ValueError(f"tick {tick} has no execution output frames")
            frames.extend(
                FrameSpec(tick=tick, kind="execution") for _ in range(repeats)
            )
        elif tick == maximum_tick and ending_seconds:
            frames.extend(
                FrameSpec(tick=tick, kind="ending")
                for _ in range(round(ending_seconds * fps))
            )
    return tuple(frames)


def _overlay_color(name: str) -> tuple[int, int, int]:
    return (255, 208, 120) if name.startswith("passenger ") else (103, 222, 240)


def annotate_front_frame(
    front_rgb: NDArray[np.uint8],
    front_seg: NDArray[np.uint8],
    ids_by_name: Mapping[str, int],
    *,
    min_pixels: int = 100,
) -> tuple[Image.Image, OverlayResult]:
    """Tint visible instances and draw inclusive boxes and grounded labels."""

    if front_rgb.ndim != 3 or front_rgb.shape[2] != 3:
        raise ValueError("front_rgb must have shape (H, W, 3)")
    if front_rgb.dtype != np.uint8:
        raise TypeError("front_rgb must have dtype uint8")
    id_map = decode_instance_ids(front_seg)
    if id_map.shape != front_rgb.shape[:2]:
        id_map = align_to_shape(id_map, front_rgb.shape[:2]).astype(
            np.int32, copy=False
        )
    result = compute_object_overlays(id_map, ids_by_name, min_pixels=min_pixels)
    annotated = front_rgb.copy()
    for overlay in result.overlays.values():
        color = np.asarray(_overlay_color(overlay.name), dtype=np.float32)
        pixels = annotated[overlay.mask].astype(np.float32)
        annotated[overlay.mask] = np.clip(pixels * 0.55 + color * 0.45, 0, 255).astype(
            np.uint8
        )

    image = Image.fromarray(annotated, mode="RGB")
    draw = ImageDraw.Draw(image)
    for overlay in result.overlays.values():
        color = _overlay_color(overlay.name)
        box = overlay.bbox
        text_box = draw.textbbox((0, 0), overlay.name, font=_font(18))
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        label_x = max(0, min(image.width - text_width - 8, box.x0))
        label_y = max(0, box.y0 - text_height - 8)
        draw.rounded_rectangle(
            (label_x, label_y, label_x + text_width + 8, label_y + text_height + 6),
            radius=3,
            fill="#091321",
        )
        draw.text(
            (label_x + 4, label_y + 1), overlay.name, font=_font(18), fill=color
        )
        draw.rectangle((box.x0, box.y0, box.x1, box.y1), outline=color, width=3)
    return image, result


def _annotate_inset(
    front_rgb: NDArray[np.uint8],
    front_seg: NDArray[np.uint8],
    ids_by_name: Mapping[str, int],
    *,
    inset_size: tuple[int, int] = _FRONT_IMAGE_SIZE,
    min_pixels: int = 100,
) -> tuple[Image.Image, OverlayResult]:
    """Annotate the front view at inset scale so its labels stay legible."""

    height, width = front_rgb.shape[:2]
    scale = min(1.0, inset_size[0] / width)
    if scale < 1.0:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        rgb_image = Image.fromarray(front_rgb, mode="RGB").resize(
            size, Image.Resampling.LANCZOS
        )
        seg_image = Image.fromarray(front_seg, mode="RGB").resize(
            size, Image.Resampling.NEAREST
        )
        rgb = np.asarray(rgb_image, dtype=np.uint8)
        seg = np.asarray(seg_image, dtype=np.uint8)
        pixels = max(1, round(min_pixels * scale * scale))
    else:
        rgb, seg, pixels = front_rgb, front_seg, min_pixels
    return annotate_front_frame(rgb, seg, ids_by_name, min_pixels=pixels)


def _label_chip(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    *,
    size: int = 16,
    color: str = "#67def0",
) -> None:
    """Draw a small dark chip with a pane label over imagery."""

    width = round(draw.textlength(text, font=_font(size)))
    draw.rounded_rectangle(
        (x, y, x + width + 16, y + size + 10), radius=5, fill="#091321"
    )
    draw.text((x + 8, y + 3), text, font=_font(size), fill=color)


def _fit_image(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_width, target_height = size
    scale = min(target_width / image.width, target_height / image.height)
    resized = image.resize(
        (round(image.width * scale), round(image.height * scale)),
        Image.Resampling.LANCZOS,
    )
    result = Image.new("RGB", size, "#091321")
    result.paste(
        resized,
        ((target_width - resized.width) // 2, (target_height - resized.height) // 2),
    )
    return result


def _coverage_label(value: Any) -> str:
    """Format the recorded dock-coverage percentage for the right pane."""

    return "n/a" if value is None else f"{float(value):.1f}%"


def _metric_at_time(
    metrics: Mapping[str, Any],
    mission_time_s: float,
    availability_field: str = "belief_available_at",
) -> Mapping[str, Any]:
    rows = sorted(
        metrics["timeline"], key=lambda row: float(row["mission_time_seconds"])
    )
    available = [
        row
        for row in rows
        if float(row["mission_time_seconds"]) <= mission_time_s
        and float(row[availability_field]) <= mission_time_s
    ]
    if not available:
        raise ValueError(f"no metric is available at mission time {mission_time_s}")
    return available[-1]


def compose_frame(
    front_rgb: NDArray[np.uint8],
    front_seg: NDArray[np.uint8],
    third_rgb: NDArray[np.uint8],
    metadata_row: Mapping[str, Any],
    metric_row: Mapping[str, Any],
    ids_by_name: Mapping[str, int],
    frame_spec: FrameSpec,
    ending_text: str,
    world_frame: Image.Image | None = None,
    accepted_command: str = "Awaiting first command",
    min_pixels: int = 100,
    known_static_ids: AbstractSet[int] = frozenset(),
    profile: MissionProfile = MISSION1,
) -> Image.Image:
    """Compose one 1920×1080 frame using the approved demo's panel conventions.

    The AirSim third-person chase camera is the dominant main pane; the
    annotated front camera and the world-model overview are secondary
    insets layered over it, and the right pane carries the public decision
    summary plus the latest accepted command.
    """

    background = "#091321"
    panel = "#132338"
    white = "#f0f5ff"
    muted = "#a2b7cf"
    cyan = "#67def0"
    green = "#75e4ac"
    amber = "#ffd078"
    paused = frame_spec.kind == "pause"
    ending = frame_spec.kind == "ending"
    accent = amber if paused else green
    mission_time = float(metadata_row["mission_time"])

    annotated, overlay_result = _annotate_inset(
        front_rgb, front_seg, ids_by_name, min_pixels=min_pixels
    )
    chase = Image.fromarray(third_rgb.astype(np.uint8, copy=False), mode="RGB")
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    draw.text(
        (40, 22), "ONR  /  MULTI-AGENT MISSION SYSTEM", font=_font(23), fill=cyan
    )
    draw.text(
        (1325, 26), "AIRSIM-AUGMENTED RECORDED REPLAY", font=_font(20), fill=muted
    )
    title = (
        frame_spec.chapter.title
        if paused and frame_spec.chapter is not None
        else profile.ending_title
        if ending
        else profile.execution_title
    )
    draw.text((40, 66), title, font=_font(34), fill=white)
    draw.rounded_rectangle(
        (40, 126, 1880, 187),
        radius=12,
        fill="#33291b" if paused else "#173129",
    )
    banner = (
        "WORLD PAUSED FOR RECORDED REASONING  /  ALL PANES FROZEN"
        if paused
        else profile.ending_banner
        if ending
        else profile.execution_banner
    )
    draw.text((60, 139), banner, font=_font(27), fill=accent)
    draw.text(
        (1540, 145), f"MISSION {mission_time:05.1f} s", font=_font(21), fill=accent
    )

    # Main pane: dominant AirSim third-person chase camera.
    draw.rounded_rectangle(MAIN_PANE, radius=18, fill=panel)
    main_image = _fit_image(chase, _MAIN_IMAGE_SIZE)
    image.paste(main_image, _MAIN_IMAGE_ORIGIN)
    main_x, main_y = _MAIN_IMAGE_ORIGIN
    draw.rectangle(
        (
            main_x - 2,
            main_y - 2,
            main_x + _MAIN_IMAGE_SIZE[0] + 1,
            main_y + _MAIN_IMAGE_SIZE[1] + 1,
        ),
        outline="#425971",
        width=2,
    )
    _label_chip(
        draw,
        main_x + 8,
        main_y + 8,
        "THIRD-PERSON CHASE CAMERA  /  AIRSIM RECONSTRUCTION",
        size=18,
        color=muted,
    )

    # Secondary inset: annotated front camera with ideal instance overlays.
    front_image = _fit_image(annotated, _FRONT_IMAGE_SIZE)
    front_x, front_y = _FRONT_IMAGE_ORIGIN
    image.paste(front_image, (front_x, front_y))
    draw.rectangle(
        (
            front_x - 2,
            front_y - 2,
            front_x + _FRONT_IMAGE_SIZE[0] + 1,
            front_y + _FRONT_IMAGE_SIZE[1] + 1,
        ),
        outline="#425971",
        width=2,
    )
    _label_chip(
        draw,
        front_x - 2,
        front_y - 32,
        "FRONT CAMERA  /  IDEAL INSTANCE OVERLAYS",
    )

    # Secondary inset: compact world-model overview for this tick.
    world_x, world_y = _WORLD_IMAGE_ORIGIN
    if world_frame is not None:
        world_image = _fit_image(world_frame, _WORLD_IMAGE_SIZE)
    else:
        world_image = Image.new("RGB", _WORLD_IMAGE_SIZE, "#0d1a2b")
        placeholder = ImageDraw.Draw(world_image)
        placeholder.text(
            (28, _WORLD_IMAGE_SIZE[1] // 2 - 10),
            "WORLD FRAME UNAVAILABLE",
            font=_font(16),
            fill=muted,
        )
    image.paste(world_image, (world_x, world_y))
    draw.rectangle(
        (
            world_x - 2,
            world_y - 2,
            world_x + _WORLD_IMAGE_SIZE[0] + 1,
            world_y + _WORLD_IMAGE_SIZE[1] + 1,
        ),
        outline="#425971",
        width=2,
    )
    _label_chip(draw, world_x - 2, world_y - 32, "WORLD MODEL  /  MAP OVERVIEW")

    # Right pane: public decision summary plus the accepted command.
    draw.rounded_rectangle(TEXT_PANE, radius=18, fill=panel)
    text_x = TEXT_PANE[0] + 28
    text_width = TEXT_PANE[2] - text_x - 28
    draw.text(
        (text_x, 226), "Perception  /  Hyper  /  Maneuver", font=_font(24), fill=cyan
    )
    row_fields = profile.prepare_row(metric_row)
    status_fields = {
        **row_fields,
        "visible_ships": ", ".join(
            map(str, metadata_row.get("visible_ship_ids", []))
        )
        or "none",
        "checks": int(metadata_row.get("checks", 0)),
        "dock_coverage": _coverage_label(metadata_row.get("m4_coverage_pct")),
    }
    if paused and frame_spec.chapter is not None:
        chapter = frame_spec.chapter
        y = _paragraph(
            draw,
            profile.pause_subtitle(chapter.seconds, chapter.actual_wait_seconds),
            text_x,
            280,
            text_width,
            size=19,
            color=muted,
        )
        _paragraph(draw, chapter.body, text_x, y + 16, text_width, size=24)
    elif ending:
        _paragraph(draw, ending_text, text_x, 280, text_width, size=24)
    else:
        _paragraph(
            draw,
            profile.execution_body,
            text_x,
            280,
            text_width,
            size=24,
        )
    draw.line((text_x, 620, TEXT_PANE[2] - 28, 620), fill="#40536b", width=2)
    prefix = (
        "Last accepted: " if paused or ending else "Accepted command: "
    )
    draw.text((text_x, 640), "ACCEPTED COMMAND", font=_font(16), fill=muted)
    _paragraph(
        draw, prefix + accepted_command, text_x, 668, text_width, size=21, color=cyan
    )
    for line in profile.status_lines:
        draw.text(
            (text_x, line.y),
            line.template.format(**status_fields),
            font=_font(line.size),
            fill=muted if line.fill == "muted" else line.fill,
        )
    unknown_ids = sorted(set(overlay_result.unknown_ids) - set(known_static_ids))
    unknown = ", ".join(map(str, unknown_ids)) or "none"
    draw.text(
        (text_x, profile.unmapped_ids_y),
        f"Unmapped decoded IDs: {unknown}",
        font=_font(17),
        fill=muted,
    )

    draw.rounded_rectangle(METRIC_STRIP, radius=12, fill=panel)
    for line in profile.strip_lines:
        draw.text(
            line.xy,
            line.template.format(**row_fields),
            font=_font(line.size),
            fill=line.fill,
        )
    draw.text((44, 997), DISCLOSURE_RECONSTRUCTION, font=_font(18), fill=muted)
    draw.text((44, 1027), DISCLOSURE_PERCEPTION, font=_font(18), fill=muted)
    draw.text(
        (1435, 1027),
        "Evaluator metrics are not planner input",
        font=_font(17),
        fill=muted,
    )
    return image


def encode_video(
    frames: Iterable[Image.Image],
    output_path: str | Path,
    *,
    ffmpeg_path: str | Path = DEFAULT_FFMPEG,
    fps: int = FPS,
    bitrate_kbps: int = 3000,
    cpu_used: int = 4,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> int:
    """Stream JPEG frames to ffmpeg as image2pipe/mjpeg and return the count.

    The Playwright-bundled ffmpeg has no rawvideo demuxer or libvpx-vp9
    encoder; the proven invocation (render_verified_run.py) is mjpeg over
    image2pipe into libvpx (VP8 WebM).
    """

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        str(ffmpeg_path),
        "-y",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libvpx",
        "-b:v",
        f"{bitrate_kbps}k",
        "-deadline",
        "good",
        "-cpu-used",
        str(cpu_used),
        str(destination),
    ]
    process = popen_factory(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None:
        raise RuntimeError("ffmpeg stdin pipe was not created")
    frame_count = 0
    buffer = io.BytesIO()
    for frame in frames:
        if frame.size != (WIDTH, HEIGHT):
            raise ValueError(f"frame must be {WIDTH}x{HEIGHT}, got {frame.size}")
        buffer.seek(0)
        buffer.truncate()
        frame.convert("RGB").save(buffer, format="JPEG", quality=95)
        process.stdin.write(buffer.getvalue())
        frame_count += 1
    process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"ffmpeg exited with status {return_code}")
    return frame_count


def _image_array(path: Path) -> NDArray[np.uint8]:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def render_video(
    capture_manifest: str | Path,
    mapping_path: str | Path,
    metadata_path: str | Path,
    metrics_path: str | Path,
    storyboard_path: str | Path,
    output_path: str | Path,
    tick_range: tuple[int, int],
    *,
    world_frames: str | Path = DEFAULT_WORLD_FRAMES,
    run_dir: str | Path = DEFAULT_RUN_DIR,
    ffmpeg_path: str | Path = DEFAULT_FFMPEG,
    bitrate_kbps: int = 3000,
    cpu_used: int = 4,
    min_pixels: int = 100,
    profile: MissionProfile = MISSION1,
) -> RenderResult:
    """Render the selected available AirSim capture ticks into one WebM video."""

    manifest_path = Path(capture_manifest)
    captures = load_capture_ticks(manifest_path, tick_range)
    metadata_rows = _read_json(Path(metadata_path))
    if not isinstance(metadata_rows, list):
        raise TypeError("frame metadata must be a list")
    metrics = load_metrics(metrics_path, profile)
    metadata = {int(row["tick"]): row for row in metadata_rows}
    chapters, ending_text = load_storyboard(storyboard_path)
    ids_by_name = load_dynamic_object_ids(mapping_path)
    accepted = load_accepted_commands(run_dir)
    known_static_ids = parse_static_instance_ids(INSTANCE_OBJECT_IDS)
    storyboard = build_storyboard(
        metadata_rows, chapters, captures, tick_range
    )
    if not storyboard:
        raise ValueError("no complete captured frames exist in the requested range")
    world_dir = Path(world_frames)

    @lru_cache(maxsize=8)
    def world_image(tick: int) -> Image.Image | None:
        path = world_dir / f"{tick:04d}.png"
        if not path.is_file():
            return None
        with Image.open(path) as source:
            return source.convert("RGB").resize(
                _WORLD_IMAGE_SIZE, Image.Resampling.LANCZOS
            )

    @lru_cache(maxsize=16)
    def render_spec(spec: FrameSpec) -> Image.Image:
        capture = captures[spec.tick]
        mission_time = float(metadata[spec.tick]["mission_time"])
        return compose_frame(
            _image_array(capture.front_rgb),
            _image_array(capture.front_seg),
            _image_array(capture.third_rgb),
            metadata[spec.tick],
            _metric_at_time(
                metrics, mission_time, profile.availability_field
            ),
            ids_by_name,
            spec,
            ending_text=ending_text,
            world_frame=world_image(spec.tick),
            accepted_command=accepted_command_at(accepted, mission_time),
            min_pixels=min_pixels,
            known_static_ids=known_static_ids,
            profile=profile,
        )

    count = encode_video(
        (render_spec(spec) for spec in storyboard),
        output_path,
        ffmpeg_path=ffmpeg_path,
        bitrate_kbps=bitrate_kbps,
        cpu_used=cpu_used,
    )
    if count != len(storyboard):
        raise RuntimeError(
            f"encoded frame count {count} != storyboard total {len(storyboard)}"
        )
    selected_ticks = tuple(sorted(captures))
    result = RenderResult(Path(output_path), count, len(storyboard), selected_ticks)
    receipt = {
        "video": str(result.output_path.resolve()),
        "frame_count": result.frame_count,
        "storyboard_total": result.storyboard_total,
        "fps": FPS,
        "resolution": [WIDTH, HEIGHT],
        "selected_ticks": list(result.selected_ticks),
        "tick_range": list(tick_range),
        "capture_manifest": str(manifest_path.resolve()),
        "mapping": str(Path(mapping_path).resolve()),
        "frame_metadata": str(Path(metadata_path).resolve()),
        "metrics": str(Path(metrics_path).resolve()),
        **profile.receipt_fields(metrics),
        "world_frames": str(Path(world_frames).resolve()),
        "run": str(Path(run_dir).resolve()),
        "rerender_command": shlex.join(
            [
                sys.executable,
                "-m",
                "onr.demo.airsim_reconstruction.render",
                *sys.argv[1:],
            ]
        ),
    }
    receipt_path = result.output_path.with_suffix(".receipt.json")
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the AirSim-augmented recorded Mission 1 demo."
    )
    parser.add_argument(
        "--capture-manifest",
        type=Path,
        default=DEFAULT_CAPTURE_DIR / "capture-manifest.json",
    )
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--storyboard", type=Path, default=DEFAULT_STORYBOARD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--world-frames", type=Path, default=DEFAULT_WORLD_FRAMES)
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    parser.add_argument("--bitrate-kbps", type=int, default=3000)
    parser.add_argument("--cpu-used", type=int, default=4)
    parser.add_argument("--ticks", type=parse_tick_range, default=(0, 599))
    parser.add_argument("--min-pixels", type=int, default=100)
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
    """Run the AirSim reconstruction renderer command-line interface."""

    args = _parser().parse_args(argv)
    profile = (
        load_profile_from_file(args.profile_file)
        if args.profile_file
        else load_profile(args.profile)
    )
    result = render_video(
        args.capture_manifest,
        args.mapping,
        args.metadata,
        args.metrics,
        args.storyboard,
        args.output,
        args.ticks,
        world_frames=args.world_frames,
        run_dir=args.run,
        ffmpeg_path=args.ffmpeg,
        bitrate_kbps=args.bitrate_kbps,
        min_pixels=args.min_pixels,
        cpu_used=args.cpu_used,
        profile=profile,
    )
    print(
        f"Rendered {result.frame_count} frames to {result.output_path} "
        f"from {len(result.selected_ticks)} captured ticks",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
