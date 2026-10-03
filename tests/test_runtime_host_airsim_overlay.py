"""Scene-clock camera annotations from the perception module's own output."""

from __future__ import annotations

import json
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import numpy as np
from PIL import Image

from onr.runtime_host.airsim_overlay import PerceptionAnnotator
from onr.runtime_host.world import CameraCapture, WorldView

MISSION = "mission-7c1f9a2e"
ACTORS = {
    "12_yacht": {"entity": 12, "ship": "Ship 14", "object": 12},
    "3_boat": {"entity": 3, "ship": "Ship 03", "object": 3},
    "5_tug": {"entity": 5, "ship": "Ship 05", "object": 5},
}


def _perception_run(root: Path) -> Path:
    run = root / "perception/runs/perception-run-1"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "configuration": {
                    "actor_entity_ids": {actor: row["entity"] for actor, row in ACTORS.items()},
                    "actor_ship_ids": {actor: row["ship"] for actor, row in ACTORS.items()},
                    "actor_object_ids": {actor: row["object"] for actor, row in ACTORS.items()},
                }
            }
        ),
        encoding="utf-8",
    )
    return run


def _audit_row(stamp: int, observed: float) -> dict[str, Any]:
    return {
        "observation_time_s": observed,
        "airsim_image_timestamp_ns": stamp,
        "perceived": [
            {
                "bbox": {"x": 100, "y": 50, "w": 40, "h": 20},
                "score": 0.87,
                "ship_id": "Ship 14",
                "actor": "12_yacht",
                "association": {"reason": "gated"},
            },
            {
                "bbox": {"x": 300, "y": 80, "w": 10, "h": 10},
                "score": 0.41,
                "ship_id": None,
                "actor": None,
                "association": {"reason": "outside_gate"},
            },
        ],
    }


def test_yolo_boxes_are_drawn_only_for_the_frame_they_were_detected_in(tmp_path: Path) -> None:
    audit = _perception_run(tmp_path) / "perception_audit.jsonl"
    complete = json.dumps(_audit_row(1_000, 143.48)) + "\n"
    partial = json.dumps(_audit_row(2_000, 143.98))
    audit.write_text(complete + partial[:40], encoding="utf-8")
    annotator = PerceptionAnnotator(tmp_path, perception="yolo", mission_id=MISSION)

    same = annotator.annotate(sensor_timestamp_ns=1_000, mission_time_seconds=None, id_map=None)
    assert same.match == "exact"
    assert [(box.label, box.tone, box.x0, box.y1) for box in same.boxes] == [
        ("ship 12 · 0.87", "identified", 100.0, 69.0),
        ("unidentified (outside_gate) · 0.41", "unidentified", 300.0, 89.0),
    ]
    assert same.status()["perception_mission_time_seconds"] == 143.48

    # A half-written audit line is not a detection yet.
    later = annotator.annotate(sensor_timestamp_ns=2_000, mission_time_seconds=144.5, id_map=None)
    assert (later.match, later.boxes, later.headline) == (
        "none",
        (),
        "no perception sample for this frame (last 143.5 s)",
    )
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(partial[40:] + "\n")
    assert (
        annotator.annotate(sensor_timestamp_ns=2_000, mission_time_seconds=None, id_map=None).match
        == "exact"
    )


def _observation(directory: Path, index: int, entity: int, observed: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{index:020d}-perception.json").write_text(
        json.dumps(
            {
                "event_kind": "entity.observed",
                "payload": {
                    "entity_id": entity,
                    "observation_kind": "entity",
                    "observed_time": observed,
                },
            }
        ),
        encoding="utf-8",
    )


def test_ideal_perception_outlines_only_reported_ships_within_the_sensor_lag(
    tmp_path: Path,
) -> None:
    _perception_run(tmp_path)
    events = (
        tmp_path
        / "transport/topics/environment-perceptions/missions"
        / quote(MISSION, safe="")
    )
    _observation(events, 0, 3, 143.48)
    _observation(events, 1, 12, 143.48)
    id_map = np.zeros((40, 60), dtype=np.int32)
    id_map[2:6, 4:10] = 3
    id_map[20:30, 30:50] = 5  # visible, but perception did not report ship 5
    annotator = PerceptionAnnotator(tmp_path, perception="ideal", mission_id=MISSION)

    frame = annotator.annotate(sensor_timestamp_ns=0, mission_time_seconds=143.5, id_map=id_map)
    assert frame.match == "exact"
    assert frame.headline == "ideal perception · 2 ships reported · same frame"
    assert [(box.label, box.x0, box.y0, box.x1, box.y1) for box in frame.boxes] == [
        ("ship 3", 4, 2, 9, 5)
    ]

    # 0.52 s after the sample, or before it, is a different frame.
    for mission_time in (144.0, 143.4):
        other = annotator.annotate(sensor_timestamp_ns=0, mission_time_seconds=mission_time, id_map=id_map)
        assert (other.match, other.boxes) == ("none", ())
    assert annotator.annotate(sensor_timestamp_ns=0, mission_time_seconds=None, id_map=id_map).match == "none"


class FakeSdk:
    class ImageType:
        Scene = 0
        Segmentation = 5

    def __init__(self) -> None:
        self.requests: list[list[tuple[str, int]]] = []

    @staticmethod
    def ImageRequest(camera: str, image_type: int, *_args: Any) -> tuple[str, int]:
        return camera, image_type

    def VehicleClient(self, **_kwargs: Any) -> Any:
        sdk = self

        class Client:
            client = None

            def simGetImages(self, requests: list[tuple[str, int]], vehicle_name: str) -> list[Any]:
                sdk.requests.append(list(requests))
                pixels = np.full((54, 96, 3), 40, dtype=np.uint8)
                return [
                    SimpleNamespace(
                        image_data_uint8=pixels.tobytes(), width=96, height=54, time_stamp=1_000
                    )
                    for _ in requests
                ]

        return Client()


def test_scene_clock_capture_publishes_an_annotated_front_frame(tmp_path: Path) -> None:
    (tmp_path / "stack.json").write_text(json.dumps({"viewer_port": 5066}), encoding="utf-8")
    (_perception_run(tmp_path) / "perception_audit.jsonl").write_text(
        json.dumps(_audit_row(1_000, 143.5)) + "\n", encoding="utf-8"
    )
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {"Vehicles": {"SimpleFlight": {"Cameras": {"front_center_custom": {}, "third_person_demo": {}}}}}
        ),
        encoding="utf-8",
    )

    def viewer(url: str, _timeout: float, _limit: int) -> bytes:
        if url.endswith("/api/state"):
            return json.dumps({"state_version": 9, "mission_time_seconds": 143.5}).encode()
        raise FileNotFoundError(url)

    sdk = FakeSdk()
    capture = CameraCapture(
        tmp_path,
        cameras={"camera_front": "front_center_custom", "camera_third_person": "third_person_demo"},
        vehicle_name="SimpleFlight",
        rpc_port=41451,
        viewer_port=5066,
        airsim_settings=settings,
        annotator=PerceptionAnnotator(tmp_path, perception="yolo", mission_id=MISSION),
        period_seconds=0.01,
        sdk=sdk,
        fetch=viewer,
    )
    capture.start()
    deadline = time.monotonic() + 10
    while not (tmp_path / "world-frames/camera_front_annotated.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    live = WorldView(tmp_path, fetch=viewer).section()
    assert capture.stop()

    assert [frame["source"] for frame in live["frames"]] == [
        "camera_front_annotated",
        "camera_front",
        "camera_third_person",
    ]
    assert live["airsim"]["mode"] == "scene_clock"
    assert live["airsim"]["perception"] == "yolo"
    assert live["airsim"]["annotation"]["match"] == "exact"
    assert live["airsim"]["annotation"]["objects"] == 2
    # YOLO needs no segmentation request; the capture stays Scene-only.
    assert {tuple(request) for request in sdk.requests} == {
        (("front_center_custom", 0), ("third_person_demo", 0))
    }
    annotated = WorldView(tmp_path).frame("camera_front_annotated", live=False)
    assert Image.open(BytesIO(annotated.data)).size == (960, 540)
    metadata = json.loads((tmp_path / "world-frames/camera_front_annotated.json").read_text())
    assert metadata["sensor_timestamp_ns"] == 1_000
    assert metadata["boxes"][0]["label"] == "ship 12 · 0.87"
    assert WorldView(tmp_path).section(live=False)["airsim"]["state"] == "stopped"


def test_low_resolution_segmentation_boxes_land_in_scene_pixels() -> None:
    from onr.runtime_host.airsim_overlay import segmentation_boxes, segmentation_ids

    raw = np.zeros((4, 8, 3), dtype=np.uint8)
    raw[2:4, 6:8] = (0, 0, 14)  # object 14 in the bottom-right quarter
    response = SimpleNamespace(image_data_uint8=raw.tobytes(), width=8, height=4)

    ids = segmentation_ids(response, (40, 80))
    assert ids.shape == (40, 80)
    (box,) = segmentation_boxes(ids, {14: "ship 14"}, min_pixels=1)
    assert (box.x0, box.y0, box.x1, box.y1) == (60, 20, 79, 39)
