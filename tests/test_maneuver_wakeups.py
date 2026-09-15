from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.application.maneuver_wakeups import ManeuverWakeups
from onr.application.transition_intents import TransitionIntentJournal
from onr.contracts.fsm import Statechart, StatechartTransition


def context(*, pursuit=False, end=12.5):
    transport = InProcessTransport()
    runner = FSMRunner(transport, store=InMemoryFSMStateStore())
    status = asyncio.run(
        runner.activate(
            Statechart(
                mission_id="wake-test",
                plan_revision=1,
                mission_snapshot_id="snapshot",
                planning_profile="temporal",
                entry_state="observing",
                states=("observing", "done"),
                terminal_states=("done",),
                state_context={
                    "observing": {
                        "surveillance_mode": "pursue_ship" if pursuit else "fixed_view",
                        "target_entity_id": 23 if pursuit else None,
                        "target_report_ids": ["r1"],
                        "observation_window": {
                            "start": {"seconds": end - 0.5},
                            "duration": {"seconds": 0.5},
                        },
                    },
                    "done": {},
                },
                transitions=(
                    StatechartTransition(
                        "finish",
                        "observing",
                        "done",
                        {
                            "readiness": {"not_before": {"seconds": end}},
                        },
                    ),
                ),
            )
        )
    )
    intent = TransitionIntentJournal(transport).select(
        status, "done", "Assess at gate", selected_at=0
    )
    return status, intent


def test_deadlines_are_coalesced_once_without_authorizing_transition():
    status, intent = context()
    wakeups = ManeuverWakeups()
    assert wakeups.due(status, intent, {}, 0) == ()
    assert wakeups.due(status, intent, {}, 12) == ("deadline:observation-start:12",)
    assert wakeups.due(status, intent, {}, 12.5) == (
        "deadline:intent-not-before:12.5",
        "deadline:observation-end:12.5",
    )
    assert wakeups.due(status, intent, {}, 13) == ()
    assert status.active_state == "observing"  # Scheduling is not condition assessment.


def test_skipped_ticks_still_deliver_due_boundaries_once():
    status, intent = context()
    wakeups = ManeuverWakeups()
    wakeups.due(status, intent, {}, 0)
    assert len(wakeups.due(status, intent, {}, 15)) == 3
    assert wakeups.due(status, intent, {}, 16) == ()


def test_replacement_revision_does_not_suppress_same_time_boundary():
    status, intent = context()
    wakeups = ManeuverWakeups()
    assert len(wakeups.due(status, intent, {}, 13)) == 3
    status = replace(status, plan_revision=2, statechart_revision=2)
    assert len(wakeups.due(status, None, {}, 13)) == 2


def test_active_deadline_wakes_but_completed_navigation_does_not():
    status, intent = context()
    wakeups = ManeuverWakeups()
    lifecycle = {
        "lifecycle": "active",
        "command_id": "nav",
        "parameters": {"deadline_time": 5.5},
    }
    environment = {"maneuver_lifecycle": lifecycle}
    assert wakeups.due(status, intent, environment, 5) == ()
    assert wakeups.due(status, intent, environment, 5.5) == (
        "deadline:maneuver:nav:5.5",
    )
    assert wakeups.due(status, intent, environment, 6) == ()
    lifecycle.update(lifecycle="completed", command_id="nav-2")
    assert wakeups.due(status, intent, environment, 6.5) == ()


def test_only_relevant_report_checks_trigger_and_repeated_payloads_are_quiet():
    status, intent = context()
    wakeups = ManeuverWakeups()
    world = {"event_report_checks": []}
    environment = {"world_model_info": world}
    assert wakeups.due(status, intent, environment, 0) == ()
    world["event_report_checks"].append({"report_id": "unrelated"})
    assert wakeups.due(status, intent, environment, 1) == ()
    world["event_report_checks"].append({"report_id": "r1"})
    assert wakeups.due(status, intent, environment, 2) == (
        "evidence:target-report-checks",
    )
    assert wakeups.due(status, intent, environment, 3) == ()


def test_pursuit_sighting_loss_and_new_target_gps_wake():
    status, intent = context(pursuit=True)
    wakeups = ManeuverWakeups()
    world = {"visible_ship_ids": [], "public_position_fixes": []}
    environment = {"world_model_info": world}
    assert wakeups.due(status, intent, environment, 0) == ()
    world["visible_ship_ids"] = [12]
    world["public_position_fixes"] = [{"entity_id": 12, "sampled_at_s": 1}]
    assert wakeups.due(status, intent, environment, 1) == ()
    world["visible_ship_ids"] = [23]
    world["public_position_fixes"] = [{"entity_id": 23, "sampled_at_s": 2}]
    assert wakeups.due(status, intent, environment, 2) == (
        "evidence:target-visibility",
        "evidence:target-gps",
    )
    assert wakeups.due(status, intent, environment, 3) == ()
    world["visible_ship_ids"] = []
    assert wakeups.due(status, intent, environment, 4) == (
        "evidence:target-visibility",
    )


def test_fixed_view_ignores_gps_and_visibility_updates():
    status, intent = context()
    wakeups = ManeuverWakeups()
    wakeups.due(status, intent, {}, 0)
    assert (
        wakeups.due(
            status,
            intent,
            {
                "world_model_info": {
                    "visible_ship_ids": [23],
                    "public_position_fixes": [{"entity_id": 23, "sampled_at_s": 1}],
                }
            },
            1,
        )
        == ()
    )


def test_searching_pursuit_folds_visibility_but_keeps_gps_checks_and_deadlines():
    status, intent = context(pursuit=True)
    wakeups = ManeuverWakeups()
    world = {
        "visible_ship_ids": [],
        "public_position_fixes": [],
        "gps_interval_seconds": 5,
        "next_gps_update_time_s": 5,
        "event_report_checks": [],
    }
    environment = {
        "world_model_info": world,
        "maneuver_lifecycle": {
            "action": "pursue",
            "lifecycle": "active",
            "phase": "search",
            "parameters": {"entity_id": 23},
            "start_time": 0,
        },
    }
    assert wakeups.due(status, intent, environment, 0) == ()
    for tick in range(1, 5):
        world["visible_ship_ids"] = [23] if tick % 2 else []
        assert wakeups.due(status, intent, environment, tick) == ()
    # A prolonged search still has its original GPS deadline; visibility
    # flicker must not slide that bound or suppress fresh recovery evidence.
    assert wakeups.due(status, intent, environment, 5) == (
        "deadline:acquisition-gps:5",
    )
    world["next_gps_update_time_s"] = 10
    world["public_position_fixes"] = [{"entity_id": 23, "sampled_at_s": 5}]
    assert wakeups.due(status, intent, environment, 5.5) == ("evidence:target-gps",)
    world["event_report_checks"] = [{"report_id": "r1"}]
    assert wakeups.due(status, intent, environment, 12) == (
        "deadline:observation-start:12",
        "evidence:target-report-checks",
    )
    assert wakeups.due(status, intent, environment, 12.5) == (
        "deadline:intent-not-before:12.5",
        "deadline:observation-end:12.5",
    )


@pytest.mark.parametrize("phase, visible", [("pursuit", []), ("search", [23])])
def test_tracking_or_visible_pursuit_folds_redundant_gps_wakeups(phase, visible):
    status, intent = context(pursuit=True, end=100)
    wakeups = ManeuverWakeups()
    world = {
        "visible_ship_ids": visible,
        "public_position_fixes": [],
        "gps_interval_seconds": 5,
        "next_gps_update_time_s": 5,
        "event_report_checks": [],
    }
    environment = {
        "world_model_info": world,
        "maneuver_lifecycle": {
            "action": "pursue",
            "lifecycle": "active",
            "phase": phase,
            "parameters": {"entity_id": 23},
            "start_time": 0,
        },
    }

    assert wakeups.due(status, intent, environment, 0) == ()
    assert wakeups.due(status, intent, environment, 5) == ()
    world["next_gps_update_time_s"] = 10
    world["public_position_fixes"] = [{"entity_id": 23, "sampled_at_s": 5}]
    assert wakeups.due(status, intent, environment, 5.5) == ()

    world["visible_ship_ids"] = []
    environment["maneuver_lifecycle"]["phase"] = "search"
    assert wakeups.due(status, intent, environment, 6) == (
        "deadline:acquisition-gps:5",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"lifecycle": "accepted"},
        {"action": "navigate"},
        {"parameters": {"entity_id": 99}},
    ],
)
def test_unestablished_or_other_target_pursuit_still_wakes_on_visibility(changes):
    status, intent = context(pursuit=True)
    wakeups = ManeuverWakeups()
    world = {"visible_ship_ids": []}
    environment = {
        "world_model_info": world,
        "maneuver_lifecycle": {
            "action": "pursue",
            "lifecycle": "active",
            "parameters": {"entity_id": 23},
            **changes,
        },
    }
    assert wakeups.due(status, intent, environment, 0) == ()
    world["visible_ship_ids"] = [23]
    assert wakeups.due(status, intent, environment, 1) == (
        "evidence:target-visibility",
    )


@pytest.mark.parametrize("next_gps", [17, 24, 31])
def test_search_bound_uses_configured_gps_phase_and_attempt_not_current_time(next_gps):
    status, intent = context(pursuit=True, end=100)
    wakeups = ManeuverWakeups()
    environment = {
        "maneuver_lifecycle": {
            "action": "pursue",
            "lifecycle": "active",
            "phase": "search",
            "parameters": {"entity_id": 23},
            "start_time": 8,
        },
        "world_model_info": {
            "visible_ship_ids": [],
            "gps_interval_seconds": 7,
            "next_gps_update_time_s": next_gps,
        },
    }
    assert wakeups.due(status, intent, environment, 9.5) == ()
    assert wakeups.due(status, intent, environment, 10) == (
        "deadline:acquisition-gps:10",
    )
    assert wakeups.due(status, intent, environment, 16) == ()


def test_unsupported_symbolic_timing_is_not_interpreted():
    status, intent = context()
    status = replace(status, active_state_context={})
    intent = replace(
        intent, condition={"readiness": {"not_before": "after inspection"}}
    )
    assert ManeuverWakeups().due(status, intent, {}, 50) == ()
