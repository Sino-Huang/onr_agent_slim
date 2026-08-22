"""Subprocess adapter for Fast Downward symbolic plan execution."""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlannerStaticCheckResult,
    PlanningOutcome,
    SymbolicPlannerExecutionResult,
)

_SEARCH_OPTION = "astar(lmcut())"
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

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        """Return Fast Downward acceptance plus its exact process output."""

        if set(assets) != {"domain.pddl", "problem.pddl"}:
            return PlannerStaticCheckResult(
                False,
                None,
                stderr="Fast Downward static check requires domain.pddl and problem.pddl.",
            )
        try:
            artifact_root = Path(self.artifact_root)
            artifact_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="check-", dir=artifact_root
            ) as temporary:
                directory = Path(temporary).resolve()
                paths = _materialize_assets(directory, assets)
                completed = subprocess.run(
                    [
                        str(self.executable),
                        *self.arguments,
                        "--translate",
                        str(paths["domain.pddl"]),
                        str(paths["problem.pddl"]),
                    ],
                    capture_output=True,
                    check=False,
                    cwd=str(directory),
                    text=True,
                    timeout=self.timeout_seconds,
                )
        except subprocess.TimeoutExpired as exc:
            return PlannerStaticCheckResult(
                False,
                None,
                stdout=_output_text(exc.stdout),
                stderr=(
                    _output_text(exc.stderr)
                    or f"Fast Downward static check timed out after {self.timeout_seconds} seconds."
                ),
            )
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            return PlannerStaticCheckResult(
                False,
                None,
                stderr=f"{type(exc).__name__}: {exc}",
            )
        return PlannerStaticCheckResult(
            completed.returncode == 0,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

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
                    return_code=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            if completed.returncode == 11:
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.UNSOLVABLE,
                    evidence=evidence,
                    return_code=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            if completed.returncode == 12:
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.INCOMPLETE,
                    evidence=evidence,
                    return_code=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            if completed.returncode != 0:
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.ERROR,
                    evidence=evidence,
                    return_code=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            if not plan_path.is_file():
                return SymbolicPlannerExecutionResult(
                    PlanningOutcome.ERROR,
                    evidence=evidence,
                    return_code=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            return SymbolicPlannerExecutionResult(
                outcome=PlanningOutcome.SOLVED,
                evidence=evidence,
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
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
                stdout=_output_text(exc.stdout),
                stderr=_output_text(
                    exc.stderr
                    or f"solver timed out after {self.timeout_seconds} seconds"
                ),
            )
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            _persist_solver_output(run_directory, "", f"{type(exc).__name__}: {exc}")
            return SymbolicPlannerExecutionResult(
                PlanningOutcome.ERROR,
                evidence=_execution_evidence(run_directory),
                stderr=f"{type(exc).__name__}: {exc}",
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
                    if path.is_file()
                    and path.name not in {"solver.stdout", "solver.stderr"}
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
    directory: Path, assets: Mapping[str, bytes]
) -> dict[str, Path]:
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
