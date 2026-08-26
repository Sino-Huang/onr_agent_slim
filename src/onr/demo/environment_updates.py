"""Fake-environment update ownership adapters."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from threading import Condition, Event, Lock, Thread
from time import monotonic
from typing import Any

from onr.contracts.environment import EnvironmentTickResult
from onr.contracts.maneuver_control import ManeuverCommand
from onr.contracts.transport import Command, TransportEvent
from onr.demo.fake_environment import FakeEnvironment
from onr.ports.environment import EnvironmentPlanningView


class CoordinatorDrivenFakeEnvironment:
    """Consume commands and advance only when Context Coordination requests it."""

    update_ownership = "coordinator_driven"

    def __init__(self, environment: FakeEnvironment, *, cadence_seconds: float) -> None:
        self.environment = environment
        self.mission_id = environment.mission_id
        self.feedback_topic = environment.feedback_topic
        self.perception_topic = environment.perception_topic
        self.environment_topic = environment.environment_topic
        self.cadence_seconds = float(cadence_seconds)
        self._consumer: Any | None = None
        self._updates: deque[EnvironmentTickResult] = deque()
        self._condition = Condition(Lock())

    @property
    def current_time(self) -> float:
        return self.environment.current_time

    @property
    def latest_environment_event(self) -> TransportEvent | None:
        return self.environment.latest_environment_event

    @property
    def has_current_maneuver(self) -> bool:
        return self.environment.has_current_maneuver

    @property
    def event_report(self) -> tuple[Mapping[str, object], ...]:
        return self.environment.event_report

    @property
    def is_alive(self) -> bool:
        return False

    def start(self, *, simulation_limit_seconds: float | None = None) -> None:
        _ = simulation_limit_seconds
        if self._consumer is None:
            self._consumer = self.environment.transport.open_consumer(
                self.environment.subscription
            )

    def consume_commands(self) -> tuple[TransportEvent, ...]:
        """Apply every available command and acknowledge only successful delivery."""

        self.start()
        consumer = self._consumer
        if consumer is None:
            raise RuntimeError("environment command consumer did not start")
        feedback: list[TransportEvent] = []
        while True:
            delivery = consumer.receive()
            if delivery is None:
                return tuple(feedback)
            if not isinstance(delivery.message, Command):
                delivery.ack()
                continue
            try:
                command = ManeuverCommand.from_command(
                    delivery.message, self.environment.command_topic
                )
                result = self.environment.apply_command(command)
            except Exception:  # noqa: BLE001 - delivery failure must be retried.
                delivery.nack()
                continue
            delivery.ack()
            feedback.extend(result.feedback_events or (result.feedback,))

    def advance(self) -> EnvironmentTickResult:
        command_feedback = self.consume_commands()
        tick = self.environment.tick()
        combined = EnvironmentTickResult(
            current_time=tick.current_time,
            environment_data=tick.environment_data,
            feedback_events=command_feedback + tick.feedback_events,
            perception_events=tick.perception_events,
        )
        with self._condition:
            self._updates.append(combined)
            self._condition.notify_all()
        return combined

    def drain_updates(self) -> tuple[EnvironmentTickResult, ...]:
        with self._condition:
            result = tuple(self._updates)
            self._updates.clear()
            return result

    def wait_for_update(self, timeout: float | None = None) -> bool:
        with self._condition:
            if self._updates:
                return True
            self._condition.wait(timeout)
            return bool(self._updates)

    def planning_view(self) -> EnvironmentPlanningView:
        return self.environment.heartbeat()

    def stop(self) -> None:
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            consumer.close()
        with self._condition:
            self._condition.notify_all()

    def join(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None


class EnvironmentDrivenFakeEnvironment(CoordinatorDrivenFakeEnvironment):
    """Advance the fake environment on wall-clock cadence in one producer thread."""

    update_ownership = "environment_driven"

    def __init__(self, environment: FakeEnvironment, *, cadence_seconds: float) -> None:
        super().__init__(environment, cadence_seconds=cadence_seconds)
        self._stop = Event()
        self._thread: Thread | None = None
        self._failure: BaseException | None = None
        self._simulation_limit_seconds: float | None = None

    def start(self, *, simulation_limit_seconds: float | None = None) -> None:
        if self._thread is not None:
            return
        super().start(simulation_limit_seconds=simulation_limit_seconds)
        self._simulation_limit_seconds = simulation_limit_seconds
        self._stop.clear()
        self._thread = Thread(
            target=self._run,
            name=f"fake-environment-updates-{self.mission_id}",
            daemon=False,
        )
        self._thread.start()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def advance(self) -> EnvironmentTickResult:
        raise RuntimeError(
            "environment-driven updates cannot be advanced by coordinator"
        )

    def _produce_update(self) -> EnvironmentTickResult:
        return super().advance()

    def _run(self) -> None:
        deadline = monotonic() + self.cadence_seconds
        try:
            while not self._stop.wait(max(0.0, deadline - monotonic())):
                update = self._produce_update()
                limit = self._simulation_limit_seconds
                if limit is not None and update.current_time >= limit:
                    self._stop.set()
                    break
                deadline += self.cadence_seconds
        except Exception as exc:  # noqa: BLE001 - surface producer failure to owner.
            self._failure = exc
            self._stop.set()
        finally:
            consumer, self._consumer = self._consumer, None
            if consumer is not None:
                consumer.close()
            with self._condition:
                self._condition.notify_all()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

    def join(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.join()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("environment update producer failed") from self._failure


CoordinatorDrivenEnvironmentUpdateSource = CoordinatorDrivenFakeEnvironment
EnvironmentDrivenEnvironmentUpdateSource = EnvironmentDrivenFakeEnvironment

__all__ = [
    "CoordinatorDrivenEnvironmentUpdateSource",
    "CoordinatorDrivenFakeEnvironment",
    "EnvironmentDrivenEnvironmentUpdateSource",
    "EnvironmentDrivenFakeEnvironment",
]
