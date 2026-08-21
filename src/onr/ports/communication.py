"""Synchronous correlated communication seam for mission agents."""

from __future__ import annotations

from typing import Protocol

from onr.contracts.communication import AgentMessage
from onr.contracts.transport import CommandOutcome


class CommunicationPort(Protocol):
    """Request a response from one registered agent recipient."""

    def request(self, message: AgentMessage) -> CommandOutcome: ...

    def available_recipients(self, sender: str) -> tuple[str, ...]: ...


__all__ = ["CommunicationPort"]
