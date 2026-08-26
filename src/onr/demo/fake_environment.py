"""Deterministic fixed-rate environment engine for the complete report demo."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, cast
from urllib.parse import quote

from onr.adapters.file_transport import FileTransport
from onr.contracts.context_coordination import create_source_fact_event
from onr.contracts.environment import (
    EntityObservation,
    EnvironmentTickResult,
    EventObservation,
    perception_to_transport_event,
)
from onr.contracts.fsm import ManeuverFeedback
from onr.contracts.maneuver_control import ManeuverCommand, PhysicalAction
from onr.contracts.transport import Command, TransportEvent
from onr.ports.transport import Subscription

SUPPORTED_LIFECYCLES = ("active", "completed", "failed", "cancelled")
_DEFAULT_EVENT_REPORT_PATH = (
    Path(__file__).parents[3]
    / "data/ships_report_and_trajectory_example/ships/events_report.json"
)


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _truncate_event_report_floats(value: object) -> object:
    if isinstance(value, float):
        return math.trunc(value * 10) / 10
    if isinstance(value, list):
        return [_truncate_event_report_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_event_report_floats(item) for key, item in value.items()}
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return result


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


@dataclass(frozen=True, slots=True)
class FakeEnvironmentResult:
    """Evidence emitted when one maneuver lifecycle is accepted or completed."""

    command: ManeuverCommand
    environment_event: TransportEvent
    source_fact: TransportEvent
    risk_observation: TransportEvent | None
    feedback: TransportEvent
    environment_file: Path
    feedback_events: tuple[TransportEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class FakeEnvironmentHeartbeat:
    """Current planning view emitted before planning or replanning."""

    environment_event: TransportEvent
    source_fact: TransportEvent
    environment_file: Path


class FakeEnvironment:
    """Advance kinematics and report sensing without consuming wall-clock time."""

    def __init__(
        self,
        transport: FileTransport,
        mission_id: str,
        *,
        target_service: str = "maneuver-adapter",
        command_topic: str = "maneuver",
        feedback_topic: str = "maneuver-feedback",
        perception_topic: str = "environment-perceptions",
        environment_topic: str = "environment-data",
        context_topic: str = "normalized-plans",
        max_retries: int = 3,
        output_root: Path | str | None = None,
        event_report_path: Path | str | None = None,
        tick_seconds: float = 0.5,
        initial_position: tuple[float, float, float] = (0, 0, -250),
        max_velocity: float = 20,
        fov_radius: float = 30,
        command_protocol_version: int = 1,
        feedback_protocol_version: int = 1,
        environment_data_protocol_version: int = 1,
        perception_protocol_version: int = 1,
        supported_actions: tuple[PhysicalAction | str, ...] = tuple(PhysicalAction),
    ) -> None:
        if not isinstance(transport, FileTransport):
            raise TypeError("FakeEnvironment requires a FileTransport")
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("FakeEnvironment Mission ID must be non-empty")
        self.transport = transport
        self.mission_id = mission_id
        self.target_service = target_service
        self.command_topic = command_topic
        self.feedback_topic = feedback_topic
        self.perception_topic = perception_topic
        self.environment_topic = environment_topic
        self.context_topic = context_topic
        self.command_protocol_version = command_protocol_version
        self.feedback_protocol_version = feedback_protocol_version
        self.environment_data_protocol_version = environment_data_protocol_version
        self.perception_protocol_version = perception_protocol_version
        for value, label in (
            (command_protocol_version, "Maneuver Command protocol version"),
            (feedback_protocol_version, "Maneuver Feedback protocol version"),
            (environment_data_protocol_version, "environment-data protocol version"),
            (perception_protocol_version, "perception protocol version"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        try:
            self.supported_actions = frozenset(
                PhysicalAction(item) for item in supported_actions
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("fake environment supported action is invalid") from exc
        if not self.supported_actions:
            raise ValueError("fake environment requires at least one supported action")
        self.tick_seconds = _positive_number(tick_seconds, "simulation tick")
        self.max_velocity = _positive_number(max_velocity, "maximum velocity")
        self.fov_radius = _positive_number(fov_radius, "field-of-view radius")
        if len(initial_position) != 3 or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in initial_position
        ):
            raise ValueError("initial drone position must contain three finite numbers")
        self.drone_position = (
            float(initial_position[0]),
            float(initial_position[1]),
            float(initial_position[2]),
        )
        self.output_root = (
            Path(output_root)
            if output_root is not None
            else self.transport.root.parent / "environment"
        )
        self.event_report_path = Path(event_report_path or _DEFAULT_EVENT_REPORT_PATH)
        raw_report = json.loads(self.event_report_path.read_text(encoding="utf-8"))
        if not isinstance(raw_report, list):
            raise TypeError("event report must be a JSON array")
        self._report = cast(
            list[dict[str, Any]], _truncate_event_report_floats(raw_report)
        )
        self.subscription = Subscription(
            target_service, mission_id, command_topic, max_retries
        )
        if self.subscription not in self.transport.subscriptions:
            self.transport.subscriptions = (
                *self.transport.subscriptions,
                self.subscription,
            )
        self._lock = RLock()
        self._results: dict[tuple[str, str], FakeEnvironmentResult] = {}
        self._environment_facts: dict[str, tuple[TransportEvent, TransportEvent]] = {}
        self._entity_positions = self._initial_entity_positions()
        self._observed_event_indices: set[int] = set()
        self._route_targets: tuple[tuple[float, float, float], ...] = ()
        self._route_index = 0
        self._active_command: ManeuverCommand | None = None
        self._environment_revision = 0
        self._current_perceptions: tuple[TransportEvent, ...] = ()
        self.last_result: FakeEnvironmentResult | None = None
        self.last_output_path: Path | None = None
        self._latest_environment_event: TransportEvent | None = None
        self.last_override_feedback: TransportEvent | None = None
        self.mission_time_seconds = 0.0
        self.current_maneuver: dict[str, object] | None = None
        self.navigation_status = "idle"

    @staticmethod
    def subscription_for(
        mission_id: str,
        *,
        target_service: str = "maneuver-adapter",
        command_topic: str = "maneuver",
        max_retries: int = 3,
    ) -> Subscription:
        return Subscription(target_service, mission_id, command_topic, max_retries)

    @property
    def event_report(self) -> tuple[Mapping[str, object], ...]:
        """Read-only planning source; it is never part of live model context."""

        with self._lock:
            return tuple(self._report)

    @property
    def active_command(self) -> ManeuverCommand | None:
        with self._lock:
            return self._active_command

    @property
    def current_time(self) -> float:
        with self._lock:
            return self.mission_time_seconds

    @property
    def latest_environment_event(self) -> TransportEvent | None:
        with self._lock:
            return self._latest_environment_event

    @property
    def has_current_maneuver(self) -> bool:
        with self._lock:
            return self.current_maneuver is not None

    def heartbeat(self) -> FakeEnvironmentHeartbeat:
        """Publish the complete planning view without advancing simulation time."""

        with self._lock:
            environment_file = self._environment_file()
            environment_data = self.planning_environment_data()
            _atomic_write_json(environment_file, environment_data)
            self.last_output_path = environment_file
            environment_event, source_fact = self._publish_environment_data(
                environment_data
            )
            return FakeEnvironmentHeartbeat(
                environment_event, source_fact, environment_file
            )

    def planning_environment_data(self) -> dict[str, object]:
        """Return current control state plus the complete report for Hyper planning."""

        with self._lock:
            current = self.current_environment_data()
            graph = dict(cast(Mapping[str, object], current["scene_graph"]))
            graph["entities"] = self._planning_entities()
            return {"scene_graph": graph, "static_info": self._report}

    def run_once(self, lifecycle: str = "active") -> FakeEnvironmentResult | None:
        """Consume and acknowledge one file-backed Maneuver command."""

        with self.transport.open_consumer(self.subscription) as consumer:
            delivery = consumer.receive()
            if delivery is None:
                return None
            if not isinstance(delivery.message, Command):
                delivery.ack()
                return None
            try:
                command = ManeuverCommand.from_command(
                    delivery.message, self.command_topic
                )
                result = self.process_command(command, lifecycle=lifecycle)
            except Exception:
                delivery.nack()
                raise
            delivery.ack()
            return result

    def process_command(
        self, command: ManeuverCommand, lifecycle: str = "active"
    ) -> FakeEnvironmentResult:
        """Apply one typed command lifecycle idempotently."""

        with self._lock:
            return self._process_command(command, lifecycle=lifecycle)

    def _process_command(
        self, command: ManeuverCommand, lifecycle: str = "active"
    ) -> FakeEnvironmentResult:

        if lifecycle not in SUPPORTED_LIFECYCLES:
            raise ValueError(f"unsupported maneuver lifecycle: {lifecycle}")
        if lifecycle == "active":
            return self._apply_command(command, lifecycle=lifecycle)
        key = (command.command_id, lifecycle)
        existing = self._results.get(key)
        if existing is not None:
            return existing
        if command.mission_id != self.mission_id:
            raise ValueError("maneuver command Mission ID does not match environment")
        self._set_current_maneuver(command, lifecycle)
        if (
            self._active_command is not None
            and self._active_command.command_id == command.command_id
        ):
            self._active_command = None
        self.navigation_status = lifecycle
        feedback = self._feedback(command, lifecycle)
        environment_event, source_fact, environment_file = self._publish_current_view()
        result = FakeEnvironmentResult(
            command,
            environment_event,
            source_fact,
            None,
            feedback,
            environment_file,
            (feedback,),
        )
        self._results[key] = result
        self.last_result = result
        return result

    def apply_command(
        self, command: ManeuverCommand, *, lifecycle: str = "active"
    ) -> FakeEnvironmentResult:
        """Apply one transport-delivered command and publish lifecycle feedback."""

        with self._lock:
            return self._apply_command(command, lifecycle=lifecycle)

    def _apply_command(
        self, command: ManeuverCommand, *, lifecycle: str = "active"
    ) -> FakeEnvironmentResult:

        if not isinstance(command, ManeuverCommand):
            raise TypeError("command application requires a ManeuverCommand")
        if command.mission_id != self.mission_id:
            raise ValueError("maneuver command Mission ID does not match environment")
        if command.schema_version != self.command_protocol_version:
            raise ValueError("maneuver command protocol version is unsupported")
        if PhysicalAction(command.action) not in self.supported_actions:
            raise ValueError("maneuver command action is not declared by the profile")
        key = (command.command_id, lifecycle)
        existing = self._results.get(key)
        if existing is not None:
            return existing
        feedback_events: list[TransportEvent] = []
        if (
            self._active_command is not None
            and self._active_command.command_id != command.command_id
        ):
            self.last_override_feedback = self._feedback(
                self._active_command,
                "cancelled",
                extra_payload={"reason": "overridden"},
            )
            feedback_events.append(self.last_override_feedback)
        self._active_command = command
        self._route_targets = ()
        self._route_index = 0
        if command.action == "search_area":
            self._route_targets = self._search_route(self._command_parameters(command))
        self._set_current_maneuver(command, "active")
        self.navigation_status = "active"
        feedback = self._feedback(command, lifecycle)
        feedback_events.append(feedback)
        environment_event, source_fact, environment_file = self._publish_current_view()
        result = FakeEnvironmentResult(
            command,
            environment_event,
            source_fact,
            None,
            feedback,
            environment_file,
            tuple(feedback_events),
        )
        self._results[key] = result
        self.last_result = result
        return result

    def submit(
        self, command: ManeuverCommand, *, lifecycle: str = "active"
    ) -> FakeEnvironmentResult:
        """Explicit environment-side application helper used by focused demos."""

        return self.apply_command(command, lifecycle=lifecycle)

    def tick(self) -> EnvironmentTickResult:
        """Advance one configured tick and publish only current perceptions."""

        with self._lock:
            return self._tick()

    def _tick(self) -> EnvironmentTickResult:

        previous_time = self.mission_time_seconds
        self.mission_time_seconds = round(previous_time + self.tick_seconds, 10)
        feedback: list[TransportEvent] = []
        self._advance_report_entities(previous_time, self.mission_time_seconds)
        action_feedback = self._advance_active_action()
        if action_feedback is not None:
            feedback.append(action_feedback)
        perceptions = self._sense_report_events(
            previous_time, self.mission_time_seconds
        )
        self._current_perceptions = perceptions

        active = self._active_command
        if active is not None and self._action_complete(active):
            feedback.append(self._feedback(active, "completed"))
            self.navigation_status = "completed"
            self._set_current_maneuver(active, "completed")
            self._active_command = None

        environment_event, _source_fact, _path = self._publish_current_view()
        return EnvironmentTickResult(
            current_time=self.mission_time_seconds,
            environment_data=environment_event.payload,
            feedback_events=tuple(feedback),
            perception_events=perceptions,
        )

    def current_environment_data(self) -> dict[str, object]:
        """Return current control evidence and only the latest tick's perceptions."""

        with self._lock:
            return self._current_environment_data()

    def _current_environment_data(self) -> dict[str, object]:

        maneuver = dict(self.current_maneuver) if self.current_maneuver else None
        raw_revision = cast(Mapping[str, object], self.current_maneuver or {}).get(
            "plan_revision", 0
        )
        retained_revision = (
            raw_revision
            if isinstance(raw_revision, int) and not isinstance(raw_revision, bool)
            else 0
        )
        graph: dict[str, object] = {
            "mission_id": self.mission_id,
            "plan_revision": (
                self._active_command.plan_revision
                if self._active_command is not None
                else retained_revision
            ),
            "mission_time_seconds": self.mission_time_seconds,
            "current_maneuver": maneuver,
            "maneuvers": [maneuver] if maneuver is not None else [],
            "navigation_status": self.navigation_status,
            "drone": {
                "entity_id": "drone-1",
                "position": {
                    "x": self.drone_position[0],
                    "y": self.drone_position[1],
                    "z": self.drone_position[2],
                },
                "max_velocity": self.max_velocity,
                "fov_radius": self.fov_radius,
            },
        }
        return {
            "scene_graph": graph,
            "perceptions": [_plain(item.payload) for item in self._current_perceptions],
        }

    def _initial_entity_positions(self) -> dict[str, tuple[float, float, float]]:
        positions: dict[str, tuple[float, float, float]] = {}
        for record in self._report:
            entity_id = str(record["entity_id"])
            raw = cast(list[int | float], record["position"])
            positions.setdefault(
                entity_id,
                (float(raw[0]), float(raw[1]), float(raw[2])),
            )
        return positions

    def _planning_entities(self) -> list[dict[str, object]]:
        entities = [
            {
                "id": entity_id,
                "type": "report-entity",
                "location": {"x": position[0], "y": position[1], "z": position[2]},
            }
            for entity_id, position in sorted(
                self._entity_positions.items(), key=lambda item: int(item[0])
            )
        ]
        entities.append(
            {
                "id": "drone-1",
                "type": "drone",
                "location": {
                    "x": self.drone_position[0],
                    "y": self.drone_position[1],
                    "z": self.drone_position[2],
                },
                "max_velocity": self.max_velocity,
                "fov_radius": self.fov_radius,
            }
        )
        return entities

    def _set_current_maneuver(self, command: ManeuverCommand, lifecycle: str) -> None:
        prior_start = self.mission_time_seconds
        if (
            self.current_maneuver is not None
            and self.current_maneuver.get("command_id") == command.command_id
        ):
            prior_start = _finite_number(
                self.current_maneuver.get("start_time", prior_start),
                "maneuver start time",
            )
        self.current_maneuver = {
            "maneuver_id": command.maneuver_id,
            "action": command.action,
            "parameters": command.intent.to_dict()["parameters"],
            "command_id": command.command_id,
            "correlation_id": command.correlation_id,
            "plan_revision": command.plan_revision,
            "start_time": prior_start,
            "lifecycle": lifecycle,
        }

    @staticmethod
    def _command_parameters(command: ManeuverCommand) -> Mapping[str, object]:
        return cast(Mapping[str, object], command.intent.to_dict()["parameters"])

    def _advance_active_action(self) -> TransportEvent | None:
        command = self._active_command
        if command is None:
            return None
        parameters = self._command_parameters(command)
        remaining_distance = 0.0
        target: tuple[float, float, float] | None = None
        requested_speed = False
        if command.action == "navigate":
            target = self._navigation_target(parameters)
            remaining_distance = math.dist(self.drone_position, target)
            requested_speed = True
        elif command.action == "search_area":
            remaining_distance = self._remaining_route_distance()
            requested_speed = True
        elif command.action == "investigate":
            entity_id = parameters.get("entity_id")
            if (
                not isinstance(entity_id, str)
                or entity_id not in self._entity_positions
            ):
                feedback = self._feedback(
                    command,
                    "failed",
                    extra_payload={"reason": "unknown entity"},
                )
                self.navigation_status = "failed"
                self._set_current_maneuver(command, "failed")
                self._active_command = None
                return feedback
            entity_position = self._entity_positions[entity_id]
            standoff = self._standoff_distance(parameters)
            entity_distance = math.dist(self.drone_position, entity_position)
            remaining_distance = max(entity_distance - standoff, 0.0)
            if remaining_distance > 1e-9:
                scale = remaining_distance / entity_distance
                target = (
                    self.drone_position[0]
                    + (entity_position[0] - self.drone_position[0]) * scale,
                    self.drone_position[1]
                    + (entity_position[1] - self.drone_position[1]) * scale,
                    self.drone_position[2]
                    + (entity_position[2] - self.drone_position[2]) * scale,
                )
        else:
            return None
        speed = self._effective_speed(
            parameters,
            remaining_distance,
            requested_speed=requested_speed,
            action=command.action,
        )
        if self.current_maneuver is not None:
            self.current_maneuver["effective_speed"] = speed
        travel = speed * self.tick_seconds
        if command.action == "search_area":
            self._advance_route(travel)
        elif target is not None:
            self._move_toward(target, travel)
        return None

    def _move_toward(self, target: tuple[float, float, float], travel: float) -> None:
        distance = math.dist(self.drone_position, target)
        if distance <= travel or distance <= 1e-9:
            self.drone_position = target
            return
        scale = travel / distance
        self.drone_position = tuple(
            self.drone_position[index]
            + (target[index] - self.drone_position[index]) * scale
            for index in range(3)
        )

    def _effective_speed(
        self,
        parameters: Mapping[str, object],
        remaining_distance: float,
        *,
        requested_speed: bool,
        action: str,
    ) -> float:
        deadline = parameters.get("deadline_time")
        if deadline is None:
            if not requested_speed:
                return self.max_velocity
            return min(
                _positive_number(
                    parameters.get("speed", self.max_velocity), f"{action} speed"
                ),
                self.max_velocity,
            )
        deadline_time = _finite_number(deadline, f"{action} deadline time")
        if deadline_time < 0:
            raise ValueError(f"{action} deadline time must be non-negative")
        interval_start = self.mission_time_seconds - self.tick_seconds
        remaining_time = deadline_time - interval_start
        if remaining_distance <= 1e-9:
            return 0.0
        required_speed = (
            remaining_distance / remaining_time
            if remaining_time > 0
            else self.max_velocity
        )
        return min(required_speed, self.max_velocity)

    def _navigation_target(
        self, parameters: Mapping[str, object]
    ) -> tuple[float, float, float]:
        return (
            _finite_number(
                parameters.get("x"),
                "navigation target x",
            ),
            _finite_number(
                parameters.get("y"),
                "navigation target y",
            ),
            _finite_number(
                parameters.get("z", self.drone_position[2]),
                "navigation target z",
            ),
        )

    def _search_route(
        self, parameters: Mapping[str, object]
    ) -> tuple[tuple[float, float, float], ...]:
        polygon = parameters.get("polygon")
        if not isinstance(polygon, (list, tuple)) or len(polygon) < 3:
            raise ValueError("search polygon must contain at least three points")
        altitude = _finite_number(
            parameters.get("altitude", self.drone_position[2]), "search altitude"
        )
        points: list[tuple[float, float, float]] = []
        for point in polygon:
            if not isinstance(point, Mapping) or set(point) != {"x", "y"}:
                raise ValueError("search polygon points must contain exactly x and y")
            points.append(
                (
                    _finite_number(point["x"], "search polygon x"),
                    _finite_number(point["y"], "search polygon y"),
                    altitude,
                )
            )
        return (*points, points[0])

    def _remaining_route_distance(self) -> float:
        if self._route_index >= len(self._route_targets):
            return 0.0
        distance = math.dist(
            self.drone_position, self._route_targets[self._route_index]
        )
        for index in range(self._route_index, len(self._route_targets) - 1):
            distance += math.dist(
                self._route_targets[index], self._route_targets[index + 1]
            )
        return distance

    def _advance_route(self, travel: float) -> None:
        while self._route_index < len(self._route_targets):
            target = self._route_targets[self._route_index]
            distance = math.dist(self.drone_position, target)
            if distance <= 1e-9:
                self.drone_position = target
                self._route_index += 1
                continue
            if travel < distance:
                self._move_toward(target, travel)
                return
            self.drone_position = target
            self._route_index += 1
            travel -= distance

    @staticmethod
    def _standoff_distance(parameters: Mapping[str, object]) -> float:
        value = _finite_number(
            parameters.get("standoff_distance", 0.0), "investigation standoff distance"
        )
        if value < 0:
            raise ValueError("investigation standoff distance must be non-negative")
        return value

    def _action_complete(self, command: ManeuverCommand) -> bool:
        parameters = self._command_parameters(command)
        if command.action == "navigate":
            return (
                math.dist(self.drone_position, self._navigation_target(parameters))
                <= 1e-9
            )
        if command.action == "search_area":
            return self._route_index >= len(self._route_targets)
        if command.action == "investigate":
            entity_id = parameters.get("entity_id")
            return (
                isinstance(entity_id, str)
                and entity_id in self._entity_positions
                and math.dist(self.drone_position, self._entity_positions[entity_id])
                <= self._standoff_distance(parameters) + 1e-9
            )
        duration = parameters.get(
            "duration_seconds", parameters.get("duration", self.tick_seconds)
        )
        start = _finite_number(
            cast(Mapping[str, object], self.current_maneuver or {}).get(
                "start_time", 0.0
            ),
            "maneuver start time",
        )
        return self.mission_time_seconds >= start + float(cast(int | float, duration))

    def _sense_report_events(
        self, previous_time: float, current_time: float
    ) -> tuple[TransportEvent, ...]:
        result: list[TransportEvent] = []
        for source_index, record in enumerate(self._report, start=1):
            event_time = float(record["time"])
            if not (previous_time < event_time <= current_time):
                continue
            entity_id = str(record["entity_id"])
            raw_position = cast(list[int | float], record["position"])
            position = (
                float(raw_position[0]),
                float(raw_position[1]),
                float(raw_position[2]),
            )
            if source_index in self._observed_event_indices:
                continue
            if math.dist(self.drone_position, position) > self.fov_radius:
                continue
            uncertainty = self._uncertainty(source_index, entity_id)
            entity = EntityObservation(
                observation_id=f"entity-observed:{self.mission_id}:{source_index}",
                entity_id=entity_id,
                position=position,
                observed_time=current_time,
                uncertainty_score=uncertainty,
            )
            event = EventObservation(
                observation_id=f"event-observed:{self.mission_id}:{source_index}",
                entity_id=entity_id,
                position=position,
                observed_time=current_time,
                uncertainty_score=uncertainty,
                source_event_index=source_index,
                event_type=str(record["event type"]),
                event_information=cast(
                    Mapping[str, object], record["event information"]
                ),
                event_time=event_time,
            )
            for perception in (entity, event):
                sequence = self.transport.next_event_sequence(
                    self.perception_topic, self.mission_id
                )
                result.append(
                    self.transport.publish_event(
                        self.perception_topic,
                        perception_to_transport_event(
                            self.mission_id,
                            perception,
                            sequence=sequence,
                            schema_version=self.perception_protocol_version,
                        ),
                    )
                )
            self._observed_event_indices.add(source_index)
        return tuple(result)

    def _advance_report_entities(
        self, previous_time: float, current_time: float
    ) -> None:
        for record in self._report:
            event_time = float(record["time"])
            if not (previous_time < event_time <= current_time):
                continue
            raw_position = cast(list[int | float], record["position"])
            self._entity_positions[str(record["entity_id"])] = (
                float(raw_position[0]),
                float(raw_position[1]),
                float(raw_position[2]),
            )

    @staticmethod
    def _uncertainty(source_index: int, entity_id: str) -> float:
        digest = hashlib.sha256(f"{source_index}:{entity_id}".encode()).digest()
        return round(0.05 + digest[0] / 255 * 0.4, 6)

    def _environment_file(self) -> Path:
        return (
            self.output_root / quote(self.mission_id, safe="._-") / "environment.json"
        )

    def _publish_current_view(
        self,
    ) -> tuple[TransportEvent, TransportEvent, Path]:
        path = self._environment_file()
        data = self.current_environment_data()
        _atomic_write_json(path, data)
        self.last_output_path = path
        environment_event, source_fact = self._publish_environment_data(data)
        return environment_event, source_fact, path

    def _publish_environment_data(
        self, environment_data: Mapping[str, object]
    ) -> tuple[TransportEvent, TransportEvent]:
        document = json.dumps(
            environment_data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        reference = hashlib.sha256(document.encode()).hexdigest()
        cached = self._environment_facts.get(reference)
        if cached is not None:
            self._latest_environment_event = cached[0]
            return cached
        environment_event_id = f"environment-data:{self.mission_id}:{reference}"
        environment_event = self.transport.get_event(environment_event_id)
        if environment_event is None:
            environment_event = self.transport.publish_event(
                self.environment_topic,
                TransportEvent(
                    schema_version=self.environment_data_protocol_version,
                    event_id=environment_event_id,
                    mission_id=self.mission_id,
                    sequence=self.transport.next_event_sequence(
                        self.environment_topic, self.mission_id
                    ),
                    event_kind="environment_data",
                    payload=json.loads(document),
                ),
            )
        source_fact_id = f"source-fact:{self.mission_id}:environment_data:{reference}"
        source_fact = self.transport.get_event(source_fact_id)
        if source_fact is None:

            def source_event() -> TransportEvent:
                return create_source_fact_event(
                    self.mission_id,
                    "environment_data",
                    self._environment_revision,
                    event_id=source_fact_id,
                    sequence=self.transport.next_event_sequence(
                        self.context_topic, self.mission_id
                    ),
                    reference=environment_event_id,
                )

            try:
                source_fact = self.transport.publish_event(
                    self.context_topic, source_event()
                )
            except ValueError as exc:
                if str(exc) != "event sequence conflicts with existing content":
                    raise
                source_fact = self.transport.publish_event(
                    self.context_topic, source_event()
                )
            self._environment_revision += 1
        self._environment_facts[reference] = (environment_event, source_fact)
        self._latest_environment_event = environment_event
        return environment_event, source_fact

    def _feedback(
        self,
        command: ManeuverCommand,
        lifecycle: str,
        *,
        extra_payload: Mapping[str, object] | None = None,
        identity_suffix: str | None = None,
    ) -> TransportEvent:
        feedback_id = f"maneuver-feedback:{command.command_id}:{lifecycle}"
        if identity_suffix is not None:
            feedback_id = f"{feedback_id}:{identity_suffix}"
        existing = self.transport.get_event(feedback_id)
        if existing is not None:
            return existing
        feedback = ManeuverFeedback(
            feedback_id=feedback_id,
            mission_id=command.mission_id,
            maneuver_id=command.maneuver_id,
            lifecycle=lifecycle,
            payload={
                "command_id": command.command_id,
                "correlation_id": command.correlation_id,
                **dict(extra_payload or {}),
            },
        )
        return self.transport.publish_event(
            self.feedback_topic,
            TransportEvent(
                schema_version=self.feedback_protocol_version,
                event_id=feedback_id,
                mission_id=self.mission_id,
                sequence=self.transport.next_event_sequence(
                    self.feedback_topic, self.mission_id
                ),
                event_kind="maneuver-feedback",
                payload=feedback.to_dict(),
            ),
        )


__all__ = [
    "SUPPORTED_LIFECYCLES",
    "FakeEnvironment",
    "FakeEnvironmentHeartbeat",
    "FakeEnvironmentResult",
]
