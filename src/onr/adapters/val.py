"""Independent VAL subprocess adapter for persisted PDDL plans."""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from onr.contracts.planning import PlannerExecutionEvidence


@dataclass(frozen=True, slots=True)
class VALPlanValidator:
    """Validate the exact domain, problem, and plan persisted by Fast Downward."""

    executable: Path | str
    arguments: tuple[str, ...] = ()
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        arguments = tuple(self.arguments)
        if not all(isinstance(argument, str) for argument in arguments):
            raise ValueError("validator arguments must be strings")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("validator timeout must be a positive finite number")
        object.__setattr__(self, "executable", Path(self.executable))
        object.__setattr__(self, "arguments", arguments)

    def validate(self, evidence: PlannerExecutionEvidence) -> bool:
        """Return whether VAL independently accepts the persisted plan."""

        if not isinstance(evidence, PlannerExecutionEvidence):
            raise TypeError("VAL validation requires Planner Execution Evidence")
        paths = {path.name: path for path in evidence.artifact_paths}
        required = {"domain.pddl", "problem.pddl", "sas_plan"}
        if not required.issubset(paths) or any(
            not paths[name].is_file() for name in required
        ):
            return False
        try:
            completed = subprocess.run(
                [
                    str(self.executable),
                    *self.arguments,
                    str(paths["domain.pddl"]),
                    str(paths["problem.pddl"]),
                    str(paths["sas_plan"]),
                ],
                capture_output=True,
                check=False,
                cwd=str(evidence.artifact_directory),
                text=True,
                timeout=self.timeout_seconds,
            )
            self._persist_output(
                evidence.artifact_directory,
                completed.stdout,
                completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            self._persist_output(
                evidence.artifact_directory,
                exc.stdout,
                exc.stderr or "VAL validation timed out",
            )
            return False
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            self._persist_output(
                evidence.artifact_directory,
                "",
                f"{type(exc).__name__}: {exc}",
            )
            return False
        return completed.returncode == 0 and "Plan valid" in completed.stdout

    @staticmethod
    def _persist_output(directory: Path, stdout: object, stderr: object) -> None:
        for name, value in (
            ("validator.stdout", stdout),
            ("validator.stderr", stderr),
        ):
            try:
                (directory / name).write_text(
                    VALPlanValidator._output_text(value),
                    encoding="utf-8",
                )
            except OSError:
                pass

    @staticmethod
    def _output_text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)


__all__ = ["VALPlanValidator"]
