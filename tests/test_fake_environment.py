from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, cast

import pytest

from harness.fake_environment import FakeEnvironment
from onr.adapters.file_transport import FileTransport
from onr.application.context_coordination import ContextCoordination
from onr.contracts.environment import EventObservation, perception_from_dict
from onr.contracts.fsm import ManeuverFeedback
from onr.contracts.maneuver_control import ManeuverCommand
from onr.contracts.planning import ManeuverIntent, ManeuverParameter
from onr.contracts.transport import Command
from onr.demo.environment_updates import (
    CoordinatorDrivenFakeEnvironment,
    EnvironmentDrivenFakeEnvironment,
)
from onr.ports.transport import Subscription


def _report(path: Path, events: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps(events), encoding="utf-8")
    return path


def _event(
    *,
    time: float,
    x: float,
    entity_id: int = 1,
    event_type: str = "intersection decision",
) -> dict[str, object]:
    return {
        "time": time,
        "position": [x, 0.0, -250.0],
        "event information": {"decision": "left"},
        "event type": event_type,
        "entity_id": entity_id,
    }


def _command(
    *,
    command_id: str = "command-1",
    mission_id: str = "mission-1",
    x: float = 10,
    speed: float | None = 2,
    extras: dict[str, object] | None = None,
) -> ManeuverCommand:
    parameters: dict[str, object] = {"x": x, "y": 0.0, "z": -250.0}
    if speed is not None:
        parameters["speed"] = speed
    parameters.update(extras or {})
    return ManeuverCommand(
        command_id=command_id,
        correlation_id=f"correlation:{command_id}",
        mission_id=mission_id,
        plan_revision=1,
        maneuver_id=f"maneuver:{command_id}",
        intent=ManeuverIntent(
            "navigate",
            tuple(ManeuverParameter(name, value) for name, value in parameters.items()),
        ),
    )


def _action_command(
    action: str,
    parameters: dict[str, object],
    *,
    command_id: str,
) -> ManeuverCommand:
    return ManeuverCommand(
        command_id=command_id,
        correlation_id=f"correlation:{command_id}",
        mission_id="mission-1",
        plan_revision=1,
        maneuver_id=f"maneuver:{command_id}",
        intent=ManeuverIntent(
            action,
            tuple(ManeuverParameter(name, value) for name, value in parameters.items()),
        ),
    )


def _transport(tmp_path: Path, mission_id: str = "mission-1") -> FileTransport:
    return FileTransport(
        tmp_path,
        (
            FakeEnvironment.subscription_for(mission_id),
            Subscription("context-coordination", mission_id, "planning-evidence"),
            Subscription("belief-manager", mission_id, "belief-observations"),
            Subscription("feedback-reader", mission_id, "maneuver-feedback"),
            Subscription("perception-reader", mission_id, "environment-perceptions"),
        ),
    )


def test_navigation_moves_one_tick_at_a_time_and_completes_only_at_target(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=20, x=20)])
    environment = FakeEnvironment(
        transport, "mission-1", event_report_path=report, tick_seconds=0.5
    )
    active = environment.submit(_command())

    assert ManeuverFeedback.from_dict(active.feedback.payload).lifecycle == "active"
    assert environment.drone_position == (0.0, 0.0, -250.0)
    for tick_number in range(1, 10):
        tick = environment.tick()
        assert tick.current_time == tick_number * 0.5
        assert environment.drone_position[0] == tick_number
        assert tick.feedback_events == ()
        assert environment.navigation_status == "active"

    final = environment.tick()
    assert environment.drone_position == (10.0, 0.0, -250.0)
    assert len(final.feedback_events) == 1
    assert ManeuverFeedback.from_dict(final.feedback_events[0].payload).lifecycle == (
        "completed"
    )
    assert environment.navigation_status == "completed"


def test_active_cancelled_and_completed_feedback_is_correlated_and_idempotent(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=20, x=20)])
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)
    first = environment.submit(_command(command_id="first", x=20, speed=20))
    replay = environment.submit(_command(command_id="first", x=20, speed=20))
    second = environment.submit(_command(command_id="second", x=1, speed=20))

    assert replay.feedback == first.feedback
    cancelled = environment.last_override_feedback
    assert cancelled is not None
    cancelled_fact = ManeuverFeedback.from_dict(cancelled.payload)
    assert cancelled_fact.lifecycle == "cancelled"
    assert cancelled_fact.maneuver_id == "maneuver:first"
    assert cancelled_fact.payload["reason"] == "overridden"
    assert ManeuverFeedback.from_dict(second.feedback.payload).maneuver_id == (
        "maneuver:second"
    )
    completed = environment.tick().feedback_events
    assert len(completed) == 1
    assert ManeuverFeedback.from_dict(completed[0].payload).maneuver_id == (
        "maneuver:second"
    )


@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("navigate", {"x": 1, "y": 2}),
        ("takeoff", {"altitude": -100}),
        ("land", {"x": 1, "y": 2}),
        (
            "search_area",
            {
                "polygon": [
                    {"x": 0, "y": 0},
                    {"x": 1, "y": 0},
                    {"x": 0, "y": 1},
                ]
            },
        ),
        ("pursue", {"entity_id": "1"}),
        ("investigate", {"entity_id": "1"}),
    ],
)
def test_lifecycle_feedback_correlates_every_physical_action(
    tmp_path: Path, action: str, parameters: dict[str, object]
) -> None:
    transport = _transport(tmp_path / action)
    report = _report(tmp_path / f"{action}.json", [])
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)
    command = _action_command(action, parameters, command_id=action)

    result = environment.process_command(command, lifecycle="completed")
    feedback = ManeuverFeedback.from_dict(result.feedback.payload)

    assert feedback.maneuver_id == command.maneuver_id
    assert feedback.payload["command_id"] == command.command_id
    assert feedback.payload["correlation_id"] == command.correlation_id


def test_navigation_deadline_derives_speed_and_completes_on_arrival(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=5, x=10)])
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)
    environment.submit(
        _command(
            x=10,
            speed=None,
            extras={"deadline_time": 5},
        )
    )

    first = environment.tick()
    assert first.feedback_events == ()
    assert environment.drone_position[0] == 1.0
    assert environment.current_maneuver["effective_speed"] == 2.0  # type: ignore[index]
    for _ in range(8):
        environment.tick()
    arrival = environment.tick()
    assert arrival.current_time == 5.0
    assert environment.drone_position[0] == 10.0
    assert len(arrival.feedback_events) == 1
    assert ManeuverFeedback.from_dict(arrival.feedback_events[0].payload).lifecycle == (
        "completed"
    )
    assert environment.navigation_status == "completed"


def test_navigation_continues_at_maximum_speed_after_infeasible_deadline(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [])
    environment = FakeEnvironment(
        transport,
        "mission-1",
        event_report_path=report,
        tick_seconds=1,
        max_velocity=2,
    )
    environment.submit(_command(x=10, speed=0.5, extras={"deadline_time": 1}))

    first = environment.tick()
    assert first.feedback_events == ()
    assert environment.drone_position[0] == 2
    assert environment.current_maneuver["effective_speed"] == 2  # type: ignore[index]
    for _ in range(3):
        assert environment.tick().feedback_events == ()
    completed = environment.tick()
    assert environment.drone_position[0] == 10
    assert (
        ManeuverFeedback.from_dict(completed.feedback_events[0].payload).lifecycle
        == "completed"
    )


def test_search_area_traverses_and_closes_polygon_at_deadline_speed(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [])
    environment = FakeEnvironment(
        transport,
        "mission-1",
        event_report_path=report,
        tick_seconds=1,
        initial_position=(0, 0, -10),
        max_velocity=20,
    )
    polygon = [
        {"x": 0, "y": 0},
        {"x": 2, "y": 0},
        {"x": 2, "y": 2},
        {"x": 0, "y": 2},
    ]
    environment.submit(
        _action_command(
            "search_area",
            {
                "polygon": polygon,
                "altitude": -5,
                "speed": 0.5,
                "deadline_time": 4,
            },
            command_id="search",
        )
    )

    assert environment.tick().feedback_events == ()
    assert environment.drone_position == (0.0, 0.0, -6.75)
    assert environment.current_maneuver["effective_speed"] == 3.25  # type: ignore[index]
    environment.tick()
    environment.tick()
    completed = environment.tick()
    assert environment.drone_position == (0.0, 0.0, -5.0)
    assert (
        ManeuverFeedback.from_dict(completed.feedback_events[0].payload).maneuver_id
        == "maneuver:search"
    )


def test_search_area_continues_at_maximum_speed_after_missed_deadline(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [])
    environment = FakeEnvironment(
        transport,
        "mission-1",
        event_report_path=report,
        tick_seconds=1,
        initial_position=(0, 0, 0),
        max_velocity=2,
    )
    environment.submit(
        _action_command(
            "search_area",
            {
                "polygon": [
                    {"x": 0, "y": 0},
                    {"x": 4, "y": 0},
                    {"x": 4, "y": 3},
                ],
                "deadline_time": 1,
            },
            command_id="late-search",
        )
    )

    first = environment.tick()
    assert first.feedback_events == ()
    assert environment.drone_position == (2.0, 0.0, 0.0)
    assert environment.current_maneuver["effective_speed"] == 2  # type: ignore[index]
    for _ in range(4):
        assert environment.tick().feedback_events == ()
    completed = environment.tick()
    assert environment.drone_position == (0.0, 0.0, 0.0)
    assert (
        ManeuverFeedback.from_dict(completed.feedback_events[0].payload).lifecycle
        == "completed"
    )


def test_investigate_uses_feasible_deadline_to_reach_standoff(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=100, x=10)])
    environment = FakeEnvironment(
        transport,
        "mission-1",
        event_report_path=report,
        tick_seconds=1,
        initial_position=(0, 0, -250),
        max_velocity=20,
    )
    environment.submit(
        _action_command(
            "investigate",
            {"entity_id": "1", "standoff_distance": 2, "deadline_time": 4},
            command_id="scheduled-investigation",
        )
    )

    first = environment.tick()
    assert first.feedback_events == ()
    assert environment.drone_position == (2.0, 0.0, -250.0)
    assert environment.current_maneuver["effective_speed"] == 2  # type: ignore[index]
    environment.tick()
    environment.tick()
    completed = environment.tick()
    assert environment.drone_position == (8.0, 0.0, -250.0)
    assert (
        ManeuverFeedback.from_dict(completed.feedback_events[0].payload).lifecycle
        == "completed"
    )


def test_investigate_reaches_standoff_and_unknown_entity_fails(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=100, x=10)])
    environment = FakeEnvironment(
        transport,
        "mission-1",
        event_report_path=report,
        tick_seconds=1,
        initial_position=(0, 0, -250),
        max_velocity=2,
    )
    environment.submit(
        _action_command(
            "investigate",
            {"entity_id": "1", "standoff_distance": 2, "deadline_time": 1},
            command_id="investigate",
        )
    )

    first = environment.tick()
    assert first.feedback_events == ()
    assert environment.current_maneuver["effective_speed"] == 2  # type: ignore[index]
    for _ in range(2):
        assert environment.tick().feedback_events == ()
    completed = environment.tick()
    assert environment.drone_position == (8.0, 0.0, -250.0)
    assert (
        ManeuverFeedback.from_dict(completed.feedback_events[0].payload).lifecycle
        == "completed"
    )

    environment.submit(
        _action_command(
            "investigate",
            {"entity_id": "missing"},
            command_id="missing",
        )
    )
    failed = environment.tick()
    fact = ManeuverFeedback.from_dict(failed.feedback_events[0].payload)
    assert fact.lifecycle == "failed"
    assert fact.maneuver_id == "maneuver:missing"
    assert fact.payload["reason"] == "unknown entity"


def test_events_are_sensed_continuously_in_fov_without_updating_belief(
    tmp_path: Path,
) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    report = _report(
        tmp_path / "report.json",
        [_event(time=0.2, x=1), _event(time=1.0, x=100, entity_id=2)],
    )
    environment = FakeEnvironment(
        transport,
        mission_id,
        event_report_path=report,
        tick_seconds=0.5,
        fov_radius=30,
    )
    first = environment.tick()
    assert len(first.perception_events) == 2
    event_payload = next(
        item.payload
        for item in first.perception_events
        if item.event_kind == "event.observed"
    )
    event = perception_from_dict(event_payload)
    assert isinstance(event, EventObservation)
    assert event.source_event_index == 1
    assert event.event_time == 0.2
    assert event.observed_time == 0.5
    assert perception_from_dict(event.to_dict()) == event
    assert "maneuver_id" not in event.to_dict()
    assert "observation_window_outcome" not in event.to_dict()
    assert math.isfinite(event.uncertainty_score)

    assert (
        transport.latest_event(
            "belief-observations", mission_id, event_kind="risk.observed"
        )
        is None
    )

    second = environment.tick()
    assert second.perception_events == ()
    assert environment.current_environment_data()["perceptions"] == []
    assert transport.next_event_sequence("belief-observations", mission_id) == 0


def test_uncertainty_is_deterministic_across_environment_replay(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path / "report.json", [_event(time=0.5, x=0)])
    scores = []
    for run in ("first", "second"):
        environment = FakeEnvironment(
            _transport(tmp_path / run),
            "mission-1",
            event_report_path=report,
        )
        payload = next(
            item.payload
            for item in environment.tick().perception_events
            if item.event_kind == "event.observed"
        )
        observation = perception_from_dict(payload)
        assert isinstance(observation, EventObservation)
        scores.append(observation.uncertainty_score)
    assert scores[0] == scores[1]


def test_event_outside_fov_is_missed_and_not_emitted_later(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=0.5, x=100)])
    environment = FakeEnvironment(
        transport, "mission-1", event_report_path=report, fov_radius=30
    )

    result = environment.tick()
    assert result.perception_events == ()
    assert environment.tick().perception_events == ()
    assert transport.next_event_sequence("belief-observations", "mission-1") == 0


def test_report_entities_persist_and_move_without_becoming_perceptions(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(
        tmp_path / "report.json",
        [_event(time=0.5, x=10), _event(time=1.0, x=20)],
    )
    environment = FakeEnvironment(
        transport, "mission-1", event_report_path=report, fov_radius=1
    )

    assert environment.tick().perception_events == ()
    assert environment.tick().perception_events == ()
    entity = next(
        item
        for item in environment.planning_environment_data()["scene_graph"]["entities"]
        if item["id"] == "1"
    )
    assert entity["location"] == {"x": 20.0, "y": 0.0, "z": -250.0}


def test_current_context_is_latest_only_while_planning_view_keeps_full_report(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    events = [_event(time=0.5, x=0), _event(time=1.0, x=0)]
    report = _report(tmp_path / "report.json", events)
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)
    heartbeat = environment.heartbeat()

    static_info = heartbeat.environment_event.payload["static_info"]
    assert len(static_info) == 2
    assert static_info[0]["time"] == 0.5
    assert static_info[1]["time"] == 1.0
    assert len(heartbeat.environment_event.payload["scene_graph"]["entities"]) == 2
    assert "static_info" not in environment.current_environment_data()

    environment.tick()
    first_context = environment.current_environment_data()
    environment.tick()
    second_context = environment.current_environment_data()
    assert len(cast(list[object], first_context["perceptions"])) == 2
    assert len(cast(list[object], second_context["perceptions"])) == 2
    first_ids = {
        item["observation_id"]
        for item in cast(list[dict[str, Any]], first_context["perceptions"])
    }
    second_ids = {
        item["observation_id"]
        for item in cast(list[dict[str, Any]], second_context["perceptions"])
    }
    assert first_ids.isdisjoint(second_ids)
    assert len(environment.planning_environment_data()["static_info"]) == 2


def test_file_command_is_consumed_and_context_fact_is_drainable(tmp_path: Path) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    report = _report(tmp_path / "report.json", [_event(time=5, x=5)])
    command = _command(mission_id=mission_id)
    transport.send_command(command.to_command("maneuver-adapter"))
    environment = FakeEnvironment(
        transport,
        mission_id,
        event_report_path=report,
        context_topic="planning-evidence",
    )
    result = environment.run_once()
    assert result is not None and result.command == command
    assert result.risk_observation is None

    coordination = ContextCoordination(
        transport, mission_id, input_topic="planning-evidence"
    )
    with transport.open_consumer(coordination.subscription) as consumer:
        snapshot = coordination.drain_to_latest(consumer)
    assert snapshot is not None
    assert snapshot.environment_data == result.environment_event.event_id


def test_environment_never_writes_bayesian_observations(
    tmp_path: Path,
) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    report = _report(tmp_path / "report.json", [_event(time=0.5, x=0)])
    environment = FakeEnvironment(
        transport,
        mission_id,
        event_report_path=report,
        context_topic="planning-evidence",
    )
    tick = environment.tick()
    assert len(tick.perception_events) == 2
    assert transport.next_event_sequence("belief-observations", mission_id) == 0


def test_completed_lifecycle_replay_reuses_transport_identity(tmp_path: Path) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    report = _report(tmp_path / "report.json", [_event(time=5, x=5)])
    command = _command(mission_id=mission_id)
    transport.send_command(command.to_command("maneuver-adapter"))
    first_environment = FakeEnvironment(transport, mission_id, event_report_path=report)
    with transport.open_consumer(first_environment.subscription) as consumer:
        delivery = consumer.receive()
        assert delivery is not None
        first = first_environment.process_command(
            ManeuverCommand.from_command(cast(Command, delivery.message)),
            lifecycle="completed",
        )
    restarted = FakeEnvironment(transport, mission_id, event_report_path=report)
    second = restarted.run_once(lifecycle="completed")
    assert second is not None
    assert second.feedback == first.feedback
    assert second.environment_event == first.environment_event
    assert second.source_fact == first.source_fact


def test_environment_consumer_can_start_after_command_enqueue_and_applies_once(
    tmp_path: Path,
) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    report = _report(tmp_path / "report.json", [_event(time=50, x=50)])
    command = _command(mission_id=mission_id, x=100, speed=1)
    receipt = transport.send_command(command.to_command("maneuver-adapter"))
    assert receipt.command_id == command.command_id
    assert transport.get_command_outcome(command.command_id) is None

    environment = FakeEnvironment(transport, mission_id, event_report_path=report)
    updates = CoordinatorDrivenFakeEnvironment(environment, cadence_seconds=0.5)
    first = updates.advance()
    second = updates.advance()
    updates.stop()

    feedback_ids = [
        event.event_id
        for update in (first, second)
        for event in update.feedback_events
        if event.event_id.endswith(":active")
    ]
    assert feedback_ids == [f"maneuver-feedback:{command.command_id}:active"]
    assert environment.active_command == command
    assert transport.get_cursor(environment.subscription)["command"] == 0


def test_command_application_failure_retries_to_dead_letter_asynchronously(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    report = _report(tmp_path / "report.json", [_event(time=50, x=50)])
    command = _command(mission_id=mission_id)
    transport.send_command(command.to_command("maneuver-adapter"))
    environment = FakeEnvironment(transport, mission_id, event_report_path=report)
    attempts = 0

    def fail(_: ManeuverCommand) -> object:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("injected command application failure")

    monkeypatch.setattr(environment, "apply_command", fail)
    updates = CoordinatorDrivenFakeEnvironment(environment, cadence_seconds=0.5)
    tick = updates.advance()
    updates.stop()

    assert tick.current_time == 0.5
    assert attempts == environment.subscription.max_retries
    dead_letters = transport.get_dead_letters(environment.subscription)
    assert len(dead_letters) == 1
    assert dead_letters[0]["identity"] == command.command_id


def test_environment_driven_source_advances_and_stops_at_limit(tmp_path: Path) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=50, x=50)])
    environment = FakeEnvironment(
        transport, "mission-1", event_report_path=report, tick_seconds=0.5
    )
    updates = EnvironmentDrivenFakeEnvironment(environment, cadence_seconds=0.01)
    updates.start(simulation_limit_seconds=1.0)
    deadline = time.monotonic() + 1
    while updates.is_alive and time.monotonic() < deadline:
        time.sleep(0.005)
    updates.join()

    assert not updates.is_alive
    assert updates.current_time == 1.0
    assert [item.current_time for item in updates.drain_updates()] == [0.5, 1.0]
    updates.raise_if_failed()


def test_environment_driven_producer_error_propagates_and_thread_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=50, x=50)])
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)

    def fail_tick() -> object:
        raise RuntimeError("injected tick failure")

    monkeypatch.setattr(environment, "tick", fail_tick)
    updates = EnvironmentDrivenFakeEnvironment(environment, cadence_seconds=0.01)
    updates.start()
    deadline = time.monotonic() + 1
    while updates.is_alive and time.monotonic() < deadline:
        time.sleep(0.005)
    updates.join()

    assert not updates.is_alive
    with pytest.raises(RuntimeError, match="producer failed"):
        updates.raise_if_failed()
