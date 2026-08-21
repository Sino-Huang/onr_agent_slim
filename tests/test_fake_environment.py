from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from harness.fake_environment import FakeEnvironment
from onr.adapters.file_transport import FileTransport
from onr.application.context_coordination import ContextCoordination
from onr.application.maneuver_control import ManeuverControl
from onr.contracts.bayesian_belief import RiskObservation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, ManeuverFeedback, TransitionCandidate
from onr.contracts.maneuver_control import ManeuverCommand, ManeuverControlDecision
from onr.contracts.planning import ManeuverIntent, ManeuverParameter
from onr.contracts.transport import Command, TransportEvent
from onr.ports.transport import Subscription

_EVENT_REPORT_PATH = (
    Path(__file__).parents[1]
    / "data/ships_report_and_trajectory_example/ships/events_report.json"
)


class FixedDecisionProvider:
    def __init__(self, decision: ManeuverControlDecision) -> None:
        self.decision = decision

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: object = None,
    ) -> ManeuverControlDecision:
        return self.decision


class StubAdapter:
    def submit(self, command: ManeuverCommand) -> object:
        return None


def _command(command_id: str = "command-1", mission_id: str = "mission-1") -> ManeuverCommand:
    return ManeuverCommand(
        command_id=command_id,
        correlation_id=f"decision-{command_id}",
        mission_id=mission_id,
        plan_revision=3,
        maneuver_id="survey",
        intent=ManeuverIntent("navigate", (ManeuverParameter("speed", 2),)),
    )


def _transport(tmp_path: Path, mission_id: str = "mission-1") -> FileTransport:
    return FileTransport(
        tmp_path,
        (
            FakeEnvironment.subscription_for(mission_id),
            Subscription("scene-reader", mission_id, "environment-data"),
            Subscription("context-coordination", mission_id, "normalized-plans"),
            Subscription("feedback-reader", mission_id, "maneuver-feedback"),
            Subscription("belief-reader", mission_id, "belief-observations"),
        ),
    )


def test_consumes_file_command_and_exposes_environment_data_and_source_fact(tmp_path: Path) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path, mission_id)
    command = _command(mission_id=mission_id)
    transport.send_command(command.to_command("maneuver-adapter"))

    result = FakeEnvironment(transport, mission_id).run_once()

    assert result is not None
    assert result.command == command
    assert result.environment_event.event_kind == "environment_data"
    assert set(result.environment_event.payload) == {"scene_graph", "static_info"}
    assert result.source_fact.event_kind == "source-fact"
    assert result.source_fact.payload["source"] == "environment_data"
    assert result.source_fact.payload["reference"] == result.environment_event.event_id
    assert result.risk_observation.event_kind == "risk.observed"
    observation = RiskObservation.from_dict(result.risk_observation.payload)
    assert observation.risk_type == "collision"
    assert {item.entity_id for item in observation.associations} == {
        "ship-1",
        "ship-2",
        "ship-3",
    }
    assert result.environment_file == tmp_path.parent / "environment" / mission_id / "environment.json"
    environment = cast(
        dict[str, Any], json.loads(result.environment_file.read_text(encoding="utf-8"))
    )
    assert environment == result.environment_event.to_dict()["payload"]
    assert environment["static_info"] == json.loads(
        _EVENT_REPORT_PATH.read_text(encoding="utf-8")
    )
    graph = cast(dict[str, Any], environment["scene_graph"])
    entities = cast(list[dict[str, Any]], graph["entities"])
    assert len(entities) == 6
    assert sum(entity["type"] == "ship" for entity in entities) == 5
    assert sum(entity["type"] == "drone" for entity in entities) == 1
    assert {entity["area"] for entity in entities} == {"windmill area", "dock"}
    ships = [entity for entity in entities if entity["type"] == "ship"]
    assert all(
        isinstance(entity["risk"], float) and 0.0 <= entity["risk"] <= 1.0
        for entity in ships
    )
    drone = next(entity for entity in entities if entity["type"] == "drone")
    assert "risk" not in drone
    assert drone["max_velocity"] == 20
    assert drone["fov_radius"] == 30
    assert all(
        set(entity["location"]) == {"x", "y", "z"}
        and all(
            isinstance(entity["location"][axis], (int, float))
            for axis in ("x", "y", "z")
        )
        for entity in entities
    )
    assert list(graph["entities"]) == entities

    with transport.open_consumer(Subscription("scene-reader", mission_id, "environment-data")) as scene:
        scene_delivery = scene.receive()
        assert scene_delivery is not None
        scene_event = cast(TransportEvent, scene_delivery.message)
        assert TransportEvent.from_json(scene_event.to_canonical_json()) == result.environment_event
        scene_delivery.ack()
    with transport.open_consumer(Subscription("context-coordination", mission_id, "normalized-plans")) as context:
        source_delivery = context.receive()
        assert source_delivery is not None
        source_event = cast(TransportEvent, source_delivery.message)
        assert source_event == result.source_fact
        source_delivery.ack()


def test_environment_heartbeat_publishes_data_before_any_maneuver(
    tmp_path: Path,
) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path, mission_id)
    environment = FakeEnvironment(transport, mission_id)

    heartbeat = environment.heartbeat()

    assert heartbeat.environment_event.event_kind == "environment_data"
    graph = cast(dict[str, Any], heartbeat.environment_event.payload["scene_graph"])
    assert graph["mission_id"] == mission_id
    assert graph["plan_revision"] == 0
    assert graph["maneuvers"] == ()
    entities = cast(list[dict[str, Any]], graph["entities"])
    assert len(entities) == 6
    assert all(
        isinstance(entity["risk"], float) and 0.0 <= entity["risk"] <= 1.0
        for entity in entities
        if entity["type"] == "ship"
    )
    heartbeat_payload = cast(
        dict[str, Any], heartbeat.environment_event.to_dict()["payload"]
    )
    drone = next(entity for entity in entities if entity["type"] == "drone")
    assert drone["max_velocity"] == 20
    assert drone["fov_radius"] == 30
    assert heartbeat_payload["static_info"] == json.loads(
        _EVENT_REPORT_PATH.read_text(encoding="utf-8")
    )
    assert heartbeat.source_fact.payload["reference"] == heartbeat.environment_event.event_id

    coordination = ContextCoordination(transport, mission_id)
    with transport.open_consumer(coordination.subscription) as consumer:
        snapshot = coordination.run_once(consumer)

    assert snapshot is not None
    assert snapshot.environment_data == heartbeat.environment_event.event_id
    assert snapshot.source_revisions["environment_data"] == 0
    assert environment.run_once() is None


def test_static_info_content_participates_in_environment_identity(
    tmp_path: Path,
) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path / "transport", mission_id)
    output_root = tmp_path / "environment"
    first_report = tmp_path / "first-report.json"
    second_report = tmp_path / "second-report.json"
    first_report.write_text('[{"event":"first"}]\n', encoding="utf-8")
    second_report.write_text('[{"event":"second"}]\n', encoding="utf-8")

    first = FakeEnvironment(
        transport,
        mission_id,
        output_root=output_root,
        event_report_path=first_report,
    ).heartbeat()
    second = FakeEnvironment(
        transport,
        mission_id,
        output_root=output_root,
        event_report_path=second_report,
    ).heartbeat()

    first_payload = cast(dict[str, Any], first.environment_event.to_dict()["payload"])
    second_payload = cast(dict[str, Any], second.environment_event.to_dict()["payload"])
    assert first_payload["static_info"] == [{"event": "first"}]
    assert second_payload["static_info"] == [{"event": "second"}]
    assert first.environment_event.event_id != second.environment_event.event_id
    assert first.source_fact.payload["content_sha256"] != second.source_fact.payload[
        "content_sha256"
    ]


def test_context_coordination_consumes_environment_data_source_fact(tmp_path: Path) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path, mission_id)
    environment = FakeEnvironment(transport, mission_id)
    command = _command(mission_id=mission_id)
    transport.send_command(command.to_command("maneuver-adapter"))
    environment.run_once()

    coordination = ContextCoordination(transport, mission_id)
    with transport.open_consumer(coordination.subscription) as consumer:
        snapshot = coordination.run_once(consumer)

    assert snapshot is not None
    last_result = environment.last_result
    assert last_result is not None
    assert snapshot.environment_data == last_result.source_fact.payload["reference"]


def test_all_maneuver_feedback_lifecycles_are_transport_events_and_correlated(tmp_path: Path) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path, mission_id)
    environment = FakeEnvironment(transport, mission_id)
    lifecycles = ("accepted", "active", "completed", "failed", "cancelled")

    for index, lifecycle in enumerate(lifecycles):
        command = _command(f"command-{index}", mission_id)
        transport.send_command(command.to_command("maneuver-adapter"))
        result = environment.run_once(lifecycle=lifecycle)
        assert result is not None
        feedback = ManeuverFeedback.from_dict(result.feedback.payload)
        assert feedback.lifecycle == lifecycle
        assert feedback.payload["command_id"] == command.command_id
        assert feedback.payload["correlation_id"] == command.correlation_id

    with transport.open_consumer(Subscription("feedback-reader", mission_id, "maneuver-feedback")) as reader:
        events: list[TransportEvent] = []
        while (delivery := reader.receive()) is not None:
            events.append(cast(TransportEvent, delivery.message))
            delivery.ack()
    assert [event.event_kind for event in events] == ["maneuver-feedback"] * 6
    assert [event.sequence for event in events] == list(range(6))
    feedback = [ManeuverFeedback.from_dict(event.payload) for event in events]
    assert [item.lifecycle for item in feedback] == [
        "accepted",
        "cancelled",
        "active",
        "completed",
        "failed",
        "cancelled",
    ]
    assert feedback[1].payload["reason"] == "overridden"


def test_maneuver_feedback_replay_is_idempotent_across_crash_window(tmp_path: Path) -> None:
    source_files = sorted(Path("src/onr").rglob("*.py"))
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in source_files}
    mission_id = "mission-1"
    transport = _transport(tmp_path, mission_id)
    command = _command(mission_id=mission_id)
    transport.send_command(command.to_command("maneuver-adapter"))
    environment = FakeEnvironment(transport, mission_id)
    with transport.open_consumer(environment.subscription) as consumer:
        delivery = consumer.receive()
        assert delivery is not None
        consumed = ManeuverCommand.from_command(cast(Command, delivery.message))
        first = environment.process_command(consumed, lifecycle="completed")
        # The process exits before acknowledging the command.
    restarted = FakeEnvironment(transport, mission_id)
    second = restarted.run_once(lifecycle="completed")
    assert second is not None
    assert second.command == first.command
    assert second.environment_event.event_id == first.environment_event.event_id
    assert second.environment_event.sequence == first.environment_event.sequence
    assert second.source_fact.event_id == first.source_fact.event_id
    assert second.source_fact.sequence == first.source_fact.sequence
    assert second.risk_observation.event_id == first.risk_observation.event_id
    assert second.risk_observation.sequence == first.risk_observation.sequence
    assert second.feedback.event_id == first.feedback.event_id
    assert second.feedback.sequence == first.feedback.sequence
    assert second.environment_file == first.environment_file
    assert second.environment_file.read_bytes() == first.environment_file.read_bytes()
    with transport.open_consumer(Subscription("feedback-reader", mission_id, "maneuver-feedback")) as reader:
        delivery = reader.receive()
        assert delivery is not None
        event = cast(TransportEvent, delivery.message)
        delivery.ack()
        assert reader.receive() is None
    assert event.event_id == second.feedback.event_id
    assert {path: hashlib.sha256(path.read_bytes()).digest() for path in source_files} == before


def test_maneuver_control_heartbeat_command_reaches_fake_environment(tmp_path: Path) -> None:
    mission_id = "mission-1"
    transport = _transport(tmp_path, mission_id)
    intent = ManeuverIntent("navigate", (ManeuverParameter("speed", 2),))
    decision = ManeuverControlDecision(
        decision_id="decision-1",
        mission_id=mission_id,
        plan_revision=3,
        maneuver_id="survey",
        physical_intent=intent,
    )
    snapshot = MissionSnapshot(mission_id, 1, "2026-01-01T00:00:00Z", plan_revision=3)
    status = FSMStatus(
        mission_id=mission_id,
        plan_revision=3,
        statechart_revision=1,
        active_state="survey-ready",
        transition_candidates=(TransitionCandidate("advance:survey", "survey-ready", "survey-active"),),
    )
    control = ManeuverControl(cast(Any, transport), StubAdapter(), FixedDecisionProvider(decision))

    heartbeat = control.heartbeat(snapshot, status)

    assert heartbeat.command is not None
    result = FakeEnvironment(transport, mission_id).run_once()
    assert result is not None
    assert result.command == heartbeat.command
    assert result.feedback.event_kind == "maneuver-feedback"
