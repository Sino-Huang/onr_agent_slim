"""Subprocess adapter for MiniZinc JSON-stream execution."""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from onr.contracts.planning import (
    JsonScalar,
    ManeuverParameter,
    PlannerExecutionEvidence,
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
_OPTIMAL_STATUS = "OPTIMAL_SOLUTION"


@dataclass(frozen=True, slots=True)
class MiniZincExecutor:
    """Execute planner-native assets with the configured MiniZinc executable."""

    executable: Path
    artifact_root: Path
    arguments: tuple[str, ...] = ()
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        executable = Path(self.executable)
        artifact_root = Path(self.artifact_root).expanduser().resolve()
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
        if artifact_root.exists() and not artifact_root.is_dir():
            raise ValueError("planner artifact root must be a directory")
        object.__setattr__(self, "artifact_root", artifact_root)
        object.__setattr__(self, "arguments", arguments)

    def check(self, assets: Mapping[str, bytes]) -> bool:
        """Return whether MiniZinc accepts the generated model instance."""

        if set(assets) != {"model.mzn", "data.dzn"}:
            return False
        try:
            artifact_root = Path(self.artifact_root)
            artifact_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="check-", dir=artifact_root
            ) as temporary:
                directory = Path(temporary).resolve()
                asset_paths = _materialize_assets(directory, assets)
                completed = subprocess.run(
                    [
                        str(self.executable),
                        *self.arguments,
                        "--instance-check-only",
                        *map(str, asset_paths),
                    ],
                    capture_output=True,
                    check=False,
                    cwd=str(directory),
                    text=True,
                    timeout=self.timeout_seconds,
                )
        except (OSError, TypeError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        try:
            artifact_root = Path(self.artifact_root)
            artifact_root.mkdir(parents=True, exist_ok=True)
            run_directory = Path(
                tempfile.mkdtemp(prefix="run-", dir=artifact_root)
            ).resolve()
        except (OSError, TypeError, ValueError):
            return PlannerExecutionResult(outcome=PlanningOutcome.ERROR)

        try:
            asset_paths = _materialize_assets(run_directory, assets)
            completed = subprocess.run(
                [
                    str(self.executable),
                    *self.arguments,
                    *_MINIZINC_ARGUMENTS,
                    *map(str, asset_paths),
                ],
                capture_output=True,
                check=False,
                cwd=str(run_directory),
                text=True,
                timeout=self.timeout_seconds,
            )
            _persist_solver_output(run_directory, completed.stdout, completed.stderr)
            evidence = _execution_evidence(run_directory)
        except subprocess.TimeoutExpired as exc:
            _persist_solver_output(
                run_directory,
                exc.stdout,
                exc.stderr or f"solver timed out after {self.timeout_seconds} seconds",
            )
            return PlannerExecutionResult(
                outcome=PlanningOutcome.TIMEOUT,
                evidence=_execution_evidence(run_directory),
            )
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            _persist_solver_output(run_directory, "", f"{type(exc).__name__}: {exc}")
            return PlannerExecutionResult(
                outcome=PlanningOutcome.ERROR,
                evidence=_execution_evidence(run_directory),
            )

        if completed.returncode != 0:
            return PlannerExecutionResult(
                outcome=PlanningOutcome.ERROR,
                evidence=evidence,
            )
        parsed = _parse_json_stream(completed.stdout)
        return PlannerExecutionResult(
            outcome=parsed.outcome,
            assignments=parsed.assignments,
            evidence=evidence,
        )


def _persist_solver_output(directory: Path, stdout: object, stderr: object) -> None:
    for name, value in (("solver.stdout", stdout), ("solver.stderr", stderr)):
        try:
            (directory / name).write_text(_output_text(value), encoding="utf-8")
        except OSError:
            pass


def _output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _execution_evidence(directory: Path) -> PlannerExecutionEvidence:
    try:
        artifact_paths = tuple(
            sorted(
                (
                    path.resolve()
                    for path in directory.iterdir()
                    if path.is_file() and path.name not in {"solver.stdout", "solver.stderr"}
                ),
                key=lambda path: path.name,
            )
        )
    except OSError:
        artifact_paths = ()
    return PlannerExecutionEvidence(
        artifact_directory=directory,
        artifact_paths=artifact_paths,
        stdout_path=directory / "solver.stdout",
        stderr_path=directory / "solver.stderr",
    )


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
        path = (directory / name).resolve()
        if path.parent != directory:
            raise ValueError("planner asset path escaped the artifact directory")
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
    if terminal_status in (None, "UNKNOWN", "SATISFIED", "ALL_SOLUTIONS"):
        return PlannerExecutionResult(outcome=PlanningOutcome.INCOMPLETE)
    if terminal_status != _OPTIMAL_STATUS or solution is None:
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
        if not isinstance(raw_assignment, dict):
            return None
        required = {"maneuver_id", "start", "duration"}
        if set(raw_assignment) not in (required, required | {"parameters"}):
            return None
        maneuver_id = raw_assignment["maneuver_id"]
        start = raw_assignment["start"]
        duration = raw_assignment["duration"]
        raw_parameters = raw_assignment.get("parameters", {})
        if not isinstance(raw_parameters, dict) or not all(
            isinstance(name, str) for name in raw_parameters
        ):
            return None

        if (
            not isinstance(maneuver_id, str)
            or not _is_int(start)
            or not _is_int(duration)
        ):
            return None
        try:
            assignment = TemporalAssignment(
                maneuver_id=maneuver_id,
                start=start,
                duration=duration,
                parameters=tuple(
                    ManeuverParameter(name, cast(JsonScalar, parameter_value))
                    for name, parameter_value in raw_parameters.items()
                ),
            )
        except (TypeError, ValueError):
            return None
        assignments.append(assignment)
    return tuple(assignments)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
