"""Public ports for planner execution."""

from collections.abc import Mapping
from typing import Protocol

from onr.contracts.planning import (
    PlannerExecutionResult,
    PlannerExecutionEvidence,
    SymbolicPlannerExecutionResult,
)


class TemporalPlannerExecutor(Protocol):
    """Executes temporal planner assets and returns timing assignments."""

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        """Return one terminal planner execution result."""
        ...


class MiniZincPlannerExecutor(TemporalPlannerExecutor, Protocol):
    """Statically validate and execute generated MiniZinc assets."""

    def check(self, assets: Mapping[str, bytes]) -> bool:
        """Return whether MiniZinc accepts the model instance."""
        ...


class SymbolicPlannerExecutor(Protocol):
    """Executes symbolic planner assets and returns ordered action calls."""

    def execute(self, assets: Mapping[str, bytes]) -> SymbolicPlannerExecutionResult:
        """Return one terminal symbolic planner execution result."""
        ...


class FastDownwardPlannerExecutor(SymbolicPlannerExecutor, Protocol):
    """Statically validate and execute generated PDDL assets."""

    def check(self, assets: Mapping[str, bytes]) -> bool:
        """Return whether Fast Downward translates the PDDL."""
        ...


class SymbolicPlanValidator(Protocol):
    """Independently validate one persisted symbolic planner result."""

    def validate(self, evidence: PlannerExecutionEvidence) -> bool:
        """Return whether the persisted plan is valid for its domain and problem."""
        ...
