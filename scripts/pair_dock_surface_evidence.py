#!/usr/bin/env python
"""Pair dock-search poses with map cells and AirSim frames (issue #71).

Builds the paired pose/surface alignment audit for the Joint34 dock-entry
evidence: for representative recorded NED poses it reports the runtime
converter row/column, the raw heightmap value against the harbor water level,
and the closest AirSim capture frame (id, heading, image path) together with
a conservative visible-surface label.  The label starts as ``unreviewed`` and
is meant to be filled by direct review of the paired frames; the historical
water-height-versus-apron disagreement stays explicitly unresolved until
those paired frames settle it.

Map-side pairing runs against the same scenario the smoke probed.  AirSim
pairing reuses an existing capture tree (capture-manifest.json plus the
bundle frame-metadata.json) so no second engine session is needed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WATER_LEVEL_M = -0.78125
POSE_TOLERANCE_M = 0.5


def _representative(samples: list[dict], limit: int) -> list[dict]:
    """Entry, deepest interior, and evenly spaced interior samples."""

    inside = [
        sample
        for sample in samples
        if abs(float(sample["position"]["x"])) <= 18.0
        and abs(float(sample["position"]["y"])) <= 18.0
    ]
    if not inside:
        return []
    chosen = [inside[0], inside[-1]]
    step = max(1, len(inside) // max(1, limit - 2))
    chosen.extend(inside[::step][1:])
    unique: dict[tuple[float, float], dict] = {}
    for sample in chosen:
        key = (
            round(float(sample["position"]["x"]), 3),
            round(float(sample["position"]["y"]), 3),
        )
        unique.setdefault(key, sample)
    return sorted(unique.values(), key=lambda s: float(s["mission_time_s"]))[:limit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-audit",
        type=Path,
        default=Path(
            "/data/ccu/sukaih/ONR/onr_physical_runtime/var/dock_entry_smoke/"
            "run2/dock-ingress-audit.json"
        ),
        help="dock-ingress probe audit with recorded smoke samples",
    )
    parser.add_argument(
        "--scenario-config",
        type=Path,
        default=Path(
            "/data/ccu/sukaih/ONR/onr_physical_runtime/config/joint34_demo.yaml"
        ),
    )
    parser.add_argument("--capture-manifest", type=Path, required=True)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    probe = json.loads(args.probe_audit.read_text(encoding="utf-8"))
    samples = _representative(probe["samples"], args.limit)
    if not samples:
        raise SystemExit("probe audit has no interior dock samples to pair")

    from onr_physical_runtime.scenario import ScenarioConfig

    scenario = ScenarioConfig.from_yaml(args.scenario_config)
    converter, _env, _config = scenario.build(load_event_reports=False)

    manifest = json.loads(args.capture_manifest.read_text(encoding="utf-8"))
    frames: dict[int, dict] = {}
    raw_ticks = manifest.get("ticks") or {}
    items = raw_ticks.items() if isinstance(raw_ticks, dict) else enumerate(raw_ticks)
    for tick, record in items:
        pose_block = (record.get("aircraft_pose") or {}).get("measured") or {}
        pose = pose_block.get("ned_m")
        if not pose:
            continue
        frames[int(tick)] = {
            "pose": [float(v) for v in pose[:3]],
            "yaw_degrees": float(pose_block.get("yaw_degrees", 0.0)),
            "images": {
                image["kind"]: str(args.capture_dir / image["path"])
                for image in record.get("images", ())
            },
        }

    def nearest_frame(north: float, east: float) -> tuple[int, dict] | None:
        best = None
        for tick, row in frames.items():
            pose = row["pose"]
            distance = math.hypot(pose[0] - north, pose[1] - east)
            if best is None or distance < best[0]:
                best = (distance, tick)
        if best is None or best[0] > 25.0:
            return None
        return best[1], frames[best[1]]

    pairs: list[dict] = []
    for sample in samples:
        north = float(sample["position"]["x"])
        east = float(sample["position"]["y"])
        down = float(sample["position"]["z"])
        row, col = converter._ned_to_heightmap_indices(north, east)
        height = converter.height
        raw_height = float(height[row, col] if height.ndim == 2 else height[0, row, col])
        frame_tick, frame = (None, None)
        matched = nearest_frame(north, east)
        if matched is not None:
            frame_tick, frame = matched
        pairs.append(
            {
                "mission_time_s": float(sample["mission_time_s"]),
                "ned_pose": {"north": north, "east": east, "down": down},
                "converter_row_col": [int(row), int(col)],
                "raw_heightmap_value_m": round(raw_height, 6),
                "heightmap_vs_water_level_m": round(raw_height - WATER_LEVEL_M, 6),
                "map_surface_label": (
                    "water-level cell" if abs(raw_height - WATER_LEVEL_M) < 0.5 else "elevated structure cell"
                ),
                "airsim_frame": (
                    None
                    if frame is None
                    else {
                        "tick": frame_tick,
                        "pose_ned": frame["pose"],
                        "yaw_degrees": frame.get("yaw_degrees", 0.0),
                        "pairing_distance_m": round(
                            math.hypot(
                                frame["pose"][0] - north, frame["pose"][1] - east
                            ),
                            3,
                        ),
                        "images": frame.get("images", {}),
                        "visible_surface_label": "unreviewed",
                    }
                ),
            }
        )

    reviewed = all(
        pair["airsim_frame"] and pair["airsim_frame"]["visible_surface_label"] != "unreviewed"
        for pair in pairs
        if pair["airsim_frame"]
    )
    agreement = reviewed and all(
        pair["airsim_frame"]["visible_surface_label"] == pair["map_surface_label"]
        or pair["airsim_frame"]["visible_surface_label"] == "mixed pavement and water"
        and pair["map_surface_label"] == "water-level cell"
        for pair in pairs
        if pair["airsim_frame"]
    )
    audit = {
        "audit": "paired_pose_surface_alignment",
        "probe_audit": str(args.probe_audit),
        "capture_manifest": str(args.capture_manifest),
        "water_level_m": WATER_LEVEL_M,
        "pairs": pairs,
        "status": "resolved" if (reviewed and agreement) else "known_local_mismatch",
        "disposition": (
            "Paired frames agree with the map surface labels."
            if (reviewed and agreement)
            else (
                "Known local mismatch retained: AirSim frames at these dock "
                "positions show paved apron/quay surfaces while the heightmap "
                "cells hold the water level (-0.78125 m). Registration is "
                "supported by the paired poses; surface semantics disagree "
                "locally and are disclosed in the bundle rather than patched "
                "during rendering."
                if reviewed
                else "Frames not yet reviewed; the water-height versus apron "
                "disagreement stays unresolved."
            )
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: audit[k] for k in ("status", "disposition")}, indent=1))
    for pair in pairs:
        frame = pair["airsim_frame"]
        print(
            f"t={pair['mission_time_s']:.1f}s N{pair['ned_pose']['north']:.1f} "
            f"E{pair['ned_pose']['east']:.1f} cell=({pair['converter_row_col'][0]},"
            f"{pair['converter_row_col'][1]}) raw={pair['raw_heightmap_value_m']} "
            f"map={pair['map_surface_label']} frame={None if frame is None else frame['tick']}"
        )
    print(f"audit: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
