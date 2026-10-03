"""Three-level progress hierarchy for the Operator Debug View (D6).

Run Narrative (root, carried beside the nodes) → Mission Log Summaries
(``summary:<seq>``) → raw operational-log records (``log:<sequence>``).
Records not covered by any summary sit under the synthetic ``live`` node.

The node stream is incremental: every node carries a change sequence and a
page returns only nodes whose latest version changed after a watermark.
When a summary covering already-published records appears, those records
are published again with their new ``parent_id`` (re-parenting).

Everything here is pure over explicit inputs (records, summaries, run state);
nothing reads global ``var/`` paths.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from onr.ports.mission_log_summarizer import SummaryArtifact
from onr.runtime_host.importance import (
    DEBUG,
    IMPORTANCE_MAPPING_VERSION,
    ROUTINE,
    empty_importance_counts,
    max_importance,
    record_importance,
    record_title,
)

ROOT_NODE_ID = "root"
LIVE_NODE_ID = "live"
LIVE_NODE_TITLE = "Live — not yet summarized"
SUMMARY_SOURCE = "mission-summary"
_SUMMARY_TITLE_CHARACTERS = 120

_NONTERMINAL_STATUSES = frozenset({"queued", "running", "awaiting_human_decision"})
PHASE_STEPS: tuple[tuple[str, str], ...] = (
    ("stack", "Stack"),
    ("intent", "Intent"),
    ("planning", "Planning"),
    ("statechart", "Statechart"),
    ("executing", "Executing"),
    ("terminal", "Done"),
)


def summary_node_id(sequence: int) -> str:
    return f"summary:{sequence}"


def record_node_id(sequence: int) -> str:
    return f"log:{sequence}"


def load_mission_log_summaries(
    agent_storage_root: Path, mission_id: str
) -> tuple[SummaryArtifact, ...]:
    """Read ``<agent-storage>/summaries/<mission>/<seq>.json`` in sequence order.

    Unreadable or invalid files are skipped so one bad artifact never hides
    the rest of the hierarchy.
    """

    if (
        not mission_id
        or Path(mission_id).name != mission_id
        or mission_id in {".", ".."}
    ):
        raise ValueError("mission ID must be one path component")
    mission_dir = Path(agent_storage_root) / "summaries" / mission_id
    if not mission_dir.is_dir():
        return ()
    artifacts: dict[int, SummaryArtifact] = {}
    for path in mission_dir.glob("[0-9]*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                continue
            artifact = SummaryArtifact.from_dict(raw)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if artifact.mission_id == mission_id:
            artifacts[artifact.sequence] = artifact
    return tuple(artifacts[sequence] for sequence in sorted(artifacts))


def _sequence(record: Mapping[str, object]) -> int | None:
    value = record.get("sequence")
    return value if type(value) is int and value > 0 else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _details(record: Mapping[str, object]) -> dict[str, object]:
    details = record.get("details")
    return dict(details) if isinstance(details, Mapping) else {}


def counts_by_importance(records: Iterable[Mapping[str, object]]) -> dict[str, int]:
    """Count operational-log records per Importance Level."""

    counts = empty_importance_counts()
    for record in records:
        counts[record_importance(record)] += 1
    return counts


def narrative_projection(narrative: Mapping[str, object] | None) -> dict[str, object]:
    """Project a public Run Narrative onto the ``progress.narrative`` shape."""

    if not narrative:
        return {
            "status": "none",
            "text": None,
            "generated_at": None,
            "source_watermark": 0,
        }
    watermark = narrative.get("source_watermark")
    return {
        "status": narrative.get("status", "none"),
        "text": narrative.get("text"),
        "generated_at": narrative.get("generated_at"),
        "source_watermark": watermark if type(watermark) is int else 0,
    }


@dataclass(frozen=True, slots=True)
class ProgressPage:
    """Nodes changed after a watermark, oldest change first."""

    nodes: list[dict[str, object]]
    watermark: int
    has_more: bool


class ProgressTree:
    """Incremental flat node stream for the ``progress`` section.

    Feed it records and summaries as they appear (duplicates are ignored),
    then page with the last returned watermark to receive only new or
    changed nodes. Watermarks are plain integers; cursor encoding belongs to
    the operator projection.
    """

    def __init__(self) -> None:
        self._records: dict[int, dict[str, object]] = {}
        self._record_importance: dict[int, str] = {}
        self._parents: dict[int, str] = {}
        self._summaries: dict[int, SummaryArtifact] = {}
        self._summary_starts: list[int] = []
        self._summary_by_start: dict[int, int] = {}
        self._summary_children: dict[int, set[int]] = {}
        self._live_children: set[int] = set()
        self._last_fsm_state: str | None = None
        self._nodes: dict[str, dict[str, object]] = {}
        self._changed_at: dict[str, int] = {}
        self._journal: list[tuple[int, str]] = []
        self._sequence = 0
        self._publish(self._live_node())

    @property
    def watermark(self) -> int:
        return self._sequence

    def nodes(self) -> list[dict[str, object]]:
        """Current version of every node, oldest change first."""

        ordered = sorted(self._nodes, key=lambda node_id: self._changed_at[node_id])
        return [dict(self._nodes[node_id]) for node_id in ordered]

    def ingest(
        self,
        records: Iterable[Mapping[str, object]] = (),
        summaries: Iterable[SummaryArtifact] = (),
    ) -> None:
        """Add new operational-log records and Mission Log Summaries."""

        live_changed = False
        touched: set[int] = set()  # existing summaries whose children changed
        new_records = sorted(
            (
                (sequence, record)
                for record in records
                if (sequence := _sequence(record)) is not None
                and sequence not in self._records
            ),
            key=lambda item: item[0],
        )
        for sequence, record in new_records:
            self._records[sequence] = dict(record)
            self._record_importance[sequence] = record_importance(record)
            covering = self._covering_summary(sequence)
            self._assign(sequence, covering)
            if covering is None:
                live_changed = True
            else:
                touched.add(covering)
            self._publish(self._record_node(sequence, self._last_fsm_state))
            if (
                record.get("source") == "fsm-runner"
                and record.get("event_kind") == "fsm"
            ):
                state = _details(record).get("state")
                if isinstance(state, str) and state:
                    self._last_fsm_state = state
        for summary in sorted(summaries, key=lambda item: item.sequence):
            if summary.sequence in self._summaries:
                continue
            self._add_summary(summary)
            moved: list[int] = []
            for sequence in range(
                summary.input_start_sequence, summary.input_end_sequence + 1
            ):
                old_parent = self._parents.get(sequence)
                if old_parent is None:
                    continue
                if old_parent == LIVE_NODE_ID:
                    live_changed = True
                else:
                    touched.add(int(old_parent.split(":", 1)[1]))
                self._assign(sequence, summary.sequence)
                moved.append(sequence)
            touched.discard(summary.sequence)
            # Publish the parent before its re-parented children.
            self._publish(self._summary_node(summary.sequence))
            for sequence in moved:
                node = dict(self._nodes[record_node_id(sequence)])
                node["parent_id"] = self._parents[sequence]
                self._publish(node)
        for summary_sequence in sorted(touched):
            self._publish(self._summary_node(summary_sequence))
        if live_changed:
            self._publish(self._live_node())

    def page(self, *, after: int = 0, limit: int) -> ProgressPage:
        """Return at most ``limit`` nodes whose latest change is after ``after``."""

        if type(after) is not int or after < 0 or after > self._sequence:
            raise ValueError("progress watermark is out of range")
        if limit < 1:
            raise ValueError("progress page limit must be positive")
        start = bisect.bisect_right(self._journal, (after, "\uffff"))
        selected: list[dict[str, object]] = []
        watermark = after
        has_more = False
        for change, node_id in self._journal[start:]:
            if self._changed_at.get(node_id) != change:
                continue  # superseded by a later change of the same node
            if len(selected) == limit:
                has_more = True
                break
            selected.append(dict(self._nodes[node_id]))
            watermark = change
        if not has_more:
            watermark = self._sequence
        return ProgressPage(selected, watermark, has_more)

    # -- internals -------------------------------------------------------

    def _add_summary(self, summary: SummaryArtifact) -> None:
        self._summaries[summary.sequence] = summary
        self._summary_children.setdefault(summary.sequence, set())
        start = summary.input_start_sequence
        if start not in self._summary_by_start:
            bisect.insort(self._summary_starts, start)
        self._summary_by_start[start] = summary.sequence

    def _covering_summary(self, sequence: int) -> int | None:
        index = bisect.bisect_right(self._summary_starts, sequence) - 1
        if index < 0:
            return None
        summary_sequence = self._summary_by_start[self._summary_starts[index]]
        summary = self._summaries[summary_sequence]
        if sequence <= summary.input_end_sequence:
            return summary_sequence
        return None

    def _assign(self, sequence: int, summary_sequence: int | None) -> None:
        old_parent = self._parents.get(sequence)
        if old_parent == LIVE_NODE_ID:
            self._live_children.discard(sequence)
        elif old_parent is not None:
            self._summary_children[int(old_parent.split(":", 1)[1])].discard(sequence)
        if summary_sequence is None:
            self._live_children.add(sequence)
            self._parents[sequence] = LIVE_NODE_ID
        else:
            self._summary_children[summary_sequence].add(sequence)
            self._parents[sequence] = summary_node_id(summary_sequence)

    def _record_node(
        self, sequence: int, previous_fsm_state: str | None
    ) -> dict[str, object]:
        record = self._records[sequence]
        return {
            "node_id": record_node_id(sequence),
            "parent_id": self._parents[sequence],
            "level": "record",
            "importance": self._record_importance[sequence],
            "time_start": _text(record.get("event_time")),
            "time_end": None,
            "mission_time_seconds": None,
            "source": _text(record.get("source")),
            "event_kind": _text(record.get("event_kind")),
            "outcome": _text(record.get("outcome")),
            "title": record_title(record, previous_fsm_state=previous_fsm_state),
            "text": None,
            "child_count": 0,
            "authoritative": True,
            "details": _details(record),
        }

    def _summary_node(self, summary_sequence: int) -> dict[str, object]:
        summary = self._summaries[summary_sequence]
        children = sorted(self._summary_children[summary_sequence])
        text = summary.summary.strip()
        first_line = text.splitlines()[0].strip() if text else ""
        if len(first_line) > _SUMMARY_TITLE_CHARACTERS:
            first_line = first_line[: _SUMMARY_TITLE_CHARACTERS - 1].rstrip() + "…"
        return {
            "node_id": summary_node_id(summary_sequence),
            "parent_id": ROOT_NODE_ID,
            "level": "summary",
            "importance": max_importance(
                (self._record_importance[sequence] for sequence in children),
                default=DEBUG,
            ),
            "time_start": (
                _text(self._records[children[0]].get("event_time"))
                if children
                else None
            ),
            "time_end": (
                _text(self._records[children[-1]].get("event_time"))
                if children
                else summary.created_at
            ),
            "mission_time_seconds": None,
            "source": SUMMARY_SOURCE,
            "event_kind": None,
            "outcome": None,
            "title": first_line,
            "text": text,
            "child_count": len(children),
            "authoritative": False,
            "details": None,
        }

    def _live_node(self) -> dict[str, object]:
        # The live node never drops below routine so it stays visible.
        return {
            "node_id": LIVE_NODE_ID,
            "parent_id": ROOT_NODE_ID,
            "level": "live",
            "importance": max_importance(
                (
                    ROUTINE,
                    *(
                        self._record_importance[sequence]
                        for sequence in self._live_children
                    ),
                )
            ),
            "time_start": None,
            "time_end": None,
            "mission_time_seconds": None,
            "source": "runtime-host",
            "event_kind": None,
            "outcome": None,
            "title": LIVE_NODE_TITLE,
            "text": None,
            "child_count": len(self._live_children),
            "authoritative": True,
            "details": None,
        }

    def _publish(self, node: dict[str, object]) -> None:
        node_id = str(node["node_id"])
        if self._nodes.get(node_id) == node:
            return
        self._sequence += 1
        self._nodes[node_id] = node
        self._changed_at[node_id] = self._sequence
        self._journal.append((self._sequence, node_id))


def progress_payload(
    *, nodes: Sequence[Mapping[str, object]], narrative: Mapping[str, object] | None
) -> dict[str, object]:
    """The ``progress`` object of the operator-view ``progress`` section."""

    return {
        "mapping_version": IMPORTANCE_MAPPING_VERSION,
        "narrative": narrative_projection(narrative),
        "nodes": [dict(node) for node in nodes],
    }


# -- overview phase ------------------------------------------------------


def _stack_services(stack: Mapping[str, object] | None) -> list[Mapping[str, object]]:
    services = stack.get("services") if stack else None
    if not isinstance(services, list):
        return []
    return [
        item
        for item in services
        if isinstance(item, Mapping) and item.get("required", True) is True
    ]


def derive_phase(
    *,
    run: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    stack: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Code-owned overview ``phase`` from run status, stack and log kinds.

    ``run`` is the public Mission Run record (``status``,
    ``terminal_classification``, ``terminal_detail``); ``stack`` is the
    ``stack`` section payload (``services[]``) when the run has one.
    A step is ``done`` only on positive evidence; when a run ends without
    succeeding, the step it stopped in is ``failed`` and later steps stay
    ``pending``.
    """

    status = str(run.get("status", "queued"))
    terminal = status not in _NONTERMINAL_STATUSES
    succeeded = status == "succeeded"
    classification = _text(run.get("terminal_classification"))
    raw_detail = run.get("terminal_detail")
    terminal_detail = raw_detail if isinstance(raw_detail, Mapping) else {}
    stage = _text(terminal_detail.get("stage")) or ""
    failure_text = _text(terminal_detail.get("message")) or classification

    seen: set[tuple[str, str, str]] = set()
    revisions: list[int] = []
    planning_rejections = 0
    statechart_rejections = 0
    for record in records:
        source = str(record.get("source", ""))
        kind = str(record.get("event_kind", ""))
        outcome = str(record.get("outcome", ""))
        seen.add((source, kind, outcome))
        if source == "hyper-agent" and kind in {"planner-assets", "planner-execution"}:
            planning_rejections += outcome not in {"accepted", "verified"}
        if source == "hyper-agent" and kind == "statechart-generation":
            statechart_rejections += outcome == "rejected"
        if source == "fsm-runner" and kind == "fsm":
            revision = _details(record).get("plan_revision")
            if type(revision) is int:
                revisions.append(revision)

    def hyper(kind: str, outcome: str) -> bool:
        return ("hyper-agent", kind, outcome) in seen

    services = _stack_services(stack)
    ready = sum(1 for item in services if item.get("state") == "ready")
    failed_services = [
        str(item.get("name"))
        for item in services
        if item.get("state") == "failed"
        or (item.get("state") == "exited" and item.get("exit_code") not in (None, 0))
    ]
    executing_started = ("fsm-runner", "fsm", "initialized") in seen
    done = {
        "stack": bool(records) or (bool(services) and ready == len(services)),
        "intent": hyper("planning-intent", "completed"),
        "planning": hyper("planner-execution", "verified"),
        "statechart": hyper("statechart-generation", "verified"),
        "executing": succeeded and executing_started,
        "terminal": succeeded,
    }
    details: dict[str, str | None] = {step: None for step, _ in PHASE_STEPS}
    if services:
        details["stack"] = f"{ready} services ready"
    if planning_rejections:
        details["planning"] = f"{planning_rejections} attempt(s) rejected"
    if statechart_rejections:
        details["statechart"] = f"{statechart_rejections} attempt(s) rejected"
    if revisions:
        details["executing"] = f"plan revision {max(revisions)}"

    failed: set[str] = set()
    if (
        classification == "stack_failed"
        or stage.startswith("stack")
        or (failed_services and not records)
    ):
        failed.add("stack")
        service = _text(terminal_detail.get("service")) or (
            failed_services[0] if failed_services else None
        )
        details["stack"] = (
            f"{service}: {failure_text}"
            if service and failure_text
            else service or failure_text
        )
    if classification == "mission_rejected" or hyper("planning-intent", "rejected"):
        failed.add("intent")
        details["intent"] = _text(terminal_detail.get("reason")) or "mission rejected"
    if terminal and not succeeded:
        failed.add("terminal")
        details["terminal"] = classification or status
        if executing_started:
            failed.add("executing")
        elif not failed - {"terminal"}:
            stopped = next(
                (step for step, _ in PHASE_STEPS[:-2] if not done[step]), "executing"
            )
            failed.add(stopped)
            details[stopped] = details[stopped] or failure_text
    elif terminal:
        details["terminal"] = "succeeded"

    statuses: dict[str, str] = {}
    active_assigned = terminal or status == "queued"
    blocked = False
    for step, _ in PHASE_STEPS:
        if step in failed:
            statuses[step] = "failed"
            blocked = True
        elif done[step]:
            statuses[step] = "done"
        elif blocked or active_assigned:
            statuses[step] = "pending"
        else:
            statuses[step] = "active"
            active_assigned = True

    if terminal:
        current = "terminal"
    else:
        current = next(
            (step for step, _ in PHASE_STEPS if statuses[step] in {"active", "failed"}),
            "stack",
        )
    return {
        "current": current,
        "steps": [
            {
                "id": step,
                "label": label,
                "status": statuses[step],
                "detail": details[step],
            }
            for step, label in PHASE_STEPS
        ],
    }


__all__ = [
    "LIVE_NODE_ID",
    "LIVE_NODE_TITLE",
    "PHASE_STEPS",
    "ROOT_NODE_ID",
    "SUMMARY_SOURCE",
    "ProgressPage",
    "ProgressTree",
    "counts_by_importance",
    "derive_phase",
    "load_mission_log_summaries",
    "narrative_projection",
    "progress_payload",
    "record_node_id",
    "summary_node_id",
]
