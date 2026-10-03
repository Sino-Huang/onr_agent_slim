from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from onr.adapters.mission_log_summarizer import FileMissionLogSummarizer
from onr.adapters.operational_log import FileOperationalLog
from onr.ports.mission_log_summarizer import SummaryArtifact
from onr.ports.operational_log import OperationalLogRecord
from onr.runtime_host.importance import (
    IMPORTANCE_LEVELS,
    IMPORTANCE_MAPPING_VERSION,
    component_badge,
    max_importance,
    record_importance,
    record_title,
    replay_disposition_importance,
    service_importance,
)
from onr.runtime_host.progress import (
    ProgressTree,
    counts_by_importance,
    derive_phase,
    load_mission_log_summaries,
    progress_payload,
)

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = (
    Path(__file__).parent / "support" / "run_a6cqxx_progress" / "operational_log.jsonl"
)
_CONTRACT = _ROOT / "docs" / "design" / "operator-console" / "contract" / "v1.2"
_MISSION = "mission:demo"


def _records() -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
    ]


def _record(
    source: str, event_kind: str, outcome: str, sequence: int = 1, **details: object
) -> dict[str, object]:
    """A record shaped by the production OperationalLogRecord factory."""

    return OperationalLogRecord.create(
        _MISSION, source, event_kind, outcome, details=details, sequence=sequence
    ).to_dict()


def _contract(name: str) -> dict[str, Any]:
    return json.loads((_CONTRACT / name).read_text(encoding="utf-8"))


# -- importance -----------------------------------------------------------


def test_reference_run_records_map_to_issue_levels() -> None:
    levels = {
        (r["source"], r["event_kind"], r["outcome"]): record_importance(r)
        for r in _records()
        if not (r["source"] == "maneuver-control" and r["event_kind"] == "heartbeat")
    }

    assert levels == {
        ("context-coordination", "heartbeat", "completed"): "debug",
        ("hyper-agent", "prior-knowledge", "not_applied"): "debug",
        ("fsm-runner", "fsm", "updated"): "routine",
        ("hyper-agent", "workflow", "started"): "notable",
        ("hyper-agent", "workflow", "completed"): "notable",
        ("hyper-agent", "planning-intent", "completed"): "notable",
        ("hyper-agent", "planner-choice", "completed"): "notable",
        ("hyper-agent", "planner-assets", "accepted"): "notable",
        ("hyper-agent", "planner-execution", "verified"): "notable",
        ("hyper-agent", "statechart-generation", "verified"): "notable",
        ("hyper-agent", "heartbeat", "replan"): "notable",
        ("fsm-runner", "fsm", "initialized"): "notable",
        ("fsm-runner", "fsm", "transitioned"): "notable",
        ("fsm-runner", "fsm", "superseded"): "notable",
        ("maneuver-control", "control", "queued"): "notable",
    }


def test_maneuver_heartbeat_is_routine_unless_it_moved_the_fsm() -> None:
    heartbeats = {
        r["sequence"]: (
            r["details"].get("successful_transition_count"),
            record_importance(r),
        )
        for r in _records()
        if r["source"] == "maneuver-control" and r["event_kind"] == "heartbeat"
    }

    assert heartbeats[23] == (1, "notable")
    assert heartbeats[45] == (0, "routine")


@pytest.mark.parametrize(
    ("record", "level"),
    [
        (
            _record("hyper-agent", "planning-intent", "rejected", reason="errand"),
            "critical",
        ),
        (
            _record("hyper-agent", "workflow", "failed", error_type="RuntimeError"),
            "critical",
        ),
        (
            _record(
                "maneuver-control",
                "heartbeat",
                "failed",
                error_type="StructuredOutputRetriesExhausted",
            ),
            "critical",
        ),
        (_record("fsm-runner", "error", "failed", operation="run_once"), "critical"),
        (
            _record(
                "context-coordination", "error", "failed", operation="consume_event"
            ),
            "critical",
        ),
        (
            _record("runtime-host", "service", "failed", service="perception"),
            "critical",
        ),
        (
            _record(
                "runtime", "summary-unavailable", "failed", operation="mission_summary"
            ),
            "warning",
        ),
        (_record("hyper-agent", "planner-assets", "rejected"), "warning"),
        (_record("hyper-agent", "planner-execution", "unsolvable"), "warning"),
        (
            _record(
                "hyper-agent", "statechart-generation", "rejected", stage="validate"
            ),
            "warning",
        ),
        (_record("hyper-agent", "planner-generation-attempt", "rejected"), "warning"),
        (
            _record("maneuver-control", "heartbeat", "failed", error_type="ValueError"),
            "warning",
        ),
        (
            _record(
                "runtime", "planning-environment-data", "insufficient_environment_data"
            ),
            "warning",
        ),
        (_record("hyper-agent", "heartbeat", "no_change"), "routine"),
        (_record("runtime", "heartbeat", "attempted"), "routine"),
        (
            _record("maneuver-control", "control", "completed", operation="decide"),
            "notable",
        ),
        (_record("runtime", "agent", "started"), "notable"),
    ],
)
def test_failure_and_correction_records_map_to_issue_levels(
    record: dict[str, object], level: str
) -> None:
    assert record_importance(record) == level


def test_level_helpers_cover_summaries_services_and_replay() -> None:
    assert IMPORTANCE_MAPPING_VERSION == 1
    assert IMPORTANCE_LEVELS == ("critical", "warning", "notable", "routine", "debug")
    assert max_importance(["debug", "notable", "routine"]) == "notable"
    assert max_importance([]) == "debug"
    assert service_importance("failed") == "critical"
    assert service_importance("exited", exit_code=1) == "critical"
    assert service_importance("ready") == "routine"
    assert replay_disposition_importance("gap") == "warning"
    assert replay_disposition_importance("normal") is None


def test_component_badges_and_titles_follow_record_source() -> None:
    assert [
        component_badge(source)
        for source in (
            "hyper-agent",
            "maneuver-control",
            "context-coordination",
            "fsm-runner",
            "bayesian-belief",
            "physical-runtime",
            "perception",
            "runtime-host",
        )
    ] == ["HYP", "MAN", "CC", "FSM", "BEL", "ENV", "PER", "STK"]
    by_sequence = {r["sequence"]: r for r in _records()}
    assert (
        record_title(
            by_sequence[18], previous_fsm_state="patrol-awaiting-first-assignment"
        )
        == "FSM patrol-awaiting-first-assignment → assignment-1-in-progress"
    )
    assert record_title(by_sequence[1]) == "Context Coordination heartbeat"
    assert record_title(by_sequence[8]) == "Planner minizinc verified plan (rev 1)"


def test_counts_by_importance_match_contract_keys() -> None:
    counts = counts_by_importance(_records())
    expected = Counter(record_importance(r) for r in _records())

    assert list(counts) == list(
        _contract("mission-run-operator-overview.response.json")["overview"][
            "counts_by_importance"
        ]
    )
    assert counts == {level: expected.get(level, 0) for level in IMPORTANCE_LEVELS}
    assert counts["debug"] == 51  # 49 CC heartbeats + 2 prior-knowledge not_applied


# -- progress hierarchy ---------------------------------------------------


def _summaries_via_producer(
    tmp_path: Path, windows: list[list[dict[str, object]]]
) -> tuple[SummaryArtifact, ...]:
    """Write real summary files with the production summarizer, then load them."""

    log = FileOperationalLog(tmp_path / "operational-log")

    class _Model:
        def invoke(self, prompt: str, **_kwargs: object) -> str:
            return f"Window ending at {len(prompt)} characters.\nSecond line."

    summarizer = FileMissionLogSummarizer(log, tmp_path / "agent-storage", _Model())
    for window in windows:
        for record in window:
            log.append(OperationalLogRecord.from_dict(record))
        assert summarizer.heartbeat(_MISSION) is not None
    return load_mission_log_summaries(tmp_path / "agent-storage", _MISSION)


def test_uncovered_records_sit_under_the_live_node(tmp_path: Path) -> None:
    records = _records()
    tree = ProgressTree()
    tree.ingest(records)

    nodes = {node["node_id"]: node for node in tree.page(limit=1000).nodes}

    assert nodes["live"]["child_count"] == len(records)
    assert nodes["live"]["importance"] == "notable"
    assert {nodes[f"log:{r['sequence']}"]["parent_id"] for r in records} == {"live"}
    assert nodes["log:18"]["title"] == (
        "FSM patrol-awaiting-first-assignment → assignment-1-in-progress"
    )
    assert nodes["log:1"]["importance"] == "debug"
    assert nodes["log:1"]["authoritative"] is True


def test_live_node_stays_visible_when_only_debug_records_are_uncovered() -> None:
    tree = ProgressTree()
    tree.ingest([r for r in _records() if r["source"] == "context-coordination"][:3])

    live = next(node for node in tree.nodes() if node["node_id"] == "live")

    assert (live["importance"], live["child_count"]) == ("routine", 3)


def test_summary_reparents_covered_records_and_takes_max_importance(
    tmp_path: Path,
) -> None:
    records = _records()
    first_window = [r for r in records if r["sequence"] <= 30]
    summaries = _summaries_via_producer(tmp_path, [first_window])
    assert [(s.input_start_sequence, s.input_end_sequence) for s in summaries] == [
        (1, 30)
    ]
    tree = ProgressTree()
    tree.ingest(records)
    before = tree.page(limit=1000)

    tree.ingest(summaries=summaries)
    changed = tree.page(after=before.watermark, limit=1000)

    ids = [node["node_id"] for node in changed.nodes]
    covered = {f"log:{r['sequence']}" for r in first_window}
    assert ids[0] == "summary:1"  # parent before re-parented children
    assert set(ids) == {"summary:1", "live"} | covered
    by_id = {node["node_id"]: node for node in changed.nodes}
    assert {by_id[node_id]["parent_id"] for node_id in covered} == {"summary:1"}
    summary = by_id["summary:1"]
    assert summary["importance"] == "notable"
    assert summary["child_count"] == len(first_window)
    assert summary["authoritative"] is False
    summary_text = summary["text"]
    assert isinstance(summary_text, str)
    assert summary["title"] == summary_text.splitlines()[0]
    assert summary["time_start"] == first_window[0]["event_time"]
    assert summary["time_end"] == first_window[-1]["event_time"]
    assert by_id["live"]["child_count"] == len(records) - len(first_window)
    # Re-parenting changes only the parent, never the record content.
    old = {node["node_id"]: node for node in before.nodes}
    assert {**old["log:18"], "parent_id": "summary:1"} == by_id["log:18"]


def test_summary_importance_rises_to_a_covered_warning(tmp_path: Path) -> None:
    window = [
        *[r for r in _records() if r["sequence"] <= 3],
        _record(
            "runtime",
            "summary-unavailable",
            "failed",
            sequence=4,
            operation="mission_summary",
        ),
    ]
    summaries = _summaries_via_producer(tmp_path, [window])
    tree = ProgressTree()
    tree.ingest(window, summaries)

    summary = next(node for node in tree.nodes() if node["node_id"] == "summary:1")

    assert summary["importance"] == "warning"


def test_records_arriving_after_their_summary_attach_directly() -> None:
    records = _records()[:10]
    tree = ProgressTree()
    tree.ingest(summaries=[SummaryArtifact.create(_MISSION, 1, 1, 10, (), "Startup.")])
    mark = tree.watermark

    tree.ingest(records)
    changed = tree.page(after=mark, limit=1000).nodes

    by_id = {node["node_id"]: node for node in changed}
    assert {by_id[f"log:{r['sequence']}"]["parent_id"] for r in records} == {
        "summary:1"
    }
    assert by_id["summary:1"]["child_count"] == 10
    assert "live" not in by_id  # the live node did not change


def test_incremental_pages_return_only_changes_without_duplicates() -> None:
    records = _records()
    tree = ProgressTree()
    tree.ingest(records[:40])
    first = tree.page(limit=1000)

    unchanged = tree.page(after=first.watermark, limit=10)
    assert (unchanged.nodes, unchanged.watermark, unchanged.has_more) == (
        [],
        first.watermark,
        False,
    )

    tree.ingest(records)  # first 40 are duplicates and must be ignored
    seen: list[str] = []
    watermark = first.watermark
    while True:
        page = tree.page(after=watermark, limit=7)
        seen.extend(str(node["node_id"]) for node in page.nodes)
        watermark = page.watermark
        if not page.has_more:
            break

    expected = {f"log:{r['sequence']}" for r in records[40:]} | {"live"}
    assert sorted(seen) == sorted(expected)
    assert watermark == tree.watermark
    with pytest.raises(ValueError):
        tree.page(after=tree.watermark + 1, limit=1)


def test_progress_payload_matches_the_v1_2_contract_shape() -> None:
    example = _contract("mission-run-operator-progress.response.json")["progress"]
    tree = ProgressTree()
    tree.ingest(
        _records()[:20],
        [SummaryArtifact.create(_MISSION, 1, 1, 10, (), "Startup done.")],
    )
    narrative = {
        "status": "available",
        "text": "Legs 1-2 complete.",
        "generated_at": "2026-08-27T14:02:00Z",
        "source_watermark": 20,
        "terminal": False,
        "evidence": None,
    }

    payload: dict[str, Any] = progress_payload(nodes=tree.nodes(), narrative=narrative)

    assert set(payload) == set(example)
    assert payload["mapping_version"] == example["mapping_version"]
    assert set(payload["narrative"]) == set(example["narrative"])
    example_keys = {node["level"]: set(node) for node in example["nodes"]}
    for node in payload["nodes"]:
        assert set(node) == example_keys[node["level"]]
    levels = {node["level"] for node in payload["nodes"]}
    assert levels == {"summary", "record", "live"}
    assert progress_payload(nodes=[], narrative=None)["narrative"] == {
        "status": "none",
        "text": None,
        "generated_at": None,
        "source_watermark": 0,
    }


def test_summary_loader_skips_invalid_files(tmp_path: Path) -> None:
    summaries = _summaries_via_producer(tmp_path, [_records()[:5]])
    mission_dir = tmp_path / "agent-storage" / "summaries" / _MISSION
    (mission_dir / "00000000000000000002.json").write_text(
        "{not json", encoding="utf-8"
    )

    assert load_mission_log_summaries(tmp_path / "agent-storage", _MISSION) == summaries
    assert load_mission_log_summaries(tmp_path / "missing", _MISSION) == ()
    with pytest.raises(ValueError):
        load_mission_log_summaries(tmp_path, "../escape")


# -- overview phase -------------------------------------------------------


def _steps(phase: Mapping[str, Any]) -> dict[str, tuple[object, object]]:
    return {step["id"]: (step["status"], step["detail"]) for step in phase["steps"]}


def test_executing_phase_matches_the_overview_contract_example() -> None:
    example = _contract("mission-run-operator-overview.response.json")["overview"][
        "phase"
    ]
    records = [
        *_records(),
        _record(
            "fsm-runner", "fsm", "superseded", sequence=176, plan_revision=3, state="s"
        ),
    ]
    stack = {
        "services": [
            {"name": f"svc-{i}", "required": True, "state": "ready"} for i in range(4)
        ]
    }

    phase = derive_phase(run={"status": "running"}, records=records, stack=stack)

    assert phase == example


def test_rejected_run_fails_at_intent_with_the_agent_reason() -> None:
    reason = (
        "'buy me a coffee' is a personal errand, not a bounded operational objective."
    )
    run = _contract("mission-runs.current.rejected.response.json")["mission_run"]
    run = {**run, "terminal_detail": {**run["terminal_detail"], "reason": reason}}
    records = [
        _record(
            "hyper-agent", "workflow", "started", sequence=1, operation="hyper_workflow"
        ),
        _record(
            "hyper-agent", "planning-intent", "rejected", sequence=2, reason=reason
        ),
    ]

    phase = derive_phase(run=run, records=records)

    assert phase["current"] == "terminal"
    assert _steps(phase) == {
        "stack": ("done", None),
        "intent": ("failed", reason),
        "planning": ("pending", None),
        "statechart": ("pending", None),
        "executing": ("pending", None),
        "terminal": ("failed", "mission_rejected"),
    }


def test_rejection_without_records_still_fails_at_intent() -> None:
    run = _contract("mission-runs.current.rejected.response.json")["mission_run"]

    statuses = {k: v[0] for k, v in _steps(derive_phase(run=run, records=[])).items()}

    assert statuses["intent"] == "failed"
    assert statuses["planning"] == "pending"


def test_terminal_phases_for_success_stack_failure_and_worker_failure() -> None:
    records = _records()
    succeeded = derive_phase(run={"status": "succeeded"}, records=records)
    assert succeeded["current"] == "terminal"
    assert {status for status, _ in _steps(succeeded).values()} == {"done"}

    stack_run = _contract("mission-runs.current.stack-failed.response.json")[
        "mission_run"
    ]
    stack_failed = _steps(derive_phase(run=stack_run, records=[]))
    assert stack_failed["stack"] == ("failed", "perception: health not ready in 900 s")
    assert stack_failed["intent"] == ("pending", None)

    worker_run = _contract("mission-runs.current.worker-failed.response.json")[
        "mission_run"
    ]
    planning_only = [r for r in records if r["sequence"] <= 7]
    worker_failed = _steps(derive_phase(run=worker_run, records=planning_only))
    assert worker_failed["intent"][0] == "done"
    assert worker_failed["planning"][0] == "failed"
    assert worker_failed["executing"] == ("pending", None)

    cancelled = _steps(
        derive_phase(
            run={
                "status": "cancelled",
                "terminal_classification": "cancelled_by_owner",
            },
            records=records,
        )
    )
    assert cancelled["executing"] == ("failed", "plan revision 2")
    assert cancelled["terminal"] == ("failed", "cancelled_by_owner")


def test_nonterminal_phase_marks_the_first_unfinished_step_active() -> None:
    records = [r for r in _records() if r["sequence"] <= 6]

    phase: dict[str, Any] = derive_phase(run={"status": "running"}, records=records)

    assert phase["current"] == "planning"
    assert [step["status"] for step in phase["steps"]] == [
        "done",
        "done",
        "active",
        "pending",
        "pending",
        "pending",
    ]
    queued: dict[str, Any] = derive_phase(run={"status": "queued"}, records=[])
    assert {step["status"] for step in queued["steps"]} == {"pending"}
