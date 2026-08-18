"""Public ports for planner execution."""

from collections.abc import Mapping
from typing import Protocol

from onr.contracts.planning import PlannerExecutionResult


class TemporalPlannerExecutor(Protocol):
    """Executes temporal planner assets and returns timing assignments."""

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        """Return one terminal planner execution result."""
        ...
