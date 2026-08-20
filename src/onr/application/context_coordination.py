"""Transport-facing Context Coordination application service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, cast

from onr.contracts.context_coordination import (
    MISSION_SNAPSHOT_SOURCES,
    MissionSnapshot,
    create_source_fact_event,
    mission_snapshot_from_transport_event,
    mission_snapshot_to_transport_event,
    normalize_source_name,
)
from onr.contracts.transport import (
    NormalizedPlanTransportEvent,
    TransportEvent,
    normalized_plan_transport_event_to_wire,
)
from onr.ports.operational_log import OperationalLog
from onr.ports.transport import Subscription


@dataclass(frozen=True, slots=True)
class _SourceFact:
    revision: int | None
    reference: str | None
    content_hash: str | None
    health: str
    fresh: bool


class ContextCoordination:
    """Assemble changed authoritative references into versioned snapshots."""

    def __init__(
        self,
        transport: Any,
        mission_id: str,
        *,
        input_topic: str = "normalized-plans",
        snapshot_topic: str = "mission-snapshots",
        service_id: str = "context-coordination",
        max_retries: int = 3,
        clock: Callable[[], str] | None = None,
        subscription: Subscription | None = None,
        operational_log: OperationalLog | None = None,
    ) -> None:
        self._transport = transport
        self.input_topic = input_topic
        self.snapshot_topic = snapshot_topic
        self._clock = clock or _utc_now
        self.operational_log = operational_log
        self.subscription = subscription or Subscription(
            service_id=service_id,
            mission_id=mission_id,
            topic=input_topic,
            max_retries=max_retries,
        )
        if self.subscription.mission_id != mission_id:
            raise ValueError("context coordination subscription mission ID does not match")
        self._facts: dict[str, _SourceFact] = {}
        self._last_snapshot: MissionSnapshot | None = None
        self._restore_latest_snapshot()

    @staticmethod
    def subscription_for(
        mission_id: str,
        *,
        input_topic: str = "normalized-plans",
        service_id: str = "context-coordination",
        max_retries: int = 3,
    ) -> Subscription:
        """Build the static subscription that must be registered on an adapter."""

        return Subscription(service_id, mission_id, input_topic, max_retries)

    def handle(
        self, event: TransportEvent | NormalizedPlanTransportEvent
    ) -> MissionSnapshot | None:
        """Consume one normalized-plan or source-fact event."""

        if isinstance(event, NormalizedPlanTransportEvent):
            event = normalized_plan_transport_event_to_wire(event)
        if not isinstance(event, TransportEvent) or event.mission_id != self.subscription.mission_id:
            return None
        fact = self._parse_context_event(event)
        if fact is None:
            return None
        source, next_fact = fact
        previous = self._facts.get(source)
        if next_fact.revision is None:
            if previous is None:
                raise _MalformedContextEvent("source fact is missing its initial revision")
            next_fact = _SourceFact(
                previous.revision,
                previous.reference if next_fact.reference is None else next_fact.reference,
                previous.content_hash if next_fact.content_hash is None else next_fact.content_hash,
                next_fact.health,
                next_fact.fresh,
            )
        else:
            if previous is not None and previous.revision is not None:
                if next_fact.revision < previous.revision:
                    return None
                if (
                    source == "bayesian_belief_snapshot"
                    and next_fact.revision == previous.revision
                    and (
                        (
                            next_fact.reference is not None
                            and next_fact.reference != previous.reference
                        )
                        or (
                            next_fact.content_hash is not None
                            and next_fact.content_hash != previous.content_hash
                        )
                    )
                ):
                    raise _MalformedContextEvent(
                        "belief revision cannot change reference or hash provenance"
                    )
                if next_fact.reference is None or next_fact.content_hash is None:
                    next_fact = _SourceFact(
                        next_fact.revision,
                        previous.reference if next_fact.reference is None else next_fact.reference,
                        previous.content_hash if next_fact.content_hash is None else next_fact.content_hash,
                        next_fact.health,
                        next_fact.fresh,
                    )
        if previous == next_fact:
            return None
        self._facts[source] = next_fact
        snapshot = self._snapshot()
        sequence = self._transport.next_event_sequence(self.snapshot_topic, snapshot.mission_id)
        self._transport.publish_event(
            self.snapshot_topic,
            mission_snapshot_to_transport_event(
                snapshot,
                event_id=f"mission-snapshot:{snapshot.mission_id}:{snapshot.version}",
                sequence=sequence,
            ),
        )
        self._last_snapshot = snapshot
        if self.operational_log is not None:
            self.operational_log.emit(
                snapshot.mission_id,
                "context-coordination",
                "heartbeat",
                "completed",
                details={"operation": "publish_snapshot", "revision": snapshot.version},
            )
        return snapshot

    handle_event = handle

    def run_once(self, consumer: Any) -> MissionSnapshot | None:
        """Deliver one event through a registered Consumer, like planning commands."""

        delivery = consumer.receive()
        if delivery is None:
            return None
        if not isinstance(delivery.message, (TransportEvent, NormalizedPlanTransportEvent)):
            delivery.ack()
            return None
        try:
            snapshot = self.handle(delivery.message)
        except _MalformedContextEvent as exc:
            delivery.nack()
            if self.operational_log is not None:
                self.operational_log.emit(
                    self.subscription.mission_id,
                    "context-coordination",
                    "error",
                    "failed",
                    details={"operation": "consume_event", "error_type": type(exc).__name__},
                )
            return None
        delivery.ack()
        return snapshot

    def publish_source_fact(
        self,
        source: str,
        revision: int,
        *,
        reference: str | None = None,
        health: str | bool = "healthy",
        fresh: bool = True,
        content_sha256: str | None = None,
    ) -> TransportEvent:
        """Publish a revision/health fact for a non-plan authority source."""

        source = normalize_source_name(source)
        if source == "plan":
            raise ValueError("the plan source is published by normalized-plan events")
        sequence = self._transport.next_event_sequence(self.input_topic, self.subscription.mission_id)
        event = create_source_fact_event(
            self.subscription.mission_id,
            source,
            revision,
            event_id=f"source-fact:{self.subscription.mission_id}:{source}:{sequence}",
            sequence=sequence,
            reference=reference,
            health=health,
            fresh=fresh,
            content_sha256=content_sha256,
        )
        return self._transport.publish_event(self.input_topic, event)

    publish_source_revision = publish_source_fact
    publish_source_health = publish_source_fact

    def _parse_context_event(self, event: TransportEvent) -> tuple[str, _SourceFact] | None:
        payload = event.payload
        if event.event_kind in {"normalized-plan", "normalized_plan"}:
            required_fields = {
                "mission_snapshot_id",
                "plan_revision",
                "planner_choice",
                "source_authority",
                "outcome",
                "normalized_plan",
                "normalized_plan_document",
                "normalized_plan_sha256",
            }
            if not required_fields.issubset(payload):
                raise _MalformedContextEvent(
                    "normalized-plan event is missing required wire fields"
                )
            revision = payload.get("plan_revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise _MalformedContextEvent("normalized-plan event has an invalid plan revision")
            reference = payload.get("normalized_plan_sha256", event.event_id)
            if reference is not None and not isinstance(reference, str):
                raise _MalformedContextEvent("normalized-plan event has an invalid reference")
            content_hash = payload.get("normalized_plan_sha256")
            if not isinstance(content_hash, str):
                raise _MalformedContextEvent("normalized-plan event has an invalid content hash")
            return "plan", _SourceFact(revision, reference, content_hash, "healthy", True)
        if event.event_kind == "belief.updated":
            required = {
                "source",
                "revision",
                "reference",
                "content_sha256",
                "health",
                "fresh",
            }
            if set(payload) != required or payload.get("source") != "bayesian_belief_snapshot":
                raise _MalformedContextEvent("belief.updated event has invalid public fields")
            revision = payload.get("revision")
            reference = payload.get("reference")
            content_hash = payload.get("content_sha256")
            health = payload.get("health")
            fresh = payload.get("fresh")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise _MalformedContextEvent("belief.updated event has an invalid revision")
            if not isinstance(reference, str) or not reference.strip():
                raise _MalformedContextEvent("belief.updated event has an invalid reference")
            if (
                not isinstance(content_hash, str)
                or len(content_hash) != 64
                or any(character not in "0123456789abcdef" for character in content_hash)
                or not reference.endswith(f"#sha256={content_hash}")
            ):
                raise _MalformedContextEvent("belief.updated event has an invalid content hash")
            if not isinstance(health, str) or not health.strip() or not isinstance(fresh, bool):
                raise _MalformedContextEvent("belief.updated event has invalid health or freshness")
            return "bayesian_belief_snapshot", _SourceFact(
                revision, reference, content_hash, health, fresh
            )
        if event.event_kind not in {
            "source-fact",
            "source-revision",
            "source-health",
            "operational_scene_graph",
            "bayesian_belief_snapshot",
            "fsm_status",
            "active_maneuver",
            "operational-scene-graph",
            "bayesian-belief-snapshot",
            "fsm-status",
            "active-maneuver",
        }:
            return None
        raw_source = payload.get("source", event.event_kind)
        try:
            source = normalize_source_name(raw_source)
        except ValueError:
            raise _MalformedContextEvent("source fact has an invalid source") from None
        raw_revision = payload.get("revision", payload.get("source_revision"))
        if raw_revision is not None and (
            isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 0
        ):
            raise _MalformedContextEvent("source fact has an invalid revision")
        reference = payload.get("reference", payload.get("source_reference"))
        if reference is not None and not isinstance(reference, str):
            raise _MalformedContextEvent("source fact has an invalid reference")
        content_hash = payload.get("content_sha256", payload.get("source_hash"))
        if content_hash is not None and (
            not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
        ):
            raise _MalformedContextEvent("source fact has an invalid content hash")
        raw_health = payload.get("health", "healthy")
        if isinstance(raw_health, bool):
            health = "healthy" if raw_health else "unhealthy"
        elif isinstance(raw_health, str) and raw_health.strip():
            health = raw_health
        else:
            raise _MalformedContextEvent("source fact has an invalid health")
        fresh = payload.get("fresh", payload.get("freshness", True))
        if not isinstance(fresh, bool):
            raise _MalformedContextEvent("source fact has invalid freshness")
        return source, _SourceFact(raw_revision, reference, content_hash, health, fresh)

    def _restore_latest_snapshot(self) -> None:
        latest_event = getattr(self._transport, "latest_event", None)
        if not callable(latest_event):
            return
        event = latest_event(
            self.snapshot_topic,
            self.subscription.mission_id,
            event_kind="mission-snapshot",
        )
        if event is None:
            return
        if not isinstance(event, TransportEvent):
            return
        try:
            snapshot = mission_snapshot_from_transport_event(cast(TransportEvent, event))
        except ValueError:
            return
        self._last_snapshot = snapshot
        for source in MISSION_SNAPSHOT_SOURCES:
            revision = snapshot.source_revisions[source]
            if revision is not None:
                self._facts[source] = _SourceFact(
                    revision,
                    snapshot.source_references[source],
                    snapshot.source_hashes[source],
                    snapshot.source_health[source],
                    snapshot.source_freshness[source],
                )

    def _snapshot(self) -> MissionSnapshot:
        revisions = {
            source: self._facts[source].revision if source in self._facts else None
            for source in MISSION_SNAPSHOT_SOURCES
        }
        references = {
            source: self._facts[source].reference if source in self._facts else None
            for source in MISSION_SNAPSHOT_SOURCES
        }
        hashes = {
            source: self._facts[source].content_hash if source in self._facts else None
            for source in MISSION_SNAPSHOT_SOURCES
        }
        health = {
            source: self._facts[source].health if source in self._facts else "missing"
            for source in MISSION_SNAPSHOT_SOURCES
        }
        freshness = {
            source: self._facts[source].fresh if source in self._facts else False
            for source in MISSION_SNAPSHOT_SOURCES
        }
        plan_revision = revisions["plan"]
        return MissionSnapshot(
            mission_id=self.subscription.mission_id,
            version=self._next_version(),
            created_at=self._clock(),
            plan_revision=plan_revision,
            plan_reference=references["plan"],
            operational_scene_graph=references["operational_scene_graph"],
            bayesian_belief_snapshot=references["bayesian_belief_snapshot"],
            fsm_status=references["fsm_status"],
            active_maneuver=references["active_maneuver"],
            source_revisions=revisions,
            source_references=references,
            source_hashes=hashes,
            source_health=health,
            source_freshness=freshness,
        )

    def _next_version(self) -> int:
        next_sequence = self._transport.next_event_sequence(
            self.snapshot_topic, self.subscription.mission_id
        )
        return max(next_sequence + 1, (self._last_snapshot.version + 1) if self._last_snapshot else 1)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class _MalformedContextEvent(ValueError):
    """A recognized event whose fact cannot be safely applied."""


ContextCoordinationHandler = ContextCoordination
