//! The committed #27 v1 wire-contract examples under
//! `docs/design/operator-console/contract/v1/` are the single source of truth
//! for the console's HTTP client shapes. Each example must round-trip through
//! the client DTOs with exact value equality, and the fixture HTTP server
//! (tests/support) serves these same bytes/shapes.
//!
//! Real interoperability against the Python Runtime Host process is validated
//! at parent level by the Python Host tests; this lane guarantees the Rust
//! fixture reflects the precise #27 contract, not a divergent schema.

use operator_console::host::{
    ActivationAccepted, ActivationRequest, CurrentRun, ErrorBody, Health,
};
use serde_json::Value;

const DIR: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../docs/design/operator-console/contract/v1/"
);

fn example(name: &str) -> Value {
    let path = format!("{DIR}{name}");
    let raw = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    serde_json::from_str(raw.trim_end()).unwrap_or_else(|e| panic!("parse {path}: {e}"))
}

/// Parse a committed example into `T` and assert the DTO covers exactly the
/// example's fields - no missing, no extra.
fn exact_roundtrip<T>(name: &str) -> T
where
    T: serde::de::DeserializeOwned + serde::Serialize + std::fmt::Debug,
{
    let value = example(name);
    let dto: T =
        serde_json::from_value(value.clone()).unwrap_or_else(|e| panic!("decode {name}: {e}"));
    let back = serde_json::to_value(&dto).unwrap();
    assert_eq!(
        back, value,
        "{name} must round-trip exactly through the DTO"
    );
    dto
}

#[test]
fn health_example_matches_issue_text_byte_for_byte() {
    let raw = std::fs::read_to_string(format!("{DIR}health.response.json")).unwrap();
    assert_eq!(
        raw.trim_end(),
        r#"{"status":"ok","api_version":{"major":1,"minor":0}}"#
    );
    let health: Health = exact_roundtrip("health.response.json");
    assert_eq!(health.status, "ok");
    assert_eq!((health.api_version.major, health.api_version.minor), (1, 0));
}

#[test]
fn activation_request_uses_exactly_the_contract_fields() {
    let request: ActivationRequest = exact_roundtrip("mission-activation.request.json");
    let value = example("mission-activation.request.json");
    let mut keys: Vec<&str> = value
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        [
            "activation_request_id",
            "console_session_id",
            "mission_intent",
            "source_authority"
        ]
    );
    assert_eq!(request.source_authority, "operator_console");
    assert!(request.mission_intent.contains('\n'));
}

#[test]
fn accepted_example_decodes_with_generated_ids_and_queued_status() {
    let accepted: ActivationAccepted = exact_roundtrip("mission-activation.accepted.response.json");
    assert_eq!(accepted.status, "queued");
    assert!(!accepted.activation_request_id.is_empty());
    assert!(!accepted.mission_id.is_empty());
    assert!(!accepted.mission_run_id.is_empty());
    assert!(!accepted.created_at.is_empty());
}

#[test]
fn conflict_examples_carry_stable_machine_readable_codes() {
    let conflict: ErrorBody = exact_roundtrip("mission-activation.conflict.response.json");
    assert_eq!(conflict.error.code, "activation_request_conflict");
    let run_active: ErrorBody = exact_roundtrip("mission-activation.run-active.response.json");
    assert_eq!(run_active.error.code, "mission_run_active");
}

#[test]
fn invalid_request_example_carries_stable_code_without_exception_text() {
    let invalid: ErrorBody = exact_roundtrip("mission-activation.invalid.response.json");
    assert_eq!(invalid.error.code, "invalid_request");
    assert!(!invalid.error.message.contains("Traceback"));
}

#[test]
fn current_run_none_example_is_an_explicit_null() {
    let current: CurrentRun = exact_roundtrip("mission-runs.current.none.response.json");
    assert_eq!(current.mission_run, None);
}

#[test]
fn current_run_active_example_has_identifiers_status_and_nullable_classification() {
    let current: CurrentRun = exact_roundtrip("mission-runs.current.active.response.json");
    let run = current.mission_run.expect("active example has a run");
    assert!(!run.mission_id.is_empty());
    assert!(!run.mission_run_id.is_empty());
    assert_eq!(run.status, "running");
    assert!(run.created_at.is_some());
    assert!(run.started_at.is_some());
    assert_eq!(run.finished_at, None);
    assert_eq!(run.terminal_classification, None);
}
