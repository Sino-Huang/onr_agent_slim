"""Environment submission port for abstract Maneuver Commands."""

from __future__ import annotations

from typing import Protocol

from onr.contracts.maneuver_control import ManeuverCommand


class ManeuverAdapter(Protocol):
    """Submit a maneuver without manufacturing lifecycle authority."""

    def submit(self, command: ManeuverCommand) -> object: ...


__all__ = ["ManeuverAdapter"]
