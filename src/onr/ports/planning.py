"""Public ports for planner execution."""

from collections.abc import Mapping
from typing import Protocol

from onr.contracts.planning import (
    PlannerStaticCheckResult,
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    SymbolicPlannerExecutionResult,
)


class MiniZincPlannerExecutor(Protocol):
    """Statically validate and execute generated MiniZinc assets."""

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult: ...

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        """Return MiniZinc acceptance plus exact process diagnostics."""
        ...


class FastDownwardPlannerExecutor(Protocol):
    """Statically validate and execute generated PDDL assets."""

    def execute(
        self, assets: Mapping[str, bytes]
    ) -> SymbolicPlannerExecutionResult: ...

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        """Return Fast Downward acceptance plus exact process diagnostics."""
        ...


class SymbolicPlanValidator(Protocol):
    """Independently validate one persisted symbolic planner result."""

    def validate(self, evidence: PlannerExecutionEvidence) -> bool:
        """Return whether the persisted plan is valid for its domain and problem."""
        ...
