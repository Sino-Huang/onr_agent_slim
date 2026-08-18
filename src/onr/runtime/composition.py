"""Minimal runtime composition without mission-authority state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.planning_commands import PlanningCommandHandler
from onr.contracts.transport import Command, CommandOutcome
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
