from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from onr.viewer import TraceProjection, TraceViewItem, load_trace_fixture, sanitize_payload


def _event(
    event_id: str,
    sequence: int,
    event_kind: str = "public-event",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "mission_id": "mission-test",
        "sequence": sequence,
        "event_kind": event_kind,
        "payload": payload or {},
    }


def _observation(sequence: int, record: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "observation_sequence": sequence,
        "observed_at": f"2026-01-01T00:00:{sequence:02d}Z",
        "record": record,
    }


def test_sanitize_and_trace_round_trip_use_distinct_replay_disposition() -> None:
    payload, redacted = sanitize_payload(
        {
            "action": "search_area",
            "text": "raw completion",
            "nested": {"messages": ["private"], "analysis": "reasoning", "token": "x"},
        }
    )
    assert payload == {"action": "search_area", "nested": {}}
    assert {"text", "nested.messages", "nested.analysis", "nested.token"}.issubset(redacted)
    item = TraceViewItem(
        trace_id="t", event_id="e", mission_id="m", sequence=1,
        occurred_at="2026-01-01", component="runtime", authority="log",
        event_kind="event", status="ready", replay_disposition="stale", payload=payload,
    )
    assert TraceViewItem.from_dict(item.to_dict()) == item
    assert item.status == "ready" and item.replay_disposition == "stale"
    with pytest.raises(ValueError, match="replay disposition"):
        TraceViewItem.from_dict({**item.to_dict(), "replay_disposition": "unknown"})


def test_explicit_adapters_reject_caller_identity_and_never_render_adversarial_values() -> None:
    projection = TraceProjection()
    valid = _event(
        "safe-event",
        1,
        payload={
            "action": "navigate",
            "text": "raw prompt",
            "messages": [{"role": "system", "content": "credential"}],
            "analysis": "private reasoning",
            "arbitrary": "must not pass through",
        },
    )
    injected = {**valid, "event_id": "injected", "authority": "caller-secret"}
    items = projection.project([valid, injected])
    public = next(item for item in items if item.event_id == "safe-event")
    assert public.component == "transport" and public.authority == "transport"
    assert public.payload == {"action": "navigate"}
    rendered = json.dumps([item.to_dict() for item in items]).lower()
    for private in ("raw prompt", "credential", "private reasoning", "arbitrary", "caller-secret"):
        assert private not in rendered
    assert any(item.replay_disposition == "malformed" for item in items)


def test_unsupported_schema_is_explicit_error_evidence() -> None:
    [item] = TraceProjection().project({**_event("future", 1), "schema_version": "prompt sk-secret-value"})
    assert item.event_kind == "error"
    assert item.replay_disposition == "malformed"
    assert item.payload["error_code"] == "unsupported_schema"
    assert item.missing_fields == ("source_record",)


def test_typed_feedback_and_replan_records_have_view_components_and_safe_links() -> None:
    feedback = {
        "schema_version": 1,
        "feedback_id": "feedback-1",
        "mission_id": "mission-test",
        "maneuver_id": "maneuver-1",
        "lifecycle": "completed",
        "source_sequence": 7,
        "source": "environment",
        "command_id": "command-1",
        "correlation_id": "correlation-1",
        "parent_id": "command:command-1",
        "plan_revision": 3,
        "snapshot_id": "snapshot-2",
    }
    replan = {
        "schema_version": 1,
        "request_id": "request-1",
        "mission_id": "mission-test",
        "reason": "scene revision changed",
        "requester": "maneuver-control",
        "observed_plan_revision": 3,
        "source_revisions": {"scene": 4, "belief": 2},
        "source_sequence": 8,
        "correlation_id": "request-1",
        "parent_id": "replan-wire-1",
        "status": "requested",
        "snapshot_id": "snapshot-2",
    }

    feedback_item, replan_item = TraceProjection().project(
        [_observation(1, feedback), _observation(2, replan)]
    )

    assert feedback_item.event_id == "feedback:feedback-1"
    assert feedback_item.component == "environment"
    assert feedback_item.authority == "environment-feedback"
    assert feedback_item.status == feedback_item.outcome == "completed"
    assert feedback_item.correlation_id == "correlation-1"
    assert feedback_item.parent_id == "command:command-1"
    assert feedback_item.payload == {
        "command_id": "command-1",
        "feedback_id": "feedback-1",
        "lifecycle": "completed",
        "maneuver_id": "maneuver-1",
        "plan_revision": 3,
        "snapshot_id": "snapshot-2",
        "source": "environment",
    }
    assert replan_item.event_id == "replan-request:request-1"
    assert replan_item.component == "hyper-agent"
    assert replan_item.authority == "hyper-agent"
    assert replan_item.status == "requested"
    assert replan_item.correlation_id == "request-1"
    assert replan_item.parent_id == "replan-wire-1"
    assert replan_item.payload == {
        "observed_plan_revision": 3,
        "reason": "scene revision changed",
        "request_id": "request-1",
        "requester": "maneuver-control",
        "snapshot_id": "snapshot-2",
        "source_revisions": {"belief": 2, "scene": 4},
    }


def test_belief_events_keep_only_public_typed_payload_fields() -> None:
    content_hash = "a" * 64
    records = [
        _event(
            "risk-1",
            1,
            "risk.observed",
            {
                "event_id": "risk-1",
                "input_revision": 1,
                "risk_type": "collision",
                "associations": [{"entity_id": "contact-1", "weight": 1.0}],
                "likelihood_given_risk": 0.9,
                "likelihood_given_safe": 0.1,
                "analysis": "private reasoning",
            },
        ),
        _event(
            "constraints-2",
            2,
            "belief.constraints",
            {
                "input_revision": 2,
                "constraints": [
                    {
                        "constraint_id": "not-both",
                        "assignments": [
                            {
                                "key": {
                                    "entity_id": "contact-1",
                                    "risk_type": "collision",
                                },
                                "is_risk": True,
                            }
                        ],
                    }
                ],
                "prompt": "private constraint prompt",
            },
        ),
        _event(
            "belief-3",
            3,
            "belief.updated",
            {
                "source": "bayesian_belief_snapshot",
                "revision": 3,
                "reference": "bayesian-beliefs/mission-test/belief-v1.json",
                "content_sha256": content_hash,
                "health": "healthy",
                "fresh": True,
                "token": "private-token",
            },
        ),
    ]

    items = TraceProjection().project(records)

    assert len(items) == 3
    assert all(item.component == "environment" for item in items)
    assert all(item.authority == "bayesian-belief-source" for item in items)
    by_kind = {item.event_kind: item.to_dict()["payload"] for item in items}
    assert by_kind["risk.observed"] == {
        "associations": [{"entity_id": "contact-1", "weight": 1.0}],
        "event_id": "risk-1",
        "input_revision": 1,
        "likelihood_given_risk": 0.9,
        "likelihood_given_safe": 0.1,
        "risk_type": "collision",
    }
    assert by_kind["belief.constraints"] == {
        "constraints": [
            {
                "assignments": [
                    {
                        "is_risk": True,
                        "key": {
                            "entity_id": "contact-1",
                            "risk_type": "collision",
                        },
                    }
                ],
                "constraint_id": "not-both",
            }
        ],
        "input_revision": 2,
    }
    assert by_kind["belief.updated"] == {
        "content_sha256": content_hash,
        "fresh": True,
        "health": "healthy",
        "reference": "bayesian-beliefs/mission-test/belief-v1.json",
        "revision": 3,
        "source": "bayesian_belief_snapshot",
    }
    rendered = json.dumps([item.to_dict() for item in items])
    assert "private reasoning" not in rendered
    assert "private constraint prompt" not in rendered
    assert "private-token" not in rendered


def test_error_diagnostics_never_emit_source_derived_strings() -> None:
    adversarial = [
        {**_event("bad-fields", 1), "prompt_sk-secret-field": "secret-value"},
        {**_event("bad-schema", 2), "schema_version": "prompt sk-secret-schema"},
        {**_event("bad-value", 3), "sequence": "analysis sk-secret-sequence"},
        '{"schema_version": 1, "prompt_sk-secret-json": ',
    ]
    items = TraceProjection().project(adversarial)
    assert all(item.event_kind == "error" for item in items)
    rendered = json.dumps([item.to_dict() for item in items], sort_keys=True).lower()
    for forbidden in ("prompt", "sk-secret", "secret-value", "analysis", "bad-fields", "bad-schema", "bad-value"):
        assert forbidden not in rendered
    assert {item.payload["error_code"] for item in items} == {
        "unknown_fields", "unsupported_schema", "invalid_record", "malformed_json",
    }


def test_operational_log_and_summary_artifact_map_real_public_fields() -> None:
    operational = {
        "schema_version": 1,
        "record_id": "log-1",
        "mission_id": "mission-test",
        "sequence": 1,
        "event_time": "2026-01-01T00:00:00Z",
        "source": "maneuver-control",
        "event_kind": "control",
        "outcome": "completed",
        "details": {"operation": "select", "maneuver_id": "m-1", "status": "ready"},
    }
    summary = {
        "schema_version": 1,
        "summary_id": "summary-1",
        "mission_id": "mission-test",
        "sequence": 1,
        "created_at": "2026-01-01T00:00:01Z",
        "input_start_sequence": 3,
        "input_end_sequence": 9,
        "prior_summary_ids": ["summary-0"],
        "summary": "Public operational summary.",
    }
    items = TraceProjection().project([_observation(1, operational), _observation(2, summary)])
    log = next(item for item in items if item.event_id == "log-1")
    projected_summary = next(item for item in items if item.event_id == "summary-1")
    assert log.component == "maneuver-control" and log.authority == "operational-log"
    assert log.payload == {"maneuver_id": "m-1", "operation": "select", "status": "ready"}
    assert log.missing_fields == ()
    assert projected_summary.payload == {
        "input_end_sequence": 9,
        "input_start_sequence": 3,
        "prior_summary_ids": ("summary-0",),
        "summary": "Public operational summary.",
    }


def test_operational_log_projects_current_public_planning_metadata() -> None:
    operational = {
        "schema_version": 1,
        "record_id": "planning-log-1",
        "mission_id": "mission-test",
        "sequence": 1,
        "event_time": "2026-01-01T00:00:00Z",
        "source": "hyper-agent",
        "event_kind": "planning-intent",
        "outcome": "completed",
        "details": {
            "attempt_id": "attempt-1",
            "decision_id": "decision-1",
            "mission_input_sha256": "1" * 64,
            "mission_snapshot_id": "mission-test:snapshot:1",
            "planner_id": "minizinc",
            "planning_intent_sha256": "2" * 64,
            "planning_profile": "temporal",
            "rationale": "Temporal constraints require scheduling.",
            "translator_id": "hyper-agent",
            "translator_version": "1",
            "private_prompt": "must not be projected",
        },
    }

    (item,) = TraceProjection().project(_observation(1, operational))

    assert item.payload == {
        "attempt_id": "attempt-1",
        "decision_id": "decision-1",
        "mission_input_sha256": "1" * 64,
        "mission_snapshot_id": "mission-test:snapshot:1",
        "planner_id": "minizinc",
        "planning_intent_sha256": "2" * 64,
        "planning_profile": "temporal",
        "rationale": "Temporal constraints require scheduling.",
        "translator_id": "hyper-agent",
        "translator_version": "1",
    }
    assert "private_prompt" not in item.payload
    assert "must not be projected" not in json.dumps(item.to_dict())


def test_projection_is_permutation_invariant_and_preserves_replay_evidence() -> None:
    first = _event("first", 1, payload={"status": "accepted"})
    late = _event("late", 2, "late-record", {"status": "received"})
    conflict_a = _event("conflict", 3, payload={"action": "navigate"})
    conflict_b = _event("conflict", 3, payload={"action": "land"})
    resync = _event("resync", 4, "resynchronized", {"resume_sequence": 4})
    after_gap = _event("after-gap", 6)
    replay_a = _event("replay-a", 7)
    replay_b = _event("replay-b", 7)
    inputs: list[object] = [
        first, first, late, conflict_b, resync, after_gap, replay_b, conflict_a,
        replay_a, "not-json", ["not", "a", "mapping"],
    ]
    forward = TraceProjection().project(inputs)  # type: ignore[arg-type]
    reverse = TraceProjection().project(list(reversed(inputs)))  # type: ignore[arg-type]
    assert [item.to_dict() for item in forward] == [item.to_dict() for item in reverse]

    dispositions = [item.replay_disposition for item in forward]
    assert "duplicate" in dispositions
    assert "replayed" in dispositions
    assert "conflict" in dispositions
    assert "gap" in dispositions
    assert "resynchronized" in dispositions
    assert "stale" in dispositions
    assert dispositions.count("malformed") == 2
    assert next(item for item in forward if item.event_id == "first").status == "accepted"
    conflict_ids = [item.event_id for item in forward if item.replay_disposition == "conflict"]
    assert len(conflict_ids) == len(set(conflict_ids)) == 1
    assert any(item.replay_disposition == "gap" for item in forward)


def test_missing_markers_are_record_type_specific() -> None:
    command_without_payload = {
        "schema_version": 1,
        "command_id": "command-1",
        "correlation_id": "corr-1",
        "mission_id": "mission-test",
        "target_service": "maneuver-adapter",
        "command_kind": "maneuver",
    }
    valid_command = {**command_without_payload, "payload": {}}
    missing_summary = {
        "schema_version": 1,
        "record_id": "log-summary-missing",
        "mission_id": "mission-test",
        "sequence": 1,
        "event_time": "2026-01-01T00:00:00Z",
        "source": "runtime",
        "event_kind": "summary-unavailable",
        "outcome": "missing",
        "details": {"operation": "summary-heartbeat"},
    }
    items = TraceProjection().project([
        _observation(1, command_without_payload),
        _observation(2, valid_command),
        _observation(3, missing_summary),
    ])
    malformed = next(item for item in items if item.payload.get("error_code") == "unknown_fields")
    command = next(item for item in items if item.event_kind == "command")
    unavailable = next(item for item in items if item.event_id == "log-summary-missing")
    assert malformed.missing_fields == ("source_record",)
    assert command.missing_fields == ()
    assert unavailable.missing_fields == ("summary",)


def test_fixture_is_valid_public_and_covers_phase_one_contract() -> None:
    path = Path(__file__).parents[1] / "src/onr/viewer/fixtures/mission_trace.jsonl"
    items = load_trace_fixture(path)
    expected_order = [
        "overview-001", "hyper-002", "planner-select-003", "planner-execute-004",
        "plan-translate-005", "context-006", "mission-snapshot:mission-demo:1",
        "belief-008", "mission-snapshot:mission-demo:2", "decision-010", "adapter-011",
        "scene-012", "fanout-013", "control-feedback-014", "environment-feedback-015",
        "role-skills-016", "memory-017", "human-018", "catalogue-019", "choice-020",
        "redaction-021", "statechart:mission-demo:1",
    ]
    expected_order.extend([
        next(item.event_id for item in items if item.event_kind == "fsm-status"),
        "command:command-survey-1", "receipt:command-survey-1", "outcome:command-survey-1",
        "mission-demo:summary:1", "mission-demo:log:1",
    ])
    assert [item.event_id for item in items] == expected_order
    assert [item.observation_sequence for item in items] == list(range(1, 29))
    kinds = {item.event_kind for item in items}
    assert {
        "mission-overview", "hyper-agent", "planner-selection", "planner-execution",
        "normalized-plan", "context-coordination", "mission-snapshot", "bayesian-belief", "statechart",
        "fsm-status", "maneuver-decision", "maneuver-adapter", "operational-scene-graph",
        "transport-fan-out", "command", "command-receipt", "command-outcome",
        "control-to-hyper-replan", "environment-to-fsm-feedback", "role-skills-advisory",
        "mission-memory-isolation", "human-question", "physical-action-catalogue",
        "non-physical-choice", "summary", "summary-unavailable",
    }.issubset(kinds)
    rendered = json.dumps([item.to_dict() for item in items], sort_keys=True).lower()
    for label in ("navigate", "takeoff", "land", "search_area", "pursue", "investigate"):
        assert label in rendered
    assert '"non_physical_choice": "replan"' in rendered
    assert "bayesian-belief:1" in rendered
    assert "private prompt" not in rendered and "sk-" not in rendered
    assert any(item.redacted_fields for item in items)
    assert any(item.missing_fields == ("summary",) for item in items)
    summary = next(item for item in items if item.event_kind == "summary")
    assert summary.payload["input_start_sequence"] == 1
    assert summary.payload["input_end_sequence"] == 19

    identities = {item.event_kind: (item.component, item.authority) for item in items}
    assert identities["hyper-agent"] == ("hyper-agent", "hyper-agent")
    assert identities["planner-selection"] == ("planner", "planner")
    assert identities["context-coordination"] == ("context-coordination", "context-coordination")
    assert identities["maneuver-decision"] == ("maneuver-control", "maneuver-control")
    assert identities["maneuver-adapter"] == ("maneuver-adapter", "maneuver-adapter")
    assert identities["bayesian-belief"] == ("environment", "bayesian-belief-source")
    assert identities["role-skills-advisory"] == ("advisory-context", "advisory-context")
    belief_index = expected_order.index("belief-008")
    snapshot_index = expected_order.index("mission-snapshot:mission-demo:2")
    decision_index = expected_order.index("decision-010")
    assert belief_index < snapshot_index < decision_index
    snapshot = next(item for item in items if item.event_id == "mission-snapshot:mission-demo:2")
    assert snapshot.sequence == 2
    assert snapshot.payload["bayesian_belief_snapshot"] == "bayesian-belief:1"


def test_heterogeneous_inputs_require_envelopes_and_sort_globally() -> None:
    command = {
        "schema_version": 1, "command_id": "c", "correlation_id": "corr",
        "mission_id": "mission-test", "target_service": "maneuver-adapter",
        "command_kind": "maneuver", "payload": {},
    }
    raw = TraceProjection().project([_event("event", 1), command])
    assert all(item.payload.get("error_code") == "envelope_required" for item in raw)

    wrapped = [_observation(2, command), _observation(1, _event("event", 99))]
    projected = TraceProjection().project(wrapped)
    assert [item.event_id for item in projected] == ["event", "command:c"]
    assert [item.sequence for item in projected] == [99, 0]


def test_identical_source_record_in_distinct_envelopes_is_duplicate_not_conflict() -> None:
    source = _event("same-event", 7, payload={"status": "accepted"})
    observations = [
        {
            "schema_version": 1,
            "observation_sequence": 2,
            "observed_at": "2026-01-01T00:00:02Z",
            "record": source,
        },
        {
            "schema_version": 1,
            "observation_sequence": 1,
            "observed_at": "2026-01-01T00:00:01Z",
            "record": source,
        },
    ]

    items = TraceProjection().project(observations)

    assert [item.observation_sequence for item in items] == [1, 2]
    assert [item.observed_at for item in items] == [
        "2026-01-01T00:00:01Z",
        "2026-01-01T00:00:02Z",
    ]
    assert [item.replay_disposition for item in items] == ["normal", "duplicate"]
    assert items[0].event_id == "same-event"
    assert items[1].event_id == "duplicate:same-event:1"
    assert not any(item.replay_disposition == "conflict" for item in items)
    assert not any(item.replay_disposition == "gap" for item in items)


def test_normalized_plan_trace_explains_provenance_without_mission_spec() -> None:
    provenance = {
        "schema_version": 1,
        "mission_id": "mission-test",
        "source_authority": "mission-control",
        "mission_intent": {"reference": "mission-input:1", "sha256": "1" * 64},
        "planning_decision": {
            "reference": "planner-choice:1",
            "sha256": "2" * 64,
        },
        "operational_scene_graph": {
            "reference": "scene:1",
            "sha256": "3" * 64,
        },
        "generated_assets": {
            "model.mzn": {"reference": "model.mzn", "sha256": "4" * 64},
        },
        "solver_evidence": {
            "stdout": {"reference": "solver.stdout", "sha256": "5" * 64},
        },
    }
    record = _event(
        "normalized-plan:mission-test:1",
        1,
        event_kind="normalized-plan",
        payload={
            "outcome": "solved",
            "plan_revision": 1,
            "normalized_plan": {
                "plan_revision": 1,
                "mission_snapshot_id": "mission-test:snapshot:1",
                "planner_choice": {
                    "planning_profile": "temporal",
                    "planner_id": "minizinc",
                },
                "outcome": "solved",
                "maneuvers": [],
                "provenance": provenance,
            },
        },
    )

    (item,) = TraceProjection().project(record)

    normalized_plan = item.payload["normalized_plan"]
    assert isinstance(normalized_plan, Mapping)
    assert "mission_spec" not in normalized_plan
    assert normalized_plan["provenance"] == provenance
    assert item.component == "planner"
    assert item.authority == "planner"
