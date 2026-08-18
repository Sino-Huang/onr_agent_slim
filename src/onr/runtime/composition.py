"""Minimal runtime composition without mission-authority state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from onr.adapters.file_transport import FileTransport
from onr.adapters.fsm_store import JsonFSMStateStore
from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.fsm import FSMRunner
from onr.application.maneuver_control import ManeuverControl
from onr.application.planning_commands import PlanningCommandHandler
from onr.contracts.transport import Command, CommandOutcome
from onr.ports.maneuver import ManeuverAdapter
from onr.ports.transport import Subscription
from onr.runtime.config import RuntimeConfig, load_runtime_config


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """Validated configuration and the selected transport adapter."""

    config: RuntimeConfig
    transport: FileTransport | InProcessTransport

    @classmethod
    def create(
        cls,
        *,
        repo_root: Path,
        config_path: Path | None = None,
        subscriptions: tuple[Subscription, ...] = (),
    ) -> "RuntimeComposition":
        config = load_runtime_config(config_path, repo_root=repo_root)
        if config.transport.backend == "file":
            transport = FileTransport(config.transport.root, subscriptions)
        else:
            transport = InProcessTransport(subscriptions)
        return cls(config=config, transport=transport)

    def run_planning_command(
        self,
        command: Command,
        planner: object,
        *,
        topic: str = "normalized-plans",
    ) -> CommandOutcome:
        """Deliver one command through the configured transport and subscriber."""

        subscription = Subscription(
            service_id=command.target_service,
            mission_id=command.mission_id,
            topic=command.command_kind,
        )
        consumer = self.transport.open_consumer(subscription)
        try:
            self.transport.send_command(command)
            handler = PlanningCommandHandler(self.transport, planner, topic=topic)
            outcome = handler.run_once(consumer)
            if outcome is None:
                raise RuntimeError("planning command was not delivered")
            return outcome
        finally:
            consumer.close()

    def create_fsm_runner(
        self,
        *,
        mission_id: str,
        clock: Callable[[], int | float] | None = None,
    ) -> FSMRunner:
        """Compose the pure FSM service with the selected transport and JSON store."""

        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission ID must be a non-empty string")
        subscription = FSMRunner.subscription_for(
            mission_id,
            service_id=self.config.services.fsm_runner,
        )
        if subscription not in self.transport.subscriptions:
            self.transport.subscriptions = self.transport.subscriptions + (subscription,)
        return FSMRunner(
            self.transport,
            store=JsonFSMStateStore(self.config.storage.root / "fsm" / mission_id),
            clock=clock,
            subscription=subscription,
        )

    def create_maneuver_control(
        self,
        adapter: ManeuverAdapter,
        decision_provider: object,
        *,
        target_service: str = "maneuver-adapter",
    ) -> ManeuverControl:
        """Compose Maneuver Control without introducing runtime authority state."""

        return ManeuverControl(
            cast(Any, self.transport),
            adapter,
            decision_provider,
            target_service=target_service,
        )



def create_runtime(
    *,
    repo_root: Path,
    config_path: Path | None = None,
    subscriptions: tuple[Subscription, ...] = (),
) -> RuntimeComposition:
    return RuntimeComposition.create(
        repo_root=repo_root,
        config_path=config_path,
        subscriptions=subscriptions,
    )
