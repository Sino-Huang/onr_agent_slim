"""Operator-view ``beliefs`` section read from one Mission Run root.

Mission 1 and joint runs persist a reporting-reliability belief under
``<run_root>/agent-storage/bayesian-beliefs/<mission>/``: one immutable,
hash-addressed ``reporting-reliability-<sha>.json`` snapshot per revision and
the committed ``reporting-reliability-state-v1.json``. The committed state
selects the current revision; the immutable snapshots supply its history.

Object-search belief (Mission 4) lives inside the environment's Mission 4
worker, not in agent storage, so it never appears here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from onr.adapters.bayesian_belief_store import (
    BayesianBeliefStoreError,
    FileBayesianBeliefStore,
)
from onr.contracts.bayesian_belief import BayesianBeliefSnapshot
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot
from onr.runtime_host.run_files import JsonFileCache, json_file_names

HISTORY_LIMIT = 20
TOP_CHANGES_LIMIT = 5
# Mission modes whose closed loop composes a belief service (runtime/cli.py).
BELIEF_MISSION_MODES = frozenset({"mission1", "joint"})

_STATE_FILE = "reporting-reliability-state-v1.json"
_SNAPSHOT_FILE = re.compile(r"reporting-reliability-[0-9a-f]{64}\.json")
_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
_STATE_MAX_BYTES = 256 * 1024 * 1024
_CHANGE_EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class _EntityBelief:
    entity_id: str
    label: str
    mean: float
    honest_probability: float | None
    credible_interval: tuple[float, float] | None
    variance: float | None
    outcome_counts: Mapping[str, int] | None


@dataclass(frozen=True, slots=True)
class _Revision:
    revision: int
    created_at: str
    content_sha256: str
    entities: tuple[_EntityBelief, ...]


def _reporting_revision(snapshot: ReportingReliabilitySnapshot) -> _Revision:
    return _Revision(
        revision=snapshot.belief_revision,
        created_at=snapshot.created_at,
        content_sha256=snapshot.content_sha256,
        entities=tuple(
            _EntityBelief(
                entity_id=str(ship.entity_id),
                label=f"ship {ship.entity_id}",
                mean=ship.mean,
                honest_probability=ship.honest_probability,
                credible_interval=ship.credible_interval,
                variance=ship.variance,
                outcome_counts=dict(ship.outcome_counts),
            )
            for ship in snapshot.ships
        ),
    )


def _risk_revision(snapshot: BayesianBeliefSnapshot) -> _Revision:
    return _Revision(
        revision=snapshot.belief_revision,
        created_at=snapshot.created_at,
        content_sha256=snapshot.content_sha256,
        entities=tuple(
            _EntityBelief(
                entity_id=f"{marginal.key.entity_id}/{marginal.key.risk_type}",
                label=f"{marginal.key.entity_id} · {marginal.key.risk_type}",
                mean=marginal.probability_risk,
                honest_probability=None,
                credible_interval=None,
                variance=None,
                outcome_counts=None,
            )
            for marginal in snapshot.marginals
        ),
    )


def _decode_snapshot_file(document: Mapping[str, object]) -> _Revision:
    return _reporting_revision(ReportingReliabilitySnapshot.from_dict(document))


def _decode_state_file(document: Mapping[str, object]) -> _Revision:
    return _reporting_revision(
        ReportingReliabilitySnapshot.from_dict(document["snapshot"])
    )


_SNAPSHOTS: JsonFileCache[_Revision] = JsonFileCache(
    _decode_snapshot_file, max_bytes=_SNAPSHOT_MAX_BYTES, max_entries=1024
)
_STATES: JsonFileCache[_Revision] = JsonFileCache(
    _decode_state_file, max_bytes=_STATE_MAX_BYTES, max_entries=8
)


def beliefs_section(
    run_root: Path, mission_id: str, mission_mode: str | None
) -> dict[str, object]:
    """Project the current belief, per-entity deltas, and bounded revision history."""

    storage = Path(run_root) / "agent-storage"
    revisions = _reporting_reliability_revisions(storage, mission_id)
    if revisions:
        return _section("reporting_reliability", revisions)
    risk = _bayesian_risk_revision(storage, mission_id)
    if risk is not None:
        return _section("bayesian_risk", [risk])
    if mission_mode is not None and mission_mode not in BELIEF_MISSION_MODES:
        reason = f"no Bayesian belief for this mission mode ({mission_mode})"
    else:
        reason = "no belief revision recorded yet"
    return {
        "belief_kind": None,
        "reason": reason,
        "revision": None,
        "created_at": None,
        "entities": [],
        "history": [],
    }


def _reporting_reliability_revisions(storage: Path, mission_id: str) -> list[_Revision]:
    """Return committed revisions, ascending, one snapshot per revision."""

    # Same mission component as FileReportingReliabilityStore._mission_root.
    mission_root = storage / "bayesian-beliefs" / quote(mission_id, safe="._-")
    names = json_file_names(mission_root)
    committed = (
        _STATES.get(mission_root / _STATE_FILE) if _STATE_FILE in names else None
    )
    by_revision: dict[int, _Revision] = {}
    for name in sorted(names):
        if not _SNAPSHOT_FILE.fullmatch(name):
            continue
        revision = _SNAPSHOTS.get(mission_root / name)
        if revision is None:
            continue
        if committed is not None and revision.revision >= committed.revision:
            # Only the committed snapshot counts at the head; later artifacts
            # are uncommitted leftovers of an interrupted save.
            continue
        by_revision[revision.revision] = revision
    if committed is not None:
        by_revision[committed.revision] = committed
    return [by_revision[key] for key in sorted(by_revision)]


def _bayesian_risk_revision(storage: Path, mission_id: str) -> _Revision | None:
    """Return the committed binary-risk belief; its store keeps no usable history."""

    if not (storage / "bayesian-beliefs").is_dir():
        return None
    try:
        store = FileBayesianBeliefStore(storage)
        if not store.current_path(mission_id).is_file():
            return None
        snapshot = store.load_current_read_only(mission_id)
    except (BayesianBeliefStoreError, OSError, ValueError):
        return None
    return None if snapshot is None else _risk_revision(snapshot)


def _section(belief_kind: str, revisions: Sequence[_Revision]) -> dict[str, object]:
    latest = revisions[-1]
    previous = revisions[-2] if len(revisions) > 1 else None
    prior = revisions[0] if revisions[0].revision == 1 else None
    window = revisions[-HISTORY_LIMIT:]
    window_means = [
        {entity.entity_id: entity.mean for entity in item.entities} for item in window
    ]
    previous_means = _means(previous)
    prior_means = _means(prior)
    entities = sorted(
        latest.entities,
        key=lambda entity: (-entity.mean, _entity_order(entity.entity_id)),
    )
    return {
        "belief_kind": belief_kind,
        "reason": None,
        "revision": latest.revision,
        "created_at": latest.created_at,
        "entities": [
            {
                "entity_id": entity.entity_id,
                "label": entity.label,
                "mean": entity.mean,
                "honest_probability": entity.honest_probability,
                "credible_interval": (
                    None
                    if entity.credible_interval is None
                    else list(entity.credible_interval)
                ),
                "variance": entity.variance,
                "outcome_counts": (
                    None
                    if entity.outcome_counts is None
                    else dict(entity.outcome_counts)
                ),
                "delta_since_previous": _delta(entity, previous_means),
                "delta_since_prior": _delta(entity, prior_means),
                "means_by_revision": [
                    means.get(entity.entity_id) for means in window_means
                ],
            }
            for entity in entities
        ],
        "history": [
            {
                "revision": item.revision,
                "created_at": item.created_at,
                "top_changes": _top_changes(
                    item, _means(revisions[index - 1]) if index > 0 else None
                ),
            }
            for index, item in enumerate(revisions)
            if index >= len(revisions) - HISTORY_LIMIT
        ],
    }


def _means(revision: _Revision | None) -> dict[str, float] | None:
    if revision is None:
        return None
    return {entity.entity_id: entity.mean for entity in revision.entities}


def _delta(entity: _EntityBelief, baseline: Mapping[str, float] | None) -> float | None:
    if baseline is None or entity.entity_id not in baseline:
        return None
    return entity.mean - baseline[entity.entity_id]


def _top_changes(
    revision: _Revision, previous: Mapping[str, float] | None
) -> list[dict[str, object]]:
    if previous is None:
        return []
    changes = [
        (entity.mean - previous[entity.entity_id], entity)
        for entity in revision.entities
        if entity.entity_id in previous
        and abs(entity.mean - previous[entity.entity_id]) > _CHANGE_EPSILON
    ]
    changes.sort(key=lambda item: (-abs(item[0]), _entity_order(item[1].entity_id)))
    return [
        {"entity_id": entity.entity_id, "mean": entity.mean, "delta": delta}
        for delta, entity in changes[:TOP_CHANGES_LIMIT]
    ]


def _entity_order(entity_id: str) -> tuple[int, int | str]:
    return (0, int(entity_id)) if entity_id.isdigit() else (1, entity_id)


__all__ = ["BELIEF_MISSION_MODES", "HISTORY_LIMIT", "beliefs_section"]
