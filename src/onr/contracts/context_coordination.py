"""Immutable contracts for Context Coordination manifests and source facts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from onr.contracts.transport import TransportEvent


MISSION_SNAPSHOT_SOURCES = (
    "plan",
    "operational_scene_graph",
    "bayesian_belief_snapshot",
    "fsm_status",
    "active_maneuver",
)
_REFERENCE_FIELDS = {
    "operational_scene_graph": "operational_scene_graph",
    "bayesian_belief_snapshot": "bayesian_belief_snapshot",
    "fsm_status": "fsm_status",
    "active_maneuver": "active_maneuver",
}
_SOURCE_ALIASES = {
    "scene_graph": "operational_scene_graph",
    "operational-scene-graph": "operational_scene_graph",
    "belief": "bayesian_belief_snapshot",
    "bayesian-belief-snapshot": "bayesian_belief_snapshot",
    "fsm": "fsm_status",
    "fsm-status": "fsm_status",
    "maneuver": "active_maneuver",
    "active-maneuver": "active_maneuver",
}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _reference(value: object, label: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{label} must be a non-empty string or null")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_source_name(value: object) -> str:
    """Return the stable source name used in a Mission Snapshot."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("source name must be a non-empty string")
    source = _SOURCE_ALIASES.get(value, value)
    if source not in MISSION_SNAPSHOT_SOURCES:
        raise ValueError("unknown Mission Snapshot source")
    return source


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class MissionSnapshot:
    """Immutable manifest of references to authoritative mission facts.

    The four world-state fields and the plan field are references, not authority
    payloads.  ``None`` references, ``missing_sources``, and the per-source
    health/freshness maps make an incomplete manifest explicit.
    """

    mission_id: str
    version: int
    created_at: str
    plan_revision: int | None = None
    plan_reference: str | None = None
    operational_scene_graph: str | None = None
    bayesian_belief_snapshot: str | None = None
    fsm_status: str | None = None
    active_maneuver: str | None = None
    source_revisions: Mapping[str, int | None] = field(default_factory=dict)
    source_references: Mapping[str, str | None] = field(default_factory=dict)
    source_health: Mapping[str, str] = field(default_factory=dict)
    source_freshness: Mapping[str, bool] = field(default_factory=dict)
    missing_sources: tuple[str, ...] = ()
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        _text(self.mission_id, "mission ID")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("snapshot version must be a positive integer")
        _text(self.created_at, "snapshot creation time")
        if self.plan_revision is not None and (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 0
        ):
            raise ValueError("plan revision must be a non-negative integer or null")

        references: dict[str, str | None] = {
            source: None for source in MISSION_SNAPSHOT_SOURCES
        }
        for source, value in self.source_references.items():
            if source not in references:
                raise ValueError("source references contain an unknown source")
            references[source] = _reference(value, f"{source} reference")
        references["plan"] = _reference(
            self.plan_reference if self.plan_reference is not None else references["plan"],
            "plan reference",
        )
        for source, field_name in _REFERENCE_FIELDS.items():
            direct = getattr(self, field_name)
            if direct is not None:
                references[source] = _reference(direct, f"{source} reference")
            object.__setattr__(self, field_name, references[source])
        object.__setattr__(self, "plan_reference", references["plan"])
        object.__setattr__(self, "source_references", MappingProxyType(references))

        revisions: dict[str, int | None] = {
            source: None for source in MISSION_SNAPSHOT_SOURCES
        }
        for source, revision in self.source_revisions.items():
            if source not in revisions:
                raise ValueError("source revisions contain an unknown source")
            if revision is not None and (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise ValueError("source revisions must be non-negative integers or null")
            revisions[source] = revision
        if self.plan_revision is not None and revisions["plan"] not in (None, self.plan_revision):
            raise ValueError("plan revision must match the plan source revision")
        if revisions["plan"] is None:
            revisions["plan"] = self.plan_revision
        elif self.plan_revision is None:
            object.__setattr__(self, "plan_revision", revisions["plan"])
        object.__setattr__(self, "source_revisions", MappingProxyType(revisions))

        health: dict[str, str] = {source: "missing" for source in MISSION_SNAPSHOT_SOURCES}
        for source, value in self.source_health.items():
            if source not in health:
                raise ValueError("source health contains an unknown source")
            health[source] = _text(value, f"{source} health")
        freshness: dict[str, bool] = {source: False for source in MISSION_SNAPSHOT_SOURCES}
        for source, value in self.source_freshness.items():
            if source not in freshness or not isinstance(value, bool):
                raise ValueError("source freshness must contain boolean known sources")
            freshness[source] = value
        missing = tuple(
            source
            for source in MISSION_SNAPSHOT_SOURCES
            if references[source] is None
            or revisions[source] is None
            or health[source] == "missing"
        )
        object.__setattr__(self, "source_health", MappingProxyType(health))
        object.__setattr__(self, "source_freshness", MappingProxyType(freshness))
        object.__setattr__(self, "missing_sources", missing)

    @property
    def source_revision_map(self) -> Mapping[str, int | None]:
        return self.source_revisions

    @property
    def creation_time(self) -> str:
        return self.created_at

    @property
    def source_missing(self) -> Mapping[str, bool]:
        return self.missing

    @property
    def source_status(self) -> Mapping[str, Mapping[str, object]]:
        return MappingProxyType(
            {
                source: MappingProxyType(
                    {
                        "revision": self.source_revisions[source],
                        "reference": self.source_references[source],
                        "health": self.source_health[source],
                        "fresh": self.source_freshness[source],
                        "missing": source in self.missing_sources,
                    }
                )
                for source in MISSION_SNAPSHOT_SOURCES
            }
        )

    @property
    def missing(self) -> Mapping[str, bool]:
        return MappingProxyType({source: source in self.missing_sources for source in MISSION_SNAPSHOT_SOURCES})

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "version": self.version,
            "created_at": self.created_at,
            "plan_revision": self.plan_revision,
            "plan_reference": self.plan_reference,
            "operational_scene_graph": self.operational_scene_graph,
            "bayesian_belief_snapshot": self.bayesian_belief_snapshot,
            "fsm_status": self.fsm_status,
            "active_maneuver": self.active_maneuver,
            "source_revisions": _json_value(self.source_revisions),
            "source_references": _json_value(self.source_references),
            "source_health": _json_value(self.source_health),
            "source_freshness": _json_value(self.source_freshness),
            "missing_sources": list(self.missing_sources),
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MissionSnapshot":
        expected = set(cls("m", 1, "t").to_dict())
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("mission snapshot contains unknown or missing fields")
        if value["schema_version"] != 1:
            raise ValueError("unsupported mission snapshot schema version")
        fields = dict(value)
        fields.pop("schema_version")
        fields.pop("missing_sources")
        return cls(**fields)

    @classmethod
    def from_json(cls, value: str) -> "MissionSnapshot":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("mission snapshot JSON is invalid") from exc
        return cls.from_dict(decoded)


def mission_snapshot_to_transport_event(
    snapshot: MissionSnapshot,
    *,
    event_id: str,
    sequence: int,
) -> TransportEvent:
    """Wrap a snapshot in the generic transport envelope."""

    if not isinstance(snapshot, MissionSnapshot):
        raise TypeError("snapshot must be a MissionSnapshot")
    return TransportEvent(
        schema_version=snapshot.schema_version,
        event_id=event_id,
        mission_id=snapshot.mission_id,
        sequence=sequence,
        event_kind="mission-snapshot",
        payload=snapshot.to_dict(),
    )


def mission_snapshot_from_transport_event(event: TransportEvent) -> MissionSnapshot:
    if not isinstance(event, TransportEvent) or event.event_kind != "mission-snapshot":
        raise ValueError("event is not a mission snapshot")
    snapshot = MissionSnapshot.from_dict(event.payload)
    if snapshot.mission_id != event.mission_id:
        raise ValueError("snapshot mission ID does not match its transport event")
    return snapshot


# Short aliases keep the public conversion seam easy to discover.
snapshot_to_transport_event = mission_snapshot_to_transport_event
snapshot_from_transport_event = mission_snapshot_from_transport_event


def create_source_fact_event(
    mission_id: str,
    source: str,
    revision: int,
    *,
    event_id: str,
    sequence: int,
    reference: str | None = None,
    health: str | bool = "healthy",
    fresh: bool = True,
) -> TransportEvent:
    """Create a generic, JSON-safe source revision/health fact event."""

    source = normalize_source_name(source)
    if source == "plan":
        raise ValueError("plan facts are supplied by normalized-plan events")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("source revision must be a non-negative integer")
    if isinstance(health, bool):
        health = "healthy" if health else "unhealthy"
    health = _text(health, "source health")
    if not isinstance(fresh, bool):
        raise ValueError("source freshness must be boolean")
    return TransportEvent(
        schema_version=1,
        event_id=event_id,
        mission_id=_text(mission_id, "mission ID"),
        sequence=sequence,
        event_kind="source-fact",
        payload={
            "source": source,
            "revision": revision,
            "reference": _reference(reference, "source reference"),
            "health": health,
            "fresh": fresh,
        },
    )


create_source_revision_event = create_source_fact_event
