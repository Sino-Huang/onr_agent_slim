"""Persistence port for Human Decision pause and resume records."""

from typing import Protocol

from onr.contracts.human_decision import (
    HumanDecision,
    HumanDecisionRequest,
    RunCheckpoint,
)


class HumanDecisionStore(Protocol):
    def save_pause(
        self, request: HumanDecisionRequest, checkpoint: RunCheckpoint
    ) -> None: ...

    def load_request_by_id(self, request_id: str) -> HumanDecisionRequest | None: ...

    def load_checkpoint(
        self, mission_id: str, mission_run_id: str
    ) -> RunCheckpoint | None: ...

    def save_decision(self, decision: HumanDecision) -> HumanDecision: ...


__all__ = ["HumanDecisionStore"]
