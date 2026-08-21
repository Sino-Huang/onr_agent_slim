"""Packaged deterministic demo environment built on the public transport seam.

This module emits external environment evidence for demonstrations. It does not
apply maneuver feedback to mission state and is not production authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from onr.adapters.file_transport import FileTransport
from onr.application.bayesian_belief import create_risk_observation_event
from onr.contracts.bayesian_belief import EntityAssociation, RiskObservation
from onr.contracts.context_coordination import create_source_fact_event
from onr.contracts.fsm import ManeuverFeedback
from onr.contracts.maneuver_control import ManeuverCommand
from onr.contracts.transport import Command, TransportEvent
from onr.ports.transport import Subscription

SUPPORTED_LIFECYCLES = ("accepted", "active", "completed", "failed", "cancelled")
_DEFAULT_EVENT_REPORT_PATH = (
    Path(__file__).parents[3]
    / "data/ships_report_and_trajectory_example/ships/events_report.json"
)


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class FakeEnvironmentResult:
    """The immutable evidence emitted for one consumed maneuver command."""

    command: ManeuverCommand
    environment_event: TransportEvent
    source_fact: TransportEvent
    risk_observation: TransportEvent
    feedback: TransportEvent
    environment_file: Path


@dataclass(frozen=True, slots=True)
class FakeEnvironmentHeartbeat:
    """Environment evidence emitted before any maneuver command."""

    environment_event: TransportEvent
    source_fact: TransportEvent
    environment_file: Path


class FakeEnvironment:
    """Consume file-backed maneuver commands and publish deterministic evidence."""

    def __init__(
        self,
        transport: FileTransport,
        mission_id: str,
        *,
        target_service: str = "maneuver-adapter",
        command_topic: str = "maneuver",
        feedback_topic: str = "maneuver-feedback",
        environment_topic: str = "environment-data",
        context_topic: str = "normalized-plans",
        max_retries: int = 3,
        output_root: Path | str | None = None,
        event_report_path: Path | str | None = None,
    ) -> None:
        if not isinstance(transport, FileTransport):
            raise TypeError("FakeEnvironment requires a FileTransport")
        self.transport = transport
        self.mission_id = mission_id
        self.target_service = target_service
        self.command_topic = command_topic
        self.feedback_topic = feedback_topic
        self.environment_topic = environment_topic
        self.context_topic = context_topic
        self.output_root = (
            Path(output_root)
            if output_root is not None
            else self.transport.root.parent / "environment"
        )
        self.event_report_path = Path(event_report_path or _DEFAULT_EVENT_REPORT_PATH)
        self.subscription = Subscription(target_service, mission_id, command_topic, max_retries)
        self._results: dict[tuple[str, str], FakeEnvironmentResult] = {}
        self._environment_facts: dict[str, tuple[TransportEvent, TransportEvent]] = {}
        self.last_result: FakeEnvironmentResult | None = None
        self.last_output_path: Path | None = None
        self.latest_environment_event: TransportEvent | None = None
        self.last_override_feedback: TransportEvent | None = None
        self.mission_time_seconds = 0.0
        self.current_maneuver: dict[str, object] | None = None
        self.navigation_status = "idle"
        self._active_command: ManeuverCommand | None = None

    @staticmethod
    def subscription_for(
        mission_id: str,
        *,
        target_service: str = "maneuver-adapter",
        command_topic: str = "maneuver",
        max_retries: int = 3,
    ) -> Subscription:
        """Return the command subscription to register on ``FileTransport``."""

        return Subscription(target_service, mission_id, command_topic, max_retries)

    def heartbeat(
        self, mission_time_seconds: float | None = None
    ) -> FakeEnvironmentHeartbeat:
        """Publish the current environment data before planner generation."""

        if mission_time_seconds is not None:
            self._set_mission_time(mission_time_seconds)

        environment_file = (
            self.output_root / quote(self.mission_id, safe="._-") / "environment.json"
        )
        self.last_output_path = environment_file
        graph = self._graph(
            plan_revision=(
                self._active_command.plan_revision if self._active_command else 0
            ),
            entities=self._entities_for_seed(f"{self.mission_id}:heartbeat"),
            environment_file=environment_file,
        )
        environment_data = self._environment_data(graph)
        _atomic_write_json(environment_file, environment_data)
        environment_event, source_fact = self._publish_environment_data(
            environment_data, 0
        )
        return FakeEnvironmentHeartbeat(
            environment_event=environment_event,
            source_fact=source_fact,
            environment_file=environment_file,
        )

    def run_once(self, lifecycle: str = "active") -> FakeEnvironmentResult | None:
        """Consume and acknowledge at most one command from the registered subscription."""

        with self.transport.open_consumer(self.subscription) as consumer:
            delivery = consumer.receive()
            if delivery is None:
                return None
            if not isinstance(delivery.message, Command):
                delivery.ack()
                return None
            try:
                command = ManeuverCommand.from_command(delivery.message)
                result = self.process_command(command, lifecycle=lifecycle)
            except Exception:
                delivery.nack()
                raise
            delivery.ack()
            self.last_result = result
            return result

    def process_command(
        self, command: ManeuverCommand, lifecycle: str = "active"
    ) -> FakeEnvironmentResult:
        """Publish deterministic evidence for a command without changing mission authority."""

        if not isinstance(command, ManeuverCommand):
            raise TypeError("process_command requires a ManeuverCommand")
        if command.mission_id != self.mission_id:
            raise ValueError("maneuver command mission ID does not match the environment")
        if lifecycle not in SUPPORTED_LIFECYCLES:
            raise ValueError(f"unsupported maneuver lifecycle: {lifecycle}")
        key = (command.command_id, lifecycle)
        existing = self._results.get(key)
        if existing is not None:
            self.last_result = existing
            return existing

        if (
            lifecycle in {"accepted", "active"}
            and self._active_command is not None
            and self._active_command.command_id != command.command_id
        ):
            self.last_override_feedback = self._feedback(
                self._active_command,
                "cancelled",
                extra_payload={"reason": "overridden"},
            )
        if lifecycle in {"accepted", "active"}:
            self._active_command = command
            self.current_maneuver = {
                "maneuver_id": command.maneuver_id,
                "action": command.action,
                "parameters": {
                    parameter.name: parameter.value for parameter in command.parameters
                },
                "command_id": command.command_id,
                "correlation_id": command.correlation_id,
                "start_time": self.mission_time_seconds,
                "lifecycle": "active",
            }
            self.navigation_status = "active"
        else:
            if self.current_maneuver is None or self.current_maneuver.get(
                "command_id"
            ) != command.command_id:
                self.current_maneuver = {
                    "maneuver_id": command.maneuver_id,
                    "action": command.action,
                    "parameters": {
                        parameter.name: parameter.value
                        for parameter in command.parameters
                    },
                    "command_id": command.command_id,
                    "correlation_id": command.correlation_id,
                    "start_time": self.mission_time_seconds,
                    "lifecycle": lifecycle,
                }
            else:
                self.current_maneuver["lifecycle"] = lifecycle
            self.navigation_status = lifecycle
            if self._active_command is not None and self._active_command.command_id == command.command_id:
                self._active_command = None

        environment_event, source_fact, environment_file = (
            self._environment_event_and_fact(command)
        )
        risk_observation = self._risk_observation(command)
        feedback = self._feedback(command, lifecycle)
        result = FakeEnvironmentResult(
            command,
            environment_event,
            source_fact,
            risk_observation,
            feedback,
            environment_file,
        )
        self._results[key] = result
        self.last_result = result
        return result

    def submit(self, command: ManeuverCommand) -> FakeEnvironmentResult:
        """Maneuver-adapter seam: make a submitted action active immediately."""

        return self.process_command(command, lifecycle="active")

    def tick(
        self, mission_time_seconds: float | None = None
    ) -> FakeEnvironmentResult | FakeEnvironmentHeartbeat:
        """Advance fake Mission time and complete the currently active action."""

        if mission_time_seconds is not None:
            self._set_mission_time(mission_time_seconds)
        active = self._active_command
        if active is None:
            return self.heartbeat()
        return self.process_command(active, lifecycle="completed")

    def current_environment_data(self) -> dict[str, object]:
        """Return the latest model-visible environment payload."""

        if self.latest_environment_event is None:
            return dict(self.heartbeat().environment_event.payload)
        return dict(self.latest_environment_event.payload)

    def _set_mission_time(self, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("fake environment Mission time must be numeric")
        self.mission_time_seconds = float(value)

    def _environment_event_and_fact(
        self, command: ManeuverCommand
    ) -> tuple[TransportEvent, TransportEvent, Path]:
        environment_file = (
            self.output_root / quote(self.mission_id, safe="._-") / "environment.json"
        )
        self.last_output_path = environment_file
        graph = self._graph(
            plan_revision=command.plan_revision,
            entities=self._entities(command),
            environment_file=environment_file,
        )
        environment_data = self._environment_data(graph)
        _atomic_write_json(environment_file, environment_data)
        environment_event, source_fact = self._publish_environment_data(
            environment_data, command.plan_revision
        )
        return environment_event, source_fact, environment_file

    def _graph(
        self,
        *,
        plan_revision: int,
        entities: list[dict[str, object]],
        environment_file: Path,
    ) -> dict[str, object]:
        maneuver = dict(self.current_maneuver) if self.current_maneuver else None
        return {
            "mission_id": self.mission_id,
            "plan_revision": plan_revision,
            "mission_time_seconds": self.mission_time_seconds,
            "current_maneuver": maneuver,
            "navigation_status": self.navigation_status,
            "maneuvers": [maneuver] if maneuver is not None else [],
            "entities": entities,
            "environment_file": str(environment_file),
        }

    def _environment_data(self, graph: dict[str, object]) -> dict[str, object]:
        event_report = json.loads(self.event_report_path.read_text(encoding="utf-8"))
        return {
            "scene_graph": graph,
            "static_info": event_report,
        }

    def _publish_environment_data(
        self, environment_data: dict[str, object], revision: int
    ) -> tuple[TransportEvent, TransportEvent]:
        environment_json = json.dumps(
            environment_data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        reference = hashlib.sha256(environment_json.encode("utf-8")).hexdigest()
        cached = self._environment_facts.get(reference)
        if cached is not None:
            return cached

        environment_event_id = f"environment-data:{self.mission_id}:{reference}"
        source_fact_id = (
            f"source-fact:{self.mission_id}:environment_data:{reference}"
        )
        environment_event = self.transport.get_event(environment_event_id)
        source_fact = self.transport.get_event(source_fact_id)
        if environment_event is None:
            environment_event = self.transport.publish_event(
                self.environment_topic,
                TransportEvent(
                    schema_version=1,
                    event_id=environment_event_id,
                    mission_id=self.mission_id,
                    sequence=self.transport.next_event_sequence(
                        self.environment_topic, self.mission_id
                    ),
                    event_kind="environment_data",
                    payload=json.loads(environment_json),
                ),
            )
        if source_fact is None:
            source_fact = self.transport.publish_event(
                self.context_topic,
                create_source_fact_event(
                    self.mission_id,
                    "environment_data",
                    revision,
                    event_id=source_fact_id,
                    sequence=self.transport.next_event_sequence(
                        self.context_topic, self.mission_id
                    ),
                    reference=environment_event_id,
                    content_sha256=reference,
                ),
            )
        self._environment_facts[reference] = (environment_event, source_fact)
        self.latest_environment_event = environment_event
        return environment_event, source_fact

    def _entities(self, command: ManeuverCommand) -> list[dict[str, object]]:
        return self._entities_for_seed(
            f"{command.mission_id}:{command.command_id}:"
            f"{command.plan_revision}:{command.maneuver_id}"
        )

    @staticmethod
    def _entities_for_seed(seed_value: str) -> list[dict[str, object]]:
        seed = hashlib.sha256(seed_value.encode("utf-8")).digest()
        generator = random.Random(int.from_bytes(seed, "big"))

        def location() -> dict[str, float]:
            return {
                axis: round(generator.uniform(-100.0, 100.0), 2)
                for axis in ("x", "y", "z")
            }

        entities: list[dict[str, object]] = [
            {
                "id": f"ship-{index}",
                "type": "ship",
                "area": "windmill area" if index <= 3 else "dock",
                "location": location(),
                "risk": round(generator.uniform(0.0, 1.0), 2),
            }
            for index in range(1, 6)
        ]
        entities.append(
            {
                "id": "drone-1",
                "type": "drone",
                "area": "windmill area",
                "location": location(),
                "max_velocity": 20,
                "fov_radius": 30,
            }
        )
        return entities

    def _risk_observation(self, command: ManeuverCommand) -> TransportEvent:
        event_id = f"risk.observed:{command.command_id}:collision"
        existing = self.transport.get_event(event_id)
        if existing is not None:
            return existing
        sequence = self.transport.next_event_sequence(
            "belief-observations", command.mission_id
        )
        observation = RiskObservation(
            event_id=event_id,
            input_revision=sequence + 1,
            risk_type="collision",
            associations=(
                EntityAssociation("ship-1", 0.7),
                EntityAssociation("ship-2", 0.2),
                EntityAssociation("ship-3", 0.1),
            ),
            likelihood_given_risk=0.85,
            likelihood_given_safe=0.15,
        )
        return self.transport.publish_event(
            "belief-observations",
            create_risk_observation_event(
                command.mission_id,
                observation,
                sequence=sequence,
            ),
        )

    def _feedback(
        self,
        command: ManeuverCommand,
        lifecycle: str,
        *,
        extra_payload: dict[str, object] | None = None,
    ) -> TransportEvent:
        feedback_id = f"maneuver-feedback:{command.command_id}:{lifecycle}"
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
                **(extra_payload or {}),
            },
        )
        event = TransportEvent(
            schema_version=feedback.schema_version,
            event_id=feedback.feedback_id,
            mission_id=feedback.mission_id,
            sequence=self.transport.next_event_sequence(
                self.feedback_topic, command.mission_id
            ),
            event_kind=feedback.event_kind,
            payload=feedback.to_dict(),
        )
        return self.transport.publish_event(self.feedback_topic, event)


__all__ = [
    "SUPPORTED_LIFECYCLES",
    "FakeEnvironment",
    "FakeEnvironmentHeartbeat",
    "FakeEnvironmentResult",
]
