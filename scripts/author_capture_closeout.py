#!/usr/bin/env python
"""Recompute and author a capture closeout receipt from a capture manifest.

Reads a stepped-beat capture directory's ``capture-manifest.json``,
recomputes every integrity number (tick contiguity, sha256-verified images,
pre/post-flush landing statistics, aircraft pose errors, segmentation ID
accounting), asserts the invariants, and writes the adjacent
``capture-closeout-receipt.json`` the validator's retention check requires.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

TOLERANCE_S = 0.25
POSE_BOUND_M = 0.5


def _landing_stats(values: list[float]) -> dict[str, Any]:
    arr = np.asarray(values, dtype=float)
    return {
        "min": float(arr.min()),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(arr.max()),
        "tolerance_s": TOLERANCE_S,
        "violations": int(np.sum(np.abs(arr) > TOLERANCE_S)),
    }


def build_receipt(capture_dir: Path, tick_count: int) -> dict[str, Any]:
    manifest_path = capture_dir / "capture-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    ticks = manifest["ticks"]

    keys = sorted(int(key) for key in ticks)
    contiguous = keys == list(range(tick_count))
    all_three = all(
        len(ticks[str(tick)].get("images", [])) == 3 for tick in range(tick_count)
    )

    failures: list[dict[str, str]] = []
    verified = 0
    total_bytes = 0
    for tick in range(tick_count):
        for image in ticks[str(tick)].get("images", []):
            path = capture_dir / image["path"]
            if not path.is_file():
                failures.append({"path": image["path"], "error": "missing"})
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != image.get("sha256"):
                failures.append({"path": image["path"], "error": "sha256 mismatch"})
                continue
            verified += 1
            total_bytes += path.stat().st_size

    unknown_ids: list[int] = []
    landing_pre: list[float] = []
    landing_post: list[float] = []
    pose_errors: list[float] = []
    heading_errors: list[float] = []
    timestamps_identical = 0
    for tick in range(tick_count):
        record = ticks[str(tick)]
        unknown_ids.extend(int(value) for value in record.get("unknown_ids", []))
        landing_pre.extend(
            float(value) for value in record.get("landing_errors_s", {}).values()
        )
        landing_post.extend(
            float(value)
            for value in record.get("landing_errors_post_flush_s", {}).values()
        )
        pose = record.get("aircraft_pose", {})
        if pose:
            pose_errors.append(float(pose["max_abs_position_error_m"]))
            heading_errors.append(float(pose["heading_error_deg"]))
        if record.get("timestamps_identical"):
            timestamps_identical += 1

    violations_pre = int(np.sum(np.abs(np.asarray(landing_pre)) > TOLERANCE_S))
    violations_post = int(np.sum(np.abs(np.asarray(landing_post)) > TOLERANCE_S))
    pose_violations = int(np.sum(np.asarray(pose_errors) > POSE_BOUND_M))

    if not contiguous:
        raise SystemExit("capture ticks are not contiguous over the requested range")
    if not all_three:
        raise SystemExit("some ticks are missing one of the three image streams")
    if failures:
        raise SystemExit(f"image integrity failures: {failures[:5]}")
    if unknown_ids:
        raise SystemExit(f"unknown segmentation ids present: {sorted(set(unknown_ids))[:10]}")
    if violations_pre or violations_post:
        raise SystemExit(
            f"landing tolerance violations: pre={violations_pre} post={violations_post}"
        )
    if pose_violations:
        raise SystemExit(f"aircraft pose violations: {pose_violations}")
    if timestamps_identical != tick_count:
        raise SystemExit("paired image timestamps were not identical on every tick")

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Capture closeout receipt recomputed from the capture manifest; "
            "every number is derived here, nothing is hand-copied."
        ),
        "model": manifest.get("model"),
        "capture_dir": str(capture_dir.resolve()),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest_sha256,
        "ticks": {
            "count": tick_count,
            "range": [0, tick_count - 1],
            "contiguous": contiguous,
            "all_three_streams": all_three,
            "timestamps_identical": timestamps_identical,
        },
        "images": {
            "verified_sha256": verified,
            "expected": tick_count * 3,
            "total_bytes": total_bytes,
            "failures": failures,
        },
        "landing": {
            "pre_flush": _landing_stats(landing_pre),
            "post_flush": _landing_stats(landing_post),
        },
        "aircraft_pose": {
            "max_abs_position_error_m": float(np.max(pose_errors)),
            "max_heading_error_deg": float(np.max(heading_errors)),
            "bound_m": POSE_BOUND_M,
            "violations": pose_violations,
        },
        "unknown_ids": sorted(set(unknown_ids)),
        "summary": manifest.get("summary", {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--ticks", type=int, required=True)
    args = parser.parse_args(argv)
    receipt = build_receipt(args.capture_dir, args.ticks)
    output = args.capture_dir / "capture-closeout-receipt.json"
    output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: receipt[k] for k in ("ticks", "images")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
