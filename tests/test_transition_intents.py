from __future__ import annotations

import asyncio
from typing import Any, cast

from onr.adapters.file_transport import FileTransport
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.application.transition_intents import TransitionIntentJournal
from onr.contracts.fsm import Statechart, StatechartTransition


def _chart(revision: int = 1) -> Statechart:
    return Statechart(
        mission_id="mission-intent",
        plan_revision=revision,
        mission_snapshot_id=f"mission-intent:snapshot:{revision}",
        planning_profile="temporal",
        entry_state="assignment-2-active",
        states=("assignment-2-active", "assignment-2-done", "assignment-3-active"),
        terminal_states=("assignment-2-done", "assignment-3-active"),
        state_context={
            "assignment-2-active": {"assignment": 2},
            "assignment-2-done": {"result": "complete"},
            "assignment-3-active": {
                "maneuver_id": "patrol-action-417",
                "operational_location": {"x": 417, "y": -31},
            },
        },
        transitions=(
            StatechartTransition(
                "finish-assignment-2",
                "assignment-2-active",
                "assignment-2-done",
                {"evidence": {"expected_report_count": 3}},
            ),
            StatechartTransition(
                "override-with-assignment-3",
                "assignment-2-active",
                "assignment-3-active",
                {"evidence": {"route_override": True}},
            ),
        ),
    )


def test_selection_is_exact_idempotent_superseding_and_reconstructable(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "transport"
    transport = FileTransport(root)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart()))
    journal = TransitionIntentJournal(transport)

    first = journal.select(
        status,
        "assignment-2-done",
        "Finish the current assignment.",
        selected_at=12.5,
    )

    assert first.condition == status.transition_candidates[0].transition_context
    assert first.selected_at == 12.5
    assert asyncio.run(runner.status()).active_state == "assignment-2-active"  # type: ignore[union-attr]
    assert transport.next_event_sequence("transition-intents", first.mission_id) == 1

    retained = journal.select(
        status,
        "assignment-2-done",
        "A changed rationale does not change the selected pair.",
        selected_at=13,
    )
    assert retained == first
    assert transport.next_event_sequence("transition-intents", first.mission_id) == 1

    changed = journal.select(
        status,
        "assignment-3-active",
        "Current evidence warrants the alternate live target.",
        selected_at=14,
    )
    assert changed.superseded_intent == first.intent_id
    assert changed.target_state == "assignment-3-active"
    assert transport.next_event_sequence("transition-intents", first.mission_id) == 2

    restored = TransitionIntentJournal(FileTransport(root))
    assert restored.current(status) == changed


def test_focused_context_hides_future_operational_context_until_transition(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    transport = FileTransport(tmp_path / "transport")
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart()))
    journal = TransitionIntentJournal(transport)

    focused = journal.focused_context(status, None).to_dict()

    assert set(focused["transition_candidates"][0]) == {
        "target_state",
        "condition",
    }
    assert "event" not in str(focused)
    assert "patrol-action-417" not in str(focused)
    assert "operational_location" not in str(focused)
    assert focused["current_state_context"] == {"assignment": 2}


def test_state_and_replan_revision_changes_invalidate_selected_intent(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    transport = FileTransport(tmp_path / "transport")
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    first_status = asyncio.run(runner.activate(_chart(1)))
    journal = TransitionIntentJournal(transport)
    selected = journal.select(
        first_status,
        "assignment-2-done",
        "Select a revision-one target.",
        selected_at=5,
    )

    replacement_status = asyncio.run(runner.activate(_chart(2)))

    assert journal.current(replacement_status, invalidate_stale=True) is None
    invalidated = journal.latest(selected.mission_id)
    assert invalidated is not None
    assert invalidated.intent_id == selected.intent_id
    assert invalidated.status == "invalidated"
