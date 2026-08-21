"""Transport-persisted in-process implementation of agent communication."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from onr.contracts.communication import AgentMessage
from onr.contracts.transport import Command, CommandOutcome
from onr.ports.transport import Transport

AgentMessageHandler = Callable[[AgentMessage], object]


class TransportCommunicationPort:
    """Dispatch registered handlers synchronously with durable command identity."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._handlers: dict[str, AgentMessageHandler] = {}

    def register(self, recipient: str, handler: AgentMessageHandler) -> None:
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("communication recipient must be non-empty")
        if not callable(handler):
            raise TypeError("communication handler must be callable")
        existing = self._handlers.get(recipient)
        if existing is not None and existing is not handler:
            raise ValueError("communication recipient is already registered")
        self._handlers[recipient] = handler

    def available_recipients(self, sender: str) -> tuple[str, ...]:
        if not isinstance(sender, str) or not sender.strip():
            raise ValueError("communication sender must be non-empty")
        return tuple(sorted(recipient for recipient in self._handlers if recipient != sender))

    def request(self, message: AgentMessage) -> CommandOutcome:
        if not isinstance(message, AgentMessage):
            raise TypeError("communication request requires an AgentMessage")
        handler = self._handlers.get(message.recipient)
        if handler is None:
            raise ValueError("agent message recipient is not registered")
        command = Command(
            schema_version=1,
            command_id=message.message_id,
            correlation_id=message.correlation_id,
            mission_id=message.mission_id,
            target_service=message.recipient,
            command_kind="agent-message",
            payload=message.to_dict(),
        )
        self.transport.send_command(command)
        get_outcome = getattr(self.transport, "get_command_outcome", None)
        existing = get_outcome(command.command_id) if callable(get_outcome) else None
        if existing is not None:
            if not isinstance(existing, CommandOutcome):
                raise TypeError("communication transport returned an invalid outcome")
            return existing
        try:
            response = handler(message)
            payload = self._response_payload(response)
            status = "completed"
        except Exception as exc:  # noqa: BLE001 - correlated handler failure evidence.
            payload = {
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            status = "failed"
        outcome = CommandOutcome(
            schema_version=1,
            command_id=message.message_id,
            correlation_id=message.correlation_id,
            mission_id=message.mission_id,
            status=status,
            payload=payload,
        )
        self.transport.publish_outcome(outcome)
        return outcome

    @staticmethod
    def _response_payload(response: object) -> Mapping[str, object]:
        if isinstance(response, CommandOutcome):
            return response.payload
        to_dict = getattr(response, "to_dict", None)
        if callable(to_dict):
            response = to_dict()
        if not isinstance(response, Mapping):
            raise TypeError("agent communication handler must return a JSON object")
        return dict(response)


__all__ = ["AgentMessageHandler", "TransportCommunicationPort"]
