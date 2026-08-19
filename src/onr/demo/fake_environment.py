"""Packaged deterministic demo environment built on the public transport seam.

This module emits external environment evidence for demonstrations. It does not
apply maneuver feedback to mission state and is not production authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import random
from urllib.parse import quote

from onr.adapters.file_transport import FileTransport
from onr.contracts.context_coordination import create_source_fact_event
from onr.contracts.fsm import ManeuverFeedback
from onr.contracts.maneuver_control import ManeuverCommand
from onr.contracts.transport import Command, TransportEvent
from onr.ports.transport import Subscription


SUPPORTED_LIFECYCLES = ("accepted", "active", "completed", "failed", "cancelled")


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
    scene_graph: TransportEvent
    source_fact: TransportEvent
    feedback: TransportEvent
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
        scene_graph_topic: str = "operational-scene-graph",
        context_topic: str = "normalized-plans",
        max_retries: int = 3,
        output_root: Path | str | None = None,
    ) -> None:
        if not isinstance(transport, FileTransport):
            raise TypeError("FakeEnvironment requires a FileTransport")
        self.transport = transport
        self.mission_id = mission_id
        self.target_service = target_service
        self.command_topic = command_topic
        self.feedback_topic = feedback_topic
        self.scene_graph_topic = scene_graph_topic
        self.context_topic = context_topic
        self.output_root = (
            Path(output_root)
            if output_root is not None
            else self.transport.root.parent / "environment"
        )
        self.subscription = Subscription(target_service, mission_id, command_topic, max_retries)
        self._results: dict[tuple[str, str], FakeEnvironmentResult] = {}
        self._scene_facts: dict[str, tuple[TransportEvent, TransportEvent]] = {}
        self.last_result: FakeEnvironmentResult | None = None
        self.last_output_path: Path | None = None

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

    def run_once(self, lifecycle: str = "completed") -> FakeEnvironmentResult | None:
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
        self, command: ManeuverCommand, lifecycle: str = "completed"
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

        scene_graph, source_fact, environment_file = self._scene_graph_and_fact(command)
        feedback = self._feedback(command, lifecycle)
        result = FakeEnvironmentResult(command, scene_graph, source_fact, feedback, environment_file)
        self._results[key] = result
        self.last_result = result
        return result

    def _scene_graph_and_fact(
        self, command: ManeuverCommand
    ) -> tuple[TransportEvent, TransportEvent, Path]:
        environment_file = self.output_root / quote(self.mission_id, safe="._-") / "scene.json"
        self.last_output_path = environment_file
        environment = {
            "mission_id": command.mission_id,
            "plan_revision": command.plan_revision,
            "command_id": command.command_id,
            "entities": self._entities(command),
        }
        _atomic_write_json(environment_file, environment)
        graph = {
            "mission_id": command.mission_id,
            "plan_revision": command.plan_revision,
            "maneuvers": [
                {
                    "maneuver_id": command.maneuver_id,
                    "action": command.action,
                    "parameters": {
                        parameter.name: parameter.value for parameter in command.parameters
                    },
                }
            ],
            "entities": environment["entities"],
            "environment_file": str(environment_file),
        }
        graph_json = json.dumps(
            graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        reference = hashlib.sha256(graph_json.encode("utf-8")).hexdigest()
        cached = self._scene_facts.get(reference)
        if cached is not None:
            return (*cached, environment_file)

        scene_event_id = f"operational-scene-graph:{command.mission_id}:{reference}"
        source_fact_id = (
            f"source-fact:{command.mission_id}:operational_scene_graph:{reference}"
        )
        scene_event = self.transport.get_event(scene_event_id)
        source_fact = self.transport.get_event(source_fact_id)
        if scene_event is None:
            scene_event = self.transport.publish_event(
                self.scene_graph_topic,
                TransportEvent(
                    schema_version=1,
                    event_id=scene_event_id,
                    mission_id=command.mission_id,
                    sequence=self.transport.next_event_sequence(
                        self.scene_graph_topic, command.mission_id
                    ),
                    event_kind="operational_scene_graph",
                    payload={
                        "source": "operational_scene_graph",
                        "revision": command.plan_revision,
                        "reference": scene_event_id,
                        "graph": json.loads(graph_json),
                    },
                ),
            )
        if source_fact is None:
            source_fact = self.transport.publish_event(
                self.context_topic,
                create_source_fact_event(
                    command.mission_id,
                    "operational_scene_graph",
                    command.plan_revision,
                    event_id=source_fact_id,
                    sequence=self.transport.next_event_sequence(
                        self.context_topic, command.mission_id
                    ),
                    reference=scene_event_id,
                ),
            )
        self._scene_facts[reference] = (scene_event, source_fact)
        return scene_event, source_fact, environment_file

    def _entities(self, command: ManeuverCommand) -> list[dict[str, object]]:
        seed = hashlib.sha256(
            f"{command.mission_id}:{command.command_id}:{command.plan_revision}:{command.maneuver_id}".encode(
                "utf-8"
            )
        ).digest()
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
            }
            for index in range(1, 6)
        ]
        entities.append(
            {
                "id": "drone-1",
                "type": "drone",
                "area": "windmill area",
                "location": location(),
            }
        )
        return entities

    def _feedback(self, command: ManeuverCommand, lifecycle: str) -> TransportEvent:
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
    "FakeEnvironment",
    "FakeEnvironmentResult",
    "SUPPORTED_LIFECYCLES",
]
