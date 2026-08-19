"""Subprocess adapter for Fast Downward symbolic plan execution."""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlanningOutcome,
    SymbolicActionCall,
    SymbolicPlannerExecutionResult,
)


_SEARCH_OPTION = "astar(lmcut())"
_COST_TRAILER = re.compile(r"; cost = ([0-9]+) \((unit cost|general cost)\)\Z")
_ACTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_ACTION_ARGUMENT = re.compile(r"[^()\s]+\Z")


@dataclass(frozen=True, slots=True)
class FastDownwardExecutor:
    """Execute plain PDDL assets with a configured Fast Downward driver."""

    executable: Path | str
    artifact_root: Path | str
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

    def execute(self, assets: Mapping[str, bytes]) -> SymbolicPlannerExecutionResult:
        try:
            artifact_root = Path(self.artifact_root)
            artifact_root.mkdir(parents=True, exist_ok=True)
            run_directory = Path(
                tempfile.mkdtemp(prefix="run-", dir=artifact_root)
            ).resolve()
        except (OSError, TypeError, ValueError):
            return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)

        try:
            paths = _materialize_assets(run_directory, assets)
            plan_path = (run_directory / "sas_plan").resolve()
            if plan_path.parent != run_directory:
                raise ValueError("planner result path escaped the artifact directory")
            completed = subprocess.run(
                [
                    str(self.executable),
                    *self.arguments,
                    "--plan-file",
                    str(plan_path),
                    str(paths["domain.pddl"]),
                    str(paths["problem.pddl"]),
                    "--search",
                    _SEARCH_OPTION,
                ],
                capture_output=True,
                check=False,
                cwd=str(run_directory),
                text=True,
                timeout=self.timeout_seconds,
            )
            _persist_solver_output(run_directory, completed.stdout, completed.stderr)
            evidence = _execution_evidence(run_directory)
            if completed.returncode == 21 or completed.returncode == 23:
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.TIMEOUT,
                    evidence=evidence,
                )
            if completed.returncode == 11:
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.UNSOLVABLE,
                    evidence=evidence,
                )
            if completed.returncode == 12:
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.INCOMPLETE,
                    evidence=evidence,
                )
            if completed.returncode != 0:
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.ERROR,
                    evidence=evidence,
                )
            parsed = _parse_plan_file(plan_path)
            return SymbolicPlannerExecutionResult(
                outcome=parsed.outcome,
                action_calls=parsed.action_calls,
                total_plan_cost=parsed.total_plan_cost,
                evidence=evidence,
            )
        except subprocess.TimeoutExpired as exc:
            _persist_solver_output(
                run_directory,
                exc.stdout,
                exc.stderr or f"solver timed out after {self.timeout_seconds} seconds",
            )
            return SymbolicPlannerExecutionResult(
                PlanningOutcome.TIMEOUT,
                evidence=_execution_evidence(run_directory),
            )
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            _persist_solver_output(run_directory, "", f"{type(exc).__name__}: {exc}")
            return SymbolicPlannerExecutionResult(
                PlanningOutcome.ERROR,
                evidence=_execution_evidence(run_directory),
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


def _materialize_assets(directory: Path, assets: Mapping[str, bytes]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name, content in assets.items():
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError("planner asset names must be plain filenames")
        if not isinstance(content, bytes):
            raise ValueError("planner assets must contain bytes")
        path = (directory / name).resolve()
        if path.parent != directory:
            raise ValueError("planner asset path escaped the temporary directory")
        path.write_bytes(content)
        paths[name] = path
    if set(paths) != {"domain.pddl", "problem.pddl"}:
        raise ValueError("Fast Downward requires domain.pddl and problem.pddl assets")
    return paths


def _parse_plan_file(path: Path) -> SymbolicPlannerExecutionResult:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError):
        return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)

    nonblank = [line for line in lines if line]
    if not nonblank:
        return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)
    trailer_match = _COST_TRAILER.fullmatch(nonblank[-1])
    if trailer_match is None:
        return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)

    calls: list[SymbolicActionCall] = []
    for line in nonblank[:-1]:
        if not line.startswith("(") or not line.endswith(")"):
            return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)
        tokens = line[1:-1].split()
        if (
            not tokens
            or _ACTION_NAME.fullmatch(tokens[0]) is None
            or any(_ACTION_ARGUMENT.fullmatch(token) is None for token in tokens[1:])
        ):
            return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)
        try:
            calls.append(SymbolicActionCall(action=tokens[0], arguments=tuple(tokens[1:])))
        except ValueError:
            return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)

    return SymbolicPlannerExecutionResult(
        outcome=PlanningOutcome.SOLVED,
        action_calls=tuple(calls),
        total_plan_cost=int(trailer_match.group(1)),
    )
