"""Persist planning pauses and deterministically resolve operator decisions."""

from __future__ import annotations

from onr.contracts.human_decision import (
    HumanDecision,
    HumanDecisionAction,
    HumanDecisionCategory,
    HumanDecisionDisposition,
    HumanDecisionRequest,
    HumanDecisionResolution,
    RunCheckpoint,
)
from onr.ports.human_decisions import HumanDecisionStore

_ACTIONS = {
    HumanDecisionCategory.TRANSLATION_REPAIR_EXHAUSTED: (
        HumanDecisionAction.RETRY_TRANSLATION,
        HumanDecisionAction.END_MISSION_RUN,
    ),
    HumanDecisionCategory.UNSOLVABLE: (
        HumanDecisionAction.REVISE_MISSION_INTENT,
        HumanDecisionAction.END_MISSION_RUN,
    ),
    HumanDecisionCategory.INSUFFICIENT_SCENE_EVIDENCE: (
        HumanDecisionAction.WAIT_FOR_SCENE_EVIDENCE,
        HumanDecisionAction.END_MISSION_RUN,
    ),
    HumanDecisionCategory.TIMEOUT: (
        HumanDecisionAction.RETRY_PLANNER,
        HumanDecisionAction.END_MISSION_RUN,
    ),
}
_RESUME_ACTIONS = {
    HumanDecisionAction.RETRY_TRANSLATION,
    HumanDecisionAction.WAIT_FOR_SCENE_EVIDENCE,
    HumanDecisionAction.RETRY_PLANNER,
}


class HumanDecisionCoordinator:
    def __init__(self, store: HumanDecisionStore) -> None:
        for method in (
            "save_pause",
            "load_request_by_id",
            "load_checkpoint",
            "save_decision",
        ):
            if not callable(getattr(store, method, None)):
                raise TypeError("Human Decision store is incomplete")
        self.store = store

    def pause_for_outcome(
        self,
        outcome: object,
        checkpoint: RunCheckpoint,
        *,
        correlation_id: str,
        evidence_references: tuple[str, ...],
    ) -> HumanDecisionRequest:
        """Persist the operator request for one terminal planning outcome."""

        categories = {
            "repair_exhausted": (HumanDecisionCategory.TRANSLATION_REPAIR_EXHAUSTED),
            "unsolvable": HumanDecisionCategory.UNSOLVABLE,
            "insufficient_scene_evidence": (
                HumanDecisionCategory.INSUFFICIENT_SCENE_EVIDENCE
            ),
            "timeout": HumanDecisionCategory.TIMEOUT,
        }
        category = categories.get(str(outcome))
        if category is None:
            raise ValueError("planning outcome does not require a Human Decision")
        return self.pause(
            category,
            checkpoint,
            correlation_id=correlation_id,
            evidence_references=evidence_references,
        )

    def pause(
        self,
        category: HumanDecisionCategory | str,
        checkpoint: RunCheckpoint,
        *,
        correlation_id: str,
        evidence_references: tuple[str, ...],
    ) -> HumanDecisionRequest:
        category = HumanDecisionCategory(category)
        request = HumanDecisionRequest(
            request_id=(f"human-decision:{checkpoint.mission_run_id}:{category}"),
            mission_id=checkpoint.mission_id,
            mission_run_id=checkpoint.mission_run_id,
            category=category,
            correlation_id=correlation_id,
            checkpoint_id=checkpoint.checkpoint_id,
            evidence_references=evidence_references,
            permitted_actions=_ACTIONS[category],
        )
        self.store.save_pause(request, checkpoint)
        return request

    def request(self, request_id: str) -> HumanDecisionRequest | None:
        return self.store.load_request_by_id(request_id)

    def record(self, decision: HumanDecision) -> HumanDecisionResolution:
        request = self.request(decision.request_id)
        if request is None:
            raise ValueError("Human Decision Request is unknown")
        if (
            decision.mission_id != request.mission_id
            or decision.mission_run_id != request.mission_run_id
        ):
            raise ValueError("Human Decision does not match its request")
        if decision.action not in request.permitted_actions:
            raise ValueError("Human Decision action is not permitted")
        recorded = self.store.save_decision(decision)
        if recorded.action in _RESUME_ACTIONS:
            checkpoint = self.store.load_checkpoint(
                request.mission_id, request.mission_run_id
            )
            if checkpoint is None or checkpoint.checkpoint_id != request.checkpoint_id:
                raise RuntimeError("Human Decision Run Checkpoint is unavailable")
            return HumanDecisionResolution(
                recorded,
                HumanDecisionDisposition.RESUME,
                checkpoint,
            )
        return HumanDecisionResolution(
            recorded,
            HumanDecisionDisposition.END,
        )


__all__ = ["HumanDecisionCoordinator"]
