from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast

from harness.fake_environment import FakeEnvironment
from onr.adapters.file_transport import FileTransport
from onr.application.context_coordination import ContextCoordination
from onr.contracts.environment import EventObservation, perception_from_dict
from onr.contracts.fsm import ManeuverFeedback
from onr.contracts.maneuver_control import ManeuverCommand
from onr.contracts.planning import ManeuverIntent, ManeuverParameter
from onr.contracts.transport import Command
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
    extras: dict[str, int | float] | None = None,
) -> ManeuverCommand:
    parameters: dict[str, int | float] = {"x": x, "y": 0.0, "z": -250.0}
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


def test_navigation_deadline_derives_speed_and_emits_arrival_phase_feedback(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=5, x=10)])
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)
    environment.submit(
        _command(
            x=10,
            speed=None,
            extras={
                "deadline_time": 5,
                "observation_start": 5,
                "observation_duration": 1,
            },
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
    phase = ManeuverFeedback.from_dict(arrival.feedback_events[0].payload)
    assert phase.lifecycle == "active"
    assert phase.payload["phase"] == "navigation-complete"
    assert environment.navigation_status == "navigation-complete"

    completed = environment.tick()
    assert completed.current_time == 5.5
    assert completed.feedback_events == ()
    completed = environment.tick()
    assert completed.current_time == 6.0
    assert ManeuverFeedback.from_dict(
        completed.feedback_events[0].payload
    ).lifecycle == ("completed")


def test_events_are_sensed_only_in_window_and_fov_and_map_exactly_to_belief(
    tmp_path: Path,
) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    report = _report(
        tmp_path / "report.json",
        [_event(time=0.5, x=1), _event(time=1.0, x=100, entity_id=2)],
    )
    environment = FakeEnvironment(
        transport,
        mission_id,
        event_report_path=report,
        tick_seconds=0.5,
        fov_radius=30,
    )
    environment.submit(
        _command(
            x=1,
            speed=20,
            extras={
                "observation_start": 0,
                "observation_duration": 1.5,
                "source_event_index": 1,
                "expected_observation_count": 1,
            },
        )
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
    assert event.maneuver_id == "maneuver:command-1"
    assert event.observation_window_outcome == "observed"
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


def test_out_of_window_event_produces_no_perception_or_belief_input(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(tmp_path / "report.json", [_event(time=0.5, x=0)])
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)
    environment.submit(
        _command(
            x=0,
            speed=20,
            extras={"observation_start": 1, "observation_duration": 1},
        )
    )

    result = environment.tick()
    assert result.perception_events == ()
    assert transport.next_event_sequence("belief-observations", "mission-1") == 0


def test_report_entities_persist_and_move_without_becoming_perceptions(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path / "transport")
    report = _report(
        tmp_path / "report.json",
        [_event(time=0.5, x=10), _event(time=1.0, x=20)],
    )
    environment = FakeEnvironment(transport, "mission-1", event_report_path=report)

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

    environment.submit(
        _command(
            x=0,
            speed=20,
            extras={"observation_start": 0, "observation_duration": 2},
        )
    )
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
    environment.submit(
        _command(
            x=0,
            speed=20,
            extras={"observation_start": 0, "observation_duration": 1},
        )
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
