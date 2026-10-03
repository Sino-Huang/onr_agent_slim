"""Normalize herdr live-demo launcher dry runs into comparable argv goldens.

The goldens were captured from the original shell launcher before it became a
thin wrapper over ``python -m onr.runtime_host.stack plan``; comparing parsed
argv (not shell text) keeps them independent of quoting style.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).parents[2]
PHYSICAL_ROOT = Path("/data/ccu/sukaih/ONR/onr_physical_runtime")
GOLDEN_PATH = Path(__file__).with_name("launcher_dry_run_goldens.json")
LAUNCHER_DIR = REPOSITORY / "scripts/live_demo_with_wm"
VIEWER_PORT = "5099"
COMMAND_KEYS = (
    "Physical command",
    "Agent command",
    "Worker command",
    "Engine command",
    "Perception command",
)

CASES: dict[str, tuple[str, dict[str, str]]] = {
    "mission1-harbor": ("herdr_start_live_demo.sh", {}),
    "mission1-airsim-yolo": ("herdr_start_mission1_airsim_live_demo.sh", {}),
    "mission1-airsim-ideal": (
        "herdr_start_mission1_airsim_live_demo.sh",
        {"ONR_DEMO_PERCEPTION": "ideal"},
    ),
    "mission2": ("herdr_start_mission2_live_demo.sh", {}),
    "mission3": ("herdr_start_mission3_live_demo.sh", {}),
    "mission4": ("herdr_start_mission4_live_demo.sh", {}),
    "joint": (
        "herdr_start_live_demo.sh",
        {
            "ONR_DEMO_MISSION_MODE": "joint",
            "ONR_DEMO_MISSION1_INSTANCE": str(
                PHYSICAL_ROOT / "data/harbor_world/mission1_instances/demo-001"
            ),
        },
    ),
    "joint24": ("herdr_start_live_demo.sh", {"ONR_DEMO_MISSION_MODE": "joint24"}),
    "joint34": ("herdr_start_live_demo.sh", {"ONR_DEMO_MISSION_MODE": "joint34"}),
    "joint34-airsim-rpc": (
        "herdr_start_live_demo.sh",
        {
            "ONR_DEMO_MISSION_MODE": "joint34",
            "ONR_DEMO_AIRSIM_RPC_URL": "http://127.0.0.1:8767",
            "ONR_DEMO_CAMERA_OWNER": "runtime",
            "ONR_DEMO_MISSION3_FIXTURE": str(
                PHYSICAL_ROOT / "config/joint34_demo/mission3_private_fixture.json"
            ),
            "ONR_DEMO_MANEUVER_SECONDS": "45",
            "ONR_DEMO_SIMULATION_LIMIT_SECONDS": "120",
        },
    ),
}


def dry_run(case: str) -> subprocess.CompletedProcess[str]:
    script, overrides = CASES[case]
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment.update(
        ONR_DEMO_DRY_RUN="1", ONR_DEMO_VIEWER_PORT=VIEWER_PORT, **overrides
    )
    return subprocess.run(
        ["bash", str(LAUNCHER_DIR / script), "05_onr"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _changed_lines(source: Path, materialized: Path) -> list[list[str]]:
    original = source.read_text(encoding="utf-8").splitlines()
    rewritten = materialized.read_text(encoding="utf-8").splitlines()
    assert len(original) == len(rewritten), materialized
    return [
        [old, new] for old, new in zip(original, rewritten, strict=True) if old != new
    ]


def normalize(stdout: str) -> dict[str, Any]:
    """Parse one dry-run stdout into normalized argv and materialized state."""

    lines = stdout.splitlines()
    fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    run_root = Path(fields["Run configuration"])

    def norm(value: str) -> str:
        return value.replace(str(run_root), "<RUN_ROOT>").replace(
            run_root.name, "<RUN_NAME>"
        )

    commands: dict[str, object] = {}
    for key in COMMAND_KEYS:
        if key not in fields:
            continue
        outer = shlex.split(fields[key])
        assert outer[:2] == ["bash", "-lc"] and len(outer) == 3, outer
        prefix, executable = outer[2].rsplit("exec ", 1)
        commands[key] = {
            "prefix": [norm(token) for token in shlex.split(prefix)],
            "argv": [norm(token) for token in shlex.split(executable)],
        }
    configs = {
        name: [
            [norm(old), norm(new)]
            for old, new in _changed_lines(REPOSITORY / "conf" / name, run_root / name)
        ]
        for name in ("onr_agent_params.yaml", "environment_physical.yaml")
    }
    return {
        "header": lines[0],
        "run_parent": str(run_root.parent.relative_to(REPOSITORY)),
        "run_entries": sorted(entry.name for entry in run_root.iterdir()),
        "commands": commands,
        "audit": [norm(token) for token in shlex.split(fields["Terminal audit"])],
        "configs": configs,
    }


def capture(case: str) -> dict[str, Any]:
    result = dry_run(case)
    assert result.returncode == 0, result.stderr
    run_root = Path(
        next(
            line.removeprefix("Run configuration: ")
            for line in result.stdout.splitlines()
            if line.startswith("Run configuration: ")
        )
    )
    try:
        return normalize(result.stdout)
    finally:
        shutil.rmtree(run_root)


def load_goldens() -> dict[str, dict[str, Any]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    GOLDEN_PATH.write_text(
        json.dumps({case: capture(case) for case in CASES}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
