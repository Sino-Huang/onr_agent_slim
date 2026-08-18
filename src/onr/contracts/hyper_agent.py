"""Public immutable contracts owned by the Hyper Agent boundary."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from onr.contracts.planning import MissionSpec, SymbolicMissionSpec


_HYPER_AGENT_TOKEN = object()


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _freeze(value: object, label: str) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} keys must be strings")
            frozen[key] = _freeze(item, label)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, label) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{label} must contain only finite JSON values")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: object) -> str:
    return json.dumps(_thaw(value), allow_nan=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class MissionInput:
    """The natural-language input from which a Mission is interpreted."""

    mission_id: str
    mission_text: str
    source_authority: str

    def __post_init__(self) -> None:
        _text(self.mission_id, "mission ID")
        _text(self.mission_text, "mission text")
        _text(self.source_authority, "source authority")

    @property
    def text(self) -> str:
        return self.mission_text

    def to_dict(self) -> dict[str, str]:
        return {
            "mission_id": self.mission_id,
            "mission_text": self.mission_text,
            "source_authority": self.source_authority,
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())


@dataclass(frozen=True, slots=True)
class FrozenMissionSpec:
    """Validated, auditable authority for one immutable Mission Specification."""

    mission_input: MissionInput
    mission_spec: MissionSpec | SymbolicMissionSpec
    revision: int
    canonical_document: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mission_input, MissionInput):
            raise ValueError("mission input must be a MissionInput")
        if not isinstance(self.mission_spec, (MissionSpec, SymbolicMissionSpec)):
            raise ValueError("mission spec must be a validated MissionSpec")
        _positive_int(self.revision, "mission specification revision")
        if self.mission_input.mission_id != self.mission_spec.mission_id:
            raise ValueError("mission input and mission specification IDs must match")
        if self.mission_input.source_authority != self.mission_spec.source_authority:
            raise ValueError("mission input and mission specification authorities must match")
        if not isinstance(self.canonical_document, str) or not self.canonical_document:
            raise ValueError("canonical document must be a non-empty string")
        expected_document = self.mission_spec.to_canonical_json()
        if self.canonical_document != expected_document:
            raise ValueError("canonical document does not match the validated Mission Specification")
        expected_hash = hashlib.sha256(self.canonical_document.encode("utf-8")).hexdigest()
        if self.sha256 is not None and self.sha256 != expected_hash:
            raise ValueError("SHA-256 does not match the canonical document")
        object.__setattr__(self, "sha256", expected_hash)

    @property
    def spec(self) -> MissionSpec | SymbolicMissionSpec:
        return self.mission_spec

    @property
    def canonical_json(self) -> str:
        return self.canonical_document

    @property
    def content_hash(self) -> str:
        return cast(str, self.sha256)

    @property
    def mission_id(self) -> str:
        return self.mission_input.mission_id

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_input": self.mission_input.to_dict(),
            "mission_spec": self.mission_spec.to_dict(),
            "revision": self.revision,
            "canonical_document": self.canonical_document,
            "sha256": self.sha256,
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())


MissionAuthorityRecord = FrozenMissionSpec


@dataclass(frozen=True, slots=True)
class ReplanRequest:
    """An advisory, provenance-preserving request to evaluate replanning."""

    request_id: str
    mission_id: str
    reason: str
    requester: str
    observed_plan_revision: int
    source_revisions: Mapping[str, int | None] = field(default_factory=dict)
    coalesced_request_ids: tuple[str, ...] = ()
    coalesced_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.request_id, "replan request ID")
        _text(self.mission_id, "mission ID")
        _text(self.reason, "replan reason")
        if self.requester != "maneuver-control":
            raise ValueError("replan requester must be maneuver-control")
        _nonnegative_int(self.observed_plan_revision, "observed plan revision")
        if not isinstance(self.source_revisions, Mapping):
            raise ValueError("source revisions must be a mapping")
        revisions: dict[str, int | None] = {}
        for source, revision in self.source_revisions.items():
            _text(source, "source revision name")
            if revision is not None:
                _nonnegative_int(revision, "source revision")
            revisions[source] = revision
        ids = tuple(self.coalesced_request_ids)
        reasons = tuple(self.coalesced_reasons)
        if not all(isinstance(item, str) and item.strip() for item in ids):
            raise ValueError("coalesced request IDs must be non-empty strings")
        if not all(isinstance(item, str) and item.strip() for item in reasons):
            raise ValueError("coalesced reasons must be non-empty strings")
        object.__setattr__(self, "source_revisions", MappingProxyType(revisions))
        object.__setattr__(self, "coalesced_request_ids", ids)
        object.__setattr__(self, "coalesced_reasons", reasons)

    @property
    def plan_revision(self) -> int:
        return self.observed_plan_revision

    @property
    def authoritative_source_revisions(self) -> Mapping[str, int | None]:
        return self.source_revisions

    @property
    def coalesced_requests(self) -> tuple[str, ...]:
        return self.coalesced_request_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "mission_id": self.mission_id,
            "reason": self.reason,
            "requester": self.requester,
            "observed_plan_revision": self.observed_plan_revision,
            "source_revisions": _thaw(self.source_revisions),
            "coalesced_request_ids": list(self.coalesced_request_ids),
            "coalesced_reasons": list(self.coalesced_reasons),
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReplanRequest:
        expected = {
            "request_id",
            "mission_id",
            "reason",
            "requester",
            "observed_plan_revision",
            "source_revisions",
            "coalesced_request_ids",
            "coalesced_reasons",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("replan request contains unknown or missing fields")
        ids = value["coalesced_request_ids"]
        reasons = value["coalesced_reasons"]
        if not isinstance(ids, (list, tuple)) or not isinstance(reasons, (list, tuple)):
            raise ValueError("coalesced request fields must be arrays")
        return cls(
            request_id=value["request_id"],
            mission_id=value["mission_id"],
            reason=value["reason"],
            requester=value["requester"],
            observed_plan_revision=value["observed_plan_revision"],
            source_revisions=value["source_revisions"],
            coalesced_request_ids=tuple(ids),
            coalesced_reasons=tuple(reasons),
        )

    @classmethod
    def from_json(cls, value: str) -> ReplanRequest:
        try:
            decoded = json.loads(value, parse_constant=_reject_non_finite)
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("replan request JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True, init=False)
class HumanQuestion:
    """A Hyper Agent-only escalation for information requiring a human."""

    question_id: str
    mission_id: str
    text: str
    context: Mapping[str, object] = field(default_factory=dict)
    requester: str

    def __init__(
        self,
        question_id: str,
        mission_id: str,
        text: str,
        context: Mapping[str, object] | None = None,
        *,
        _authority_token: object | None = None,
    ) -> None:
        if _authority_token is not _HYPER_AGENT_TOKEN:
            raise ValueError("HumanQuestion may only be issued by HyperAgent")
        object.__setattr__(self, "question_id", question_id)
        object.__setattr__(self, "mission_id", mission_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "context", {} if context is None else context)
        object.__setattr__(self, "requester", "hyper-agent")
        self.__post_init__()

    def __post_init__(self) -> None:
        _text(self.question_id, "human question ID")
        _text(self.mission_id, "mission ID")
        _text(self.text, "human question text")
        if self.requester != "hyper-agent":
            raise ValueError("only hyper-agent may issue a HumanQuestion")
        frozen = _freeze(self.context, "human question context")
        if not isinstance(frozen, Mapping):
            raise ValueError("human question context must be a mapping")
        object.__setattr__(self, "context", frozen)

    @property
    def requester_id(self) -> str:
        return self.requester

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "mission_id": self.mission_id,
            "text": self.text,
            "context": _thaw(self.context),
            "requester": self.requester,
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())


def _issue_human_question(
    question_id: str,
    mission_id: str,
    text: str,
    context: Mapping[str, object] | None = None,
) -> HumanQuestion:
    """Private token-gated construction seam for the Hyper Agent service."""

    return HumanQuestion(
        question_id,
        mission_id,
        text,
        context,
        _authority_token=_HYPER_AGENT_TOKEN,
    )
