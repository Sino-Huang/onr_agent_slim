"""Subprocess adapter for MiniZinc JSON-stream execution."""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from onr.contracts.planning import (
    PlannerExecutionResult,
    PlanningOutcome,
    TemporalAssignment,
)


_MINIZINC_ARGUMENTS = (
    "--solver",
    "gecode",
    "--json-stream",
    "--output-mode",
    "json",
)
_SOLVED_STATUSES = {"SATISFIED", "ALL_SOLUTIONS", "OPTIMAL_SOLUTION"}


@dataclass(frozen=True, slots=True)
class MiniZincExecutor:
    """Execute planner-native assets with the configured MiniZinc executable."""

    executable: Path
    arguments: tuple[str, ...] = ()
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        executable = Path(self.executable)
        arguments = tuple(self.arguments)
        if not all(isinstance(argument, str) for argument in arguments):
            raise ValueError("executor arguments must be strings")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("executor timeout must be a positive finite number")
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "arguments", arguments)

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        try:
            with tempfile.TemporaryDirectory() as directory:
                asset_paths = _materialize_assets(Path(directory), assets)
                completed = subprocess.run(
                    [
                        str(self.executable),
                        *self.arguments,
                        *_MINIZINC_ARGUMENTS,
                        *map(str, asset_paths),
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=self.timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            return PlannerExecutionResult(outcome=PlanningOutcome.TIMEOUT)
        except (OSError, TypeError, ValueError, UnicodeError):
            return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)

        if completed.returncode != 0:
            return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)
        return _parse_json_stream(completed.stdout)


def _materialize_assets(
    directory: Path,
    assets: Mapping[str, bytes],
) -> tuple[Path, ...]:
    paths: dict[str, Path] = {}
    for name, content in assets.items():
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError("planner asset names must be plain filenames")
        if not isinstance(content, bytes):
            raise ValueError("planner assets must contain bytes")
        path = directory / name
        path.write_bytes(content)
        paths[name] = path

    preferred = [name for name in ("model.mzn", "data.dzn") if name in paths]
    preferred.extend(sorted(set(paths) - set(preferred)))
    return tuple(paths[name] for name in preferred)


def _parse_json_stream(stream: str) -> PlannerExecutionResult:
    solution: object | None = None
    terminal_status: str | None = None

    for line in stream.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)

        if event["type"] == "solution":
            output = event.get("output")
            if not isinstance(output, dict) or not isinstance(
                output.get("default"), str
            ):
                return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)
            try:
                solution = json.loads(output["default"])
            except json.JSONDecodeError:
                return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)
        elif event["type"] == "status":
            status = event.get("status")
            if not isinstance(status, str):
                return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)
            terminal_status = status
        elif event["type"] == "error":
            return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)

    if terminal_status == "UNSATISFIABLE":
        return PlannerExecutionResult(outcome=PlanningOutcome.UNSOLVABLE)
    if terminal_status in (None, "UNKNOWN"):
        return PlannerExecutionResult(outcome=PlanningOutcome.INCOMPLETE)
    if terminal_status not in _SOLVED_STATUSES or solution is None:
        return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)

    assignments = _parse_assignments(solution)
    if assignments is None:
        return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)
    return PlannerExecutionResult(
        outcome=PlanningOutcome.SOLVED,
        assignments=assignments,
    )


def _parse_assignments(value: object) -> tuple[TemporalAssignment, ...] | None:
    if not isinstance(value, dict) or set(value) != {"assignments"}:
        return None
    raw_assignments = value["assignments"]
    if not isinstance(raw_assignments, list):
        return None

    assignments = []
    for raw_assignment in raw_assignments:
        if not isinstance(raw_assignment, dict) or set(raw_assignment) != {
            "maneuver_id",
            "start",
            "duration",
        }:
            return None
        maneuver_id = raw_assignment["maneuver_id"]
        start = raw_assignment["start"]
        duration = raw_assignment["duration"]
        if not isinstance(maneuver_id, str) or not _is_int(start) or not _is_int(
            duration
        ):
            return None
        try:
            assignment = TemporalAssignment(
                maneuver_id=maneuver_id,
                start=start,
                duration=duration,
            )
        except ValueError:
            return None
        assignments.append(assignment)
    return tuple(assignments)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
