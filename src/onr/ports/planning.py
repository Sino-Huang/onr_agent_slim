"""Public ports for planner execution."""

from collections.abc import Mapping
from typing import Protocol

from onr.contracts.planning import (
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlannerStaticCheckResult,
    SymbolicPlannerExecutionResult,
)


class MiniZincPlannerExecutor(Protocol):
    """Statically validate and execute generated MiniZinc assets."""

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult: ...

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        """Return MiniZinc acceptance plus exact process diagnostics."""
        ...


class FastDownwardPlannerExecutor(Protocol):
    """Execute generated PDDL assets."""

    def execute(
        self, assets: Mapping[str, bytes]
    ) -> SymbolicPlannerExecutionResult: ...


class SymbolicPlanValidator(Protocol):
    """Statically check PDDL and independently validate one persisted plan."""

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        """Return VAL acceptance plus exact process diagnostics."""
        ...

    def validate(self, evidence: PlannerExecutionEvidence) -> bool:
        """Return whether the persisted plan is valid for its domain and problem."""
        ...
