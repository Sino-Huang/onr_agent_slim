"""Environment-owned update source seam for closed-loop coordination."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from onr.contracts.environment import EnvironmentTickResult
from onr.contracts.transport import TransportEvent


class EnvironmentPlanningView(Protocol):
    """Environment-authored planning evidence and its artifact path."""

    @property
    def environment_event(self) -> TransportEvent: ...

    @property
    def environment_file(self) -> Path: ...


class EnvironmentUpdateSource(Protocol):
    """Own command consumption and expose ordered environment updates."""

    mission_id: str
    feedback_topic: str
    perception_topic: str
    environment_topic: str
    update_ownership: str
    cadence_seconds: float

    @property
    def current_time(self) -> float: ...

    @property
    def latest_environment_event(self) -> TransportEvent | None: ...

    @property
    def has_current_maneuver(self) -> bool: ...

    @property
    def is_alive(self) -> bool: ...

    def start(self, *, simulation_limit_seconds: float | None = None) -> None: ...

    def advance(self) -> EnvironmentTickResult: ...

    def drain_updates(self) -> tuple[EnvironmentTickResult, ...]: ...

    def wait_for_update(self, timeout: float | None = None) -> bool: ...

    def planning_view(self) -> EnvironmentPlanningView: ...

    def stop(self) -> None: ...

    def join(self) -> None: ...

    def raise_if_failed(self) -> None: ...


__all__ = ["EnvironmentPlanningView", "EnvironmentUpdateSource"]
