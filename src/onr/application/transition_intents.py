"""Append-only Transition Intent persistence and live-authority validation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from onr.contracts.fsm import FSMStatus, TransitionCandidate
from onr.contracts.transition_intent import (
    ManeuverFSMContext,
    ManeuverTransitionCandidate,
    TransitionIntent,
    TransitionIntentStatus,
)
from onr.contracts.transport import TransportEvent


class TransitionIntentJournal:
    """Persist and reconstruct the latest agent-owned transition selection."""

    def __init__(self, transport: Any, *, topic: str = "transition-intents") -> None:
        self.transport = transport
        self.topic = topic

    def latest(self, mission_id: str) -> TransitionIntent | None:
        event = self.transport.latest_event(
            self.topic, mission_id, event_kind="transition-intent"
        )
        if event is None:
            return None
        if not isinstance(event, TransportEvent):
            raise TypeError("transition intent transport returned an invalid event")
        return TransitionIntent.from_dict(event.payload)

    def current(
        self,
        status: FSMStatus,
        *,
        invalidate_stale: bool = False,
    ) -> TransitionIntent | None:
        intent = self.latest(status.mission_id)
        if intent is None or not intent.is_selected:
            return None
        if self._matches_status(intent, status):
            return intent
        if invalidate_stale:
            self._publish(replace(intent, status=TransitionIntentStatus.INVALIDATED))
        return None

    def select(
        self,
        status: FSMStatus,
        target_state: str,
        rationale: str,
        *,
        selected_at: float,
    ) -> TransitionIntent:
        candidate = self.exact_candidate(status, target_state)
        current = self.current(status, invalidate_stale=True)
        if (
            current is not None
            and current.target_state == candidate.target
            and current.condition == candidate.transition_context
        ):
            return current
        revision = self.transport.next_event_sequence(self.topic, status.mission_id)
        intent = TransitionIntent(
            intent_id=(
                f"transition-intent-selection:{status.mission_id}:{revision}"
            ),
            mission_id=status.mission_id,
            plan_revision=status.plan_revision,
            statechart_revision=status.statechart_revision,
            source_state=status.active_state,
            target_state=candidate.target,
            condition=candidate.transition_context,
            state_entry_revision=status.state_entry_revision,
            selection_revision=revision,
            selected_at=selected_at,
            rationale=rationale,
            superseded_intent=(current.intent_id if current is not None else None),
        )
        return self._publish(intent)

    def consume(self, intent: TransitionIntent) -> TransitionIntent:
        latest = self.latest(intent.mission_id)
        if (
            latest is None
            or not latest.is_selected
            or latest.intent_id != intent.intent_id
        ):
            raise ValueError("Transition Intent is not the current selection")
        return self._publish(
            replace(latest, status=TransitionIntentStatus.CONSUMED)
        )

    def invalidate_latest(self, mission_id: str) -> TransitionIntent | None:
        latest = self.latest(mission_id)
        if latest is None or not latest.is_selected:
            return latest
        return self._publish(
            replace(latest, status=TransitionIntentStatus.INVALIDATED)
        )

    @staticmethod
    def exact_candidate(status: FSMStatus, target_state: str) -> TransitionCandidate:
        candidates = [
            item
            for item in status.transition_candidates
            if item.source == status.active_state and item.target == target_state
        ]
        if len(candidates) != 1:
            raise ValueError("target state is not an exact current transition candidate")
        return candidates[0]

    @staticmethod
    def focused_context(
        status: FSMStatus, intent: TransitionIntent | None
    ) -> ManeuverFSMContext:
        return ManeuverFSMContext(
            current_state=status.active_state,
            current_state_context=status.active_state_context,
            transition_candidates=tuple(
                ManeuverTransitionCandidate(
                    target_state=item.target,
                    condition=item.transition_context,
                )
                for item in status.transition_candidates
            ),
            state_entry_revision=status.state_entry_revision,
            transition_intent=intent,
        )

    @staticmethod
    def _matches_status(intent: TransitionIntent, status: FSMStatus) -> bool:
        if (
            intent.mission_id != status.mission_id
            or intent.plan_revision != status.plan_revision
            or intent.statechart_revision != status.statechart_revision
            or intent.source_state != status.active_state
            or intent.state_entry_revision != status.state_entry_revision
        ):
            return False
        try:
            candidate = TransitionIntentJournal.exact_candidate(
                status, intent.target_state
            )
        except ValueError:
            return False
        return candidate.transition_context == intent.condition

    def _publish(self, intent: TransitionIntent) -> TransitionIntent:
        sequence = self.transport.next_event_sequence(
            self.topic, intent.mission_id
        )
        self.transport.publish_event(
            self.topic,
            TransportEvent(
                schema_version=1,
                event_id=f"transition-intent:{intent.mission_id}:{sequence}",
                mission_id=intent.mission_id,
                sequence=sequence,
                event_kind="transition-intent",
                payload=intent.to_dict(),
            ),
        )
        return intent


__all__ = ["TransitionIntentJournal"]
