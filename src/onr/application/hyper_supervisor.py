"""Serialized, per-Mission Hyper supervision over latest-only runtime evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import RLock
from typing import Any, cast

from onr.contracts.communication import AgentMessage, AgentMessageKind
from onr.contracts.hyper_agent import (
    HyperHeartbeatDecision,
    HyperHeartbeatInvocation,
    ReplanRequest,
)
from onr.contracts.transport import TransportEvent
from onr.ports.operational_log import OperationalLog


class HyperSupervisor:
    """Queue Maneuver requests and evaluate one coalesced heartbeat at a time."""

    def __init__(
        self,
        provider: object,
        *,
        transport: object | None = None,
        outcome_topic: str = "hyper-heartbeat-outcomes",
        operational_log: OperationalLog | None = None,
    ) -> None:
        if not callable(provider) and not callable(getattr(provider, "decide", None)):
            raise TypeError(
                "Hyper supervisor provider must be callable or expose decide"
            )
        self.provider = provider
        self.transport = transport
        self.outcome_topic = outcome_topic
        self.operational_log = operational_log
        self._pending: dict[str, list[ReplanRequest]] = {}
        self._seen: dict[str, set[str]] = {}
        self._locks: dict[str, RLock] = {}

    def queue_replan(self, request: ReplanRequest) -> ReplanRequest:
        if not isinstance(request, ReplanRequest):
            raise TypeError("Hyper supervisor requires a ReplanRequest")
        with self._locks.setdefault(request.mission_id, RLock()):
            seen = self._seen.setdefault(request.mission_id, set())
            if request.request_id in seen:
                return request
            self._pending.setdefault(request.mission_id, []).append(request)
            seen.add(request.request_id)
        return request

    def handle_agent_message(self, message: AgentMessage) -> Mapping[str, object]:
        """Queue a Maneuver request; semantic evaluation occurs after its heartbeat."""

        if not isinstance(message, AgentMessage):
            raise TypeError("Hyper communication requires AgentMessage")
        if message.recipient != "hyper-agent":
            raise ValueError("Hyper communication recipient does not match")
        if message.kind is not AgentMessageKind.REPLAN:
            return {
                "status": "queued",
                "message_id": message.message_id,
                "kind": str(message.kind),
            }
        raw = message.payload.get("replan_request")
        if not isinstance(raw, Mapping):
            raise TypeError("Hyper replan message lacks ReplanRequest")
        request = ReplanRequest.from_dict(cast(Mapping[str, Any], raw))
        if (
            request.request_id != message.message_id
            or request.mission_id != message.mission_id
            or request.observed_plan_revision != message.plan_revision
        ):
            raise ValueError("Hyper replan request does not match its envelope")
        self.queue_replan(request)
        return {
            "status": "queued",
            "request_id": request.request_id,
            "disposition": "pending_hyper_evaluation",
        }

    def has_pending(self, mission_id: str) -> bool:
        with self._locks.setdefault(mission_id, RLock()):
            return bool(self._pending.get(mission_id))

    def pending_request_identities(self, mission_id: str) -> tuple[str, ...]:
        with self._locks.setdefault(mission_id, RLock()):
            return tuple(
                sorted(
                    request.request_id for request in self._pending.get(mission_id, ())
                )
            )

    def heartbeat(
        self,
        invocation: HyperHeartbeatInvocation,
    ) -> HyperHeartbeatDecision:
        """Run one serialized stateless evaluation and durably publish its outcome."""

        if not isinstance(invocation, HyperHeartbeatInvocation):
            raise TypeError("Hyper supervisor heartbeat requires its invocation")
        mission_id = invocation.mission_id
        lock = self._locks.setdefault(mission_id, RLock())
        with lock:
            pending = tuple(self._pending.get(mission_id, ()))
            coalesced = self._coalesce(pending, mission_id)
            requests = invocation.maneuver_requests
            if coalesced is not None:
                requests = requests + (coalesced,)
            effective = HyperHeartbeatInvocation(
                mission_id=invocation.mission_id,
                plan_revision=invocation.plan_revision,
                trigger_identities=invocation.trigger_identities,
                mission_snapshot=invocation.mission_snapshot,
                planner_plan_reference=invocation.planner_plan_reference,
                statechart_reference=invocation.statechart_reference,
                fsm_status=invocation.fsm_status,
                environment_data=invocation.environment_data,
                belief_snapshot=invocation.belief_snapshot,
                maneuver_requests=requests,
            )
            decision = self._decide(effective)
            if (
                decision.mission_id != mission_id
                or decision.plan_revision != invocation.plan_revision
            ):
                raise ValueError("Hyper provider returned an inconsistent decision")
            if pending:
                identities = {item.request_id for item in pending}
                self._pending[mission_id] = [
                    item
                    for item in self._pending.get(mission_id, ())
                    if item.request_id not in identities
                ]
            self._publish(decision)
            return decision

    def _decide(self, invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        decide = getattr(self.provider, "decide", None)
        raw = (
            decide(invocation)
            if callable(decide)
            else cast(Callable[[HyperHeartbeatInvocation], object], self.provider)(
                invocation
            )
        )
        if isinstance(raw, HyperHeartbeatDecision):
            return raw
        if isinstance(raw, Mapping):
            candidate = dict(raw)
            candidate.setdefault("trigger_identities", invocation.trigger_identities)
            candidate.setdefault(
                "request_identities",
                tuple(
                    identity
                    for request in invocation.maneuver_requests
                    for identity in (
                        request.coalesced_request_ids or (request.request_id,)
                    )
                ),
            )
            return HyperHeartbeatDecision.from_dict(candidate)
        raise TypeError("Hyper provider did not return HyperHeartbeatDecision")

    def _publish(self, decision: HyperHeartbeatDecision) -> None:
        if self.transport is not None:
            next_sequence = getattr(self.transport, "next_event_sequence", None)
            publish = getattr(self.transport, "publish_event", None)
            if not callable(next_sequence) or not callable(publish):
                raise TypeError("Hyper supervisor transport is invalid")
            sequence = cast(int, next_sequence(self.outcome_topic, decision.mission_id))
            publish(
                self.outcome_topic,
                TransportEvent(
                    schema_version=1,
                    event_id=(
                        f"hyper-heartbeat:{decision.mission_id}:"
                        f"{decision.plan_revision}:{sequence}"
                    ),
                    mission_id=decision.mission_id,
                    sequence=sequence,
                    event_kind="hyper-heartbeat-decision",
                    payload=decision.to_dict(),
                ),
            )
        if self.operational_log is not None:
            self.operational_log.emit(
                decision.mission_id,
                "hyper-agent",
                "heartbeat",
                str(decision.disposition),
                details={
                    "plan_revision": decision.plan_revision,
                    "trigger_identities": decision.trigger_identities,
                    "request_identities": decision.request_identities,
                },
            )

    @staticmethod
    def _coalesce(
        requests: tuple[ReplanRequest, ...], mission_id: str
    ) -> ReplanRequest | None:
        if not requests:
            return None
        latest = max(
            requests,
            key=lambda item: (item.observed_plan_revision, item.request_id),
        )
        revisions: dict[str, int | None] = {}
        for request in requests:
            for source, revision in request.source_revisions.items():
                previous = revisions.get(source)
                if revision is not None and (previous is None or revision > previous):
                    revisions[source] = revision
                elif source not in revisions:
                    revisions[source] = None
        return ReplanRequest(
            request_id=latest.request_id,
            mission_id=mission_id,
            reason=latest.reason,
            requester=latest.requester,
            observed_plan_revision=latest.observed_plan_revision,
            source_revisions=revisions,
            coalesced_request_ids=tuple(sorted({item.request_id for item in requests})),
            coalesced_reasons=tuple(sorted({item.reason for item in requests})),
        )


__all__ = ["HyperSupervisor"]
