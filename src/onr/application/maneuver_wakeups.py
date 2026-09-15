"""Wake Maneuver on explicit timing/evidence changes, never assess conditions.

Only current-state, selected-intent and public environment data are inspected.
The agent still decides whether a transition or physical action is warranted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from onr.contracts.fsm import FSMStatus
from onr.contracts.transition_intent import TransitionIntent


def _object(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _seconds(value: object) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


class ManeuverWakeups:
    """Coalesce due boundaries and changed evidence once per selected context."""

    def __init__(self) -> None:
        self._context: tuple | None = None
        self._fired: set[tuple[str, float]] = set()
        self._signals: dict[str, object] = {}

    def due(
        self,
        status: FSMStatus,
        intent: TransitionIntent | None,
        environment: Mapping[str, object],
        now: float,
    ) -> tuple[str, ...]:
        context = (
            status.plan_revision,
            status.statechart_revision,
            status.state_entry_revision,
            status.active_state,
            intent.intent_id if intent is not None else None,
        )
        if context != self._context:
            self._context = context
            self._fired.clear()
            self._signals.clear()
        result = []

        def boundary(label: str, value: object) -> None:
            due = _seconds(value)
            if due is None or due > now + 1e-9 or (label, due) in self._fired:
                return
            self._fired.add((label, due))
            result.append(f"deadline:{label}:{due:g}")

        def changed(label: str, value: object) -> None:
            if label in self._signals and value != self._signals[label]:
                result.append(f"evidence:{label}")
            self._signals[label] = value

        current = status.active_state_context
        if intent is not None:
            readiness = _object(intent.condition.get("readiness"))
            boundary("intent-not-before", readiness.get("not_before"))
        window = _object(current.get("observation_window"))
        start = _seconds(window.get("start"))
        duration = _seconds(window.get("duration"))
        boundary("observation-start", start)
        if start is not None and duration is not None:
            boundary("observation-end", start + duration)

        lifecycle = _object(environment.get("maneuver_lifecycle"))
        if lifecycle.get("lifecycle") == "active":
            parameters = _object(lifecycle.get("parameters"))
            identity = lifecycle.get(
                "command_id", lifecycle.get("maneuver_id", "active")
            )
            boundary(f"maneuver:{identity}", parameters.get("deadline_time"))

        world = _object(environment.get("world_model_info"))
        report_ids = current.get("target_report_ids", ())
        checks = world.get("event_report_checks", ())
        changed(
            "target-report-checks",
            frozenset(
                check.get("report_id")
                for check in checks
                if isinstance(check, Mapping) and check.get("report_id") in report_ids
            ),
        )
        target = current.get("target_entity_id")
        if current.get("surveillance_mode") == "pursue_ship" and target is not None:
            visible = target in world.get("visible_ship_ids", ())
            active_target_pursuit = (
                lifecycle.get("action") == "pursue"
                and lifecycle.get("lifecycle") == "active"
                and _object(lifecycle.get("parameters")).get("entity_id") == target
            )
            needs_recovery = (
                active_target_pursuit
                and lifecycle.get("phase") == "search"
                and not visible
            )
            if active_target_pursuit:
                # Tracking/search already belongs to the running controller.
                # Keep fresh visibility, but do not deliberate on every blink.
                self._signals["target-visibility"] = visible
            else:
                changed("target-visibility", visible)
            fixes = world.get("public_position_fixes", ())
            target_gps = tuple(
                fix.get("sampled_at_s")
                for fix in fixes
                if isinstance(fix, Mapping) and fix.get("entity_id") == target
            )
            if active_target_pursuit and not needs_recovery:
                self._signals["target-gps"] = target_gps
            else:
                changed("target-gps", target_gps)
            outcome = _object(current.get("desired_outcome"))
            rendezvous = _object(outcome.get("acquisition_rendezvous"))
            boundary("acquisition-arrival", rendezvous.get("arrival_deadline"))
            # Keep the GPS search bound anchored to the attempt, even after the
            # provider advances next_gps_update_time_s to the following broadcast.
            attempt = _seconds(lifecycle.get("start_time"))
            interval = _seconds(world.get("gps_interval_seconds"))
            next_gps = _seconds(world.get("next_gps_update_time_s"))
            if (
                needs_recovery
                and attempt is not None
                and interval
                and next_gps is not None
            ):
                cycles = max(0, math.ceil((next_gps - attempt) / interval) - 1)
                boundary("acquisition-gps", next_gps - cycles * interval)
        return tuple(result)
