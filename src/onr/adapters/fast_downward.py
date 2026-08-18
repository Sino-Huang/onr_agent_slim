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

    def execute(self, assets: Mapping[str, bytes]) -> SymbolicPlannerExecutionResult:
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                paths = _materialize_assets(root, assets)
                plan_path = (root / "sas_plan").resolve()
                if plan_path.parent != root:
                    return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)
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
                    cwd=str(root),
                    text=True,
                    timeout=self.timeout_seconds,
                )
                if completed.returncode == 21 or completed.returncode == 23:
                    return SymbolicPlannerExecutionResult(PlanningOutcome.TIMEOUT)
                if completed.returncode == 11:
                    return SymbolicPlannerExecutionResult(PlanningOutcome.UNSOLVABLE)
                if completed.returncode == 12:
                    return SymbolicPlannerExecutionResult(PlanningOutcome.INCOMPLETE)
                if completed.returncode != 0:
                    return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)
                return _parse_plan_file(plan_path)
        except subprocess.TimeoutExpired:
            return SymbolicPlannerExecutionResult(PlanningOutcome.TIMEOUT)
        except (OSError, TypeError, ValueError, UnicodeError):
            return SymbolicPlannerExecutionResult(PlanningOutcome.ERROR)


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
