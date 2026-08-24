//! Slice C: fixture HTTP contract tests for the Runtime Host client.
//!
//! These run against the Rust fixture server in `tests/support` - no Python
//! process is involved.

mod support;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use operator_console::host::{
    ActivationOutcome, ActivationRequest, CancellationOutcome, CancellationRequest, HostClient,
    HostError, UreqHostClient,
};
use support::FixtureHost;

fn client(host: &FixtureHost) -> UreqHostClient {
    UreqHostClient::new(&host.url(), Duration::from_secs(2))
}

fn request(request_id: &str, intent: &str) -> ActivationRequest {
    ActivationRequest {
        activation_request_id: request_id.to_string(),
        console_session_id: "session-fixture".to_string(),
        mission_intent: intent.to_string(),
        source_authority: "operator_console".to_string(),
    }
}

#[test]
fn health_reports_ok_and_api_v1_0() {
    let host = FixtureHost::start();
    let health = client(&host).health().expect("health");
    assert_eq!(health.status, "ok");
    assert_eq!((health.api_version.major, health.api_version.minor), (1, 0));
}

#[test]
fn health_wire_body_matches_contract_exactly() {
    let host = FixtureHost::start();
    let mut stream = TcpStream::connect(host.url().trim_start_matches("http://")).unwrap();
    stream
        .write_all(b"GET /api/v1/health HTTP/1.1\r\nhost: localhost\r\nconnection: close\r\n\r\n")
        .unwrap();
    let mut raw = String::new();
    stream.read_to_string(&mut raw).unwrap();
    assert!(raw.starts_with("HTTP/1.1 200 OK\r\n"));
    assert!(raw.ends_with("{\"status\":\"ok\",\"api_version\":{\"major\":1,\"minor\":0}}"));
}

#[test]
fn activation_is_accepted_asynchronously_with_queued_run() {
    let host = FixtureHost::start();
    let outcome = client(&host)
        .activate(&request("req-1", "survey the ridge"), "cred-1")
        .expect("activate");
    let ActivationOutcome::Accepted(accepted) = outcome else {
        panic!("expected acceptance, got {outcome:?}");
    };
    assert_eq!(accepted.activation_request_id, "req-1");
    assert_eq!(accepted.mission_id, "mission-fixture-001");
    assert_eq!(accepted.mission_run_id, "run-fixture-001");
    assert_eq!(accepted.status, "queued");
    assert_eq!(accepted.created_at, "2026-08-24T12:00:00Z");
    assert_eq!(host.last_authorization().as_deref(), Some("Bearer cred-1"));
}

#[test]
fn activation_retry_returns_original_acceptance() {
    let host = FixtureHost::start();
    let client = client(&host);
    let first = client.activate(&request("req-1", "survey the ridge"), "cred-1");
    let second = client.activate(&request("req-1", "survey the ridge"), "cred-1");
    assert_eq!(first.unwrap(), second.unwrap());
}

#[test]
fn conflicting_reuse_of_request_id_is_rejected() {
    let host = FixtureHost::start();
    let client = client(&host);
    client
        .activate(&request("req-1", "survey the ridge"), "cred-1")
        .unwrap();
    let outcome = client
        .activate(&request("req-1", "a different intent"), "cred-1")
        .unwrap();
    assert_eq!(
        outcome,
        ActivationOutcome::Rejected {
            code: "activation_request_conflict".to_string(),
            message: "activation_request_id was reused with a different body or credential"
                .to_string(),
        }
    );
}

#[test]
fn second_activation_while_run_is_non_terminal_is_rejected() {
    let host = FixtureHost::start();
    let client = client(&host);
    client
        .activate(&request("req-1", "survey the ridge"), "cred-1")
        .unwrap();
    let outcome = client
        .activate(&request("req-2", "hold position"), "cred-1")
        .unwrap();
    assert!(matches!(
        outcome,
        ActivationOutcome::Rejected { ref code, .. } if code == "mission_run_active"
    ));
}

#[test]
fn activation_after_terminal_run_is_admitted() {
    let host = FixtureHost::start();
    let client = client(&host);
    client
        .activate(&request("req-1", "survey the ridge"), "cred-1")
        .unwrap();
    host.finish_run("succeeded", None);
    let outcome = client
        .activate(&request("req-2", "hold position"), "cred-1")
        .unwrap();
    let ActivationOutcome::Accepted(accepted) = outcome else {
        panic!("expected acceptance, got {outcome:?}");
    };
    assert_eq!(accepted.mission_run_id, "run-fixture-002");
}

#[test]
fn invalid_json_returns_422_with_stable_code() {
    let host = FixtureHost::start();
    let mut stream = TcpStream::connect(host.url().trim_start_matches("http://")).unwrap();
    let body = b"{not json";
    let request = format!(
        "POST /api/v1/mission-activations HTTP/1.1\r\nhost: localhost\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(request.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut raw = String::new();
    stream.read_to_string(&mut raw).unwrap();
    assert!(raw.starts_with("HTTP/1.1 422 Unprocessable Entity\r\n"));
    assert!(raw.contains("\"code\":\"invalid_request\""));
    assert!(!raw.contains("Traceback"));
}

#[test]
fn missing_fields_return_422_with_stable_code() {
    let host = FixtureHost::start();
    let body = serde_json::json!({
        "activation_request_id": "req-1",
        "mission_intent": "no session or authority"
    })
    .to_string();
    let mut stream = TcpStream::connect(host.url().trim_start_matches("http://")).unwrap();
    let request = format!(
        "POST /api/v1/mission-activations HTTP/1.1\r\nhost: localhost\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(request.as_bytes()).unwrap();
    stream.write_all(body.as_bytes()).unwrap();
    let mut raw = String::new();
    stream.read_to_string(&mut raw).unwrap();
    assert!(raw.starts_with("HTTP/1.1 422 Unprocessable Entity\r\n"));
    assert!(raw.contains("\"code\":\"invalid_request\""));
}

#[test]
fn current_run_is_null_before_activation() {
    let host = FixtureHost::start();
    let current = client(&host).current_run("cred-1").expect("current run");
    assert_eq!(current.mission_run, None);
}

#[test]
fn current_run_reports_awaiting_human_decision_then_running() {
    let host = FixtureHost::start();
    let client = client(&host);

    let empty = client
        .current_run("cred-evidence")
        .expect("empty current run");
    assert_eq!(empty.mission_run, None);

    activate_fixture_run(&client);
    host.await_human_decision();
    let awaiting = client
        .current_run("cred-evidence")
        .expect("awaiting current run")
        .mission_run
        .expect("awaiting mission run");
    assert_eq!(awaiting.status, "awaiting_human_decision");

    host.promote_to_running();
    let running = client
        .current_run("cred-evidence")
        .expect("running current run")
        .mission_run
        .expect("running mission run");
    assert_eq!(running.status, "running");
}

#[test]
fn current_run_reports_lifecycle_transitions() {
    let host = FixtureHost::start();
    let client = client(&host);
    client
        .activate(&request("req-1", "survey the ridge"), "cred-1")
        .unwrap();

    let queued = client.current_run("cred-1").unwrap().mission_run.unwrap();
    assert_eq!(queued.mission_id, "mission-fixture-001");
    assert_eq!(queued.mission_run_id, "run-fixture-001");
    assert_eq!(queued.status, "queued");
    assert_eq!(queued.started_at, None);
    assert_eq!(queued.finished_at, None);
    assert_eq!(queued.terminal_classification, None);

    host.promote_to_running();
    let running = client.current_run("cred-1").unwrap().mission_run.unwrap();
    assert_eq!(running.status, "running");
    assert_eq!(running.started_at.as_deref(), Some("2026-08-24T12:00:03Z"));

    host.finish_run("failed", Some("worker_exited"));
    let failed = client.current_run("cred-1").unwrap().mission_run.unwrap();
    assert_eq!(failed.status, "failed");
    assert_eq!(failed.finished_at.as_deref(), Some("2026-08-24T12:05:00Z"));
    assert_eq!(
        failed.terminal_classification.as_deref(),
        Some("worker_exited")
    );
}

#[test]
fn unreachable_host_is_a_transport_error() {
    let client = UreqHostClient::new("http://127.0.0.1:1", Duration::from_millis(300));
    match client.health() {
        Err(HostError::Transport(_)) => {}
        other => panic!("expected transport error, got {other:?}"),
    }
}

#[test]
fn owner_can_read_mission_intent_and_request_idempotent_cancellation() {
    let host = FixtureHost::start();
    let client = client(&host);
    let accepted = match client
        .activate(&request("req-owner", "hold the ridge"), "cred-owner")
        .unwrap()
    {
        ActivationOutcome::Accepted(accepted) => accepted,
        other => panic!("expected acceptance, got {other:?}"),
    };
    let intent = client
        .mission_intent(&accepted.mission_run_id, "cred-owner")
        .unwrap();
    assert_eq!(intent.mission_intent, "hold the ridge");
    assert_eq!(intent.source_authority, "operator_console");

    let cancellation = CancellationRequest {
        cancellation_request_id: "cancel-1".to_string(),
    };
    let first = client
        .cancel(&accepted.mission_run_id, &cancellation, "cred-owner")
        .unwrap();
    let second = client
        .cancel(&accepted.mission_run_id, &cancellation, "cred-owner")
        .unwrap();
    assert_eq!(first, second);
    assert!(matches!(first, CancellationOutcome::Accepted(_)));
}

#[test]
fn owner_endpoints_return_fixed_authorization_failure() {
    let host = FixtureHost::start();
    let client = client(&host);
    let accepted = match client
        .activate(&request("req-owner", "hold the ridge"), "cred-owner")
        .unwrap()
    {
        ActivationOutcome::Accepted(accepted) => accepted,
        other => panic!("expected acceptance, got {other:?}"),
    };
    assert!(matches!(
        client.mission_intent(&accepted.mission_run_id, "wrong"),
        Err(HostError::AuthorizationFailed { ref code, .. }) if code == "authorization_failed"
    ));
    assert!(matches!(
        client.cancel(
            &accepted.mission_run_id,
            &CancellationRequest { cancellation_request_id: "cancel-1".to_string() },
            "wrong",
        ),
        Err(HostError::AuthorizationFailed { ref code, .. }) if code == "authorization_failed"
    ));
}

fn activate_fixture_run(client: &UreqHostClient) -> String {
    match client
        .activate(
            &request("req-evidence", "survey the ridge"),
            "cred-evidence",
        )
        .unwrap()
    {
        ActivationOutcome::Accepted(accepted) => accepted.mission_run_id,
        other => panic!("expected acceptance, got {other:?}"),
    }
}

#[test]
fn observations_support_paging_and_stable_errors() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);

    let first = client.observations(&run_id, None).unwrap();
    assert_eq!(first.observations.len(), 3);
    let next = first.next_cursor.expect("page cursor");
    let empty = client.observations(&run_id, Some(&next)).unwrap();
    assert!(empty.observations.is_empty());
    assert_eq!(empty.next_cursor, None);
    assert!(matches!(
        client.observations(&run_id, Some("bogus")),
        Err(HostError::InvalidCursor { ref code, .. }) if code == "invalid_cursor"
    ));
    assert!(matches!(
        client.observations("run-unknown", None),
        Err(HostError::NotFound { ref code, .. }) if code == "mission_run_not_found"
    ));
}

#[test]
fn activities_support_paging_and_stable_errors() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);

    let first = client.activities(&run_id, None).unwrap();
    assert_eq!(first.activities.len(), 2);
    let next = first.next_cursor.expect("activity page cursor");
    assert_eq!(next, "eyJ2IjoxLCJydW4iOiJydW4tZml4dHVyZS0wMDEiLCJzZXEiOjJ9");
    let empty = client.activities(&run_id, Some(&next)).unwrap();
    assert_eq!(empty.mapping_version, 1);
    assert!(empty.activities.is_empty());
    assert_eq!(empty.next_cursor, None);
    assert!(matches!(
        client.activities(&run_id, Some("bogus")),
        Err(HostError::InvalidCursor { ref code, .. }) if code == "invalid_cursor"
    ));
    assert!(matches!(
        client.activities("run-unknown", None),
        Err(HostError::NotFound { ref code, .. }) if code == "mission_run_not_found"
    ));
}

#[test]
fn narrative_fetches_without_a_cursor_and_preserves_not_found_errors() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);

    let response = client.fetch_narrative(&run_id).unwrap();
    assert_eq!(response.narrative.status, "available");
    assert!(response.narrative.text.is_some());
    assert!(matches!(
        client.fetch_narrative("run-unknown"),
        Err(HostError::NotFound { ref code, .. }) if code == "mission_run_not_found"
    ));
}

#[test]
fn all_observations_accumulates_until_the_empty_page() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);
    let observations = client.all_observations(&run_id).unwrap();
    assert!(!observations.truncated);
    assert_eq!(observations.items.len(), 3);
    assert_eq!(observations.items[0].observation_sequence, 1);
    assert_eq!(observations.items[2].observation_sequence, 3);
}

#[test]
fn endless_evidence_is_retained_and_reported_as_truncated() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);
    host.enable_endless_evidence();

    let observations = client.all_observations(&run_id).unwrap();
    assert!(observations.truncated);
    assert_eq!(observations.items.len(), 300);
    let activities = client.all_activities(&run_id).unwrap();
    assert!(activities.truncated);
    assert_eq!(activities.items.len(), 200);

    let mut app = operator_console::app::App::new_with_session_file(
        host.url(),
        operator_console::app::SessionStateFile::at(
            std::env::temp_dir()
                .join(format!(
                    "operator-console-truncated-{}",
                    uuid::Uuid::new_v4()
                ))
                .join("session.json"),
        ),
    );
    app.take_commands();
    app.handle_host_message(operator_console::app::HostMessage::Observations(Ok(
        observations,
    )));
    assert_eq!(app.observations.len(), 300);
    assert!(app.observations_truncated);
    assert_eq!(
        app.notice.as_deref(),
        Some("Showing the first 300 evidence entries; the Host retains the full timeline")
    );
}

#[test]
fn artifacts_support_paging_collection_and_stable_errors() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);

    let first = client.artifacts(&run_id, None).unwrap();
    assert_eq!(first.artifacts.len(), 3);
    assert_eq!(first.artifacts[0].artifact_id, "detection-frame");
    let next = first.next_cursor.expect("artifact page cursor");
    let empty = client.artifacts(&run_id, Some(&next)).unwrap();
    assert!(empty.artifacts.is_empty());
    assert_eq!(empty.next_cursor, None);
    let all = client.all_artifacts(&run_id).unwrap();
    assert!(!all.truncated);
    assert_eq!(all.items.len(), 3);
    assert!(matches!(
        client.artifacts(&run_id, Some("bogus")),
        Err(HostError::InvalidCursor { ref code, .. }) if code == "invalid_cursor"
    ));
    assert!(matches!(
        client.artifacts("run-unknown", None),
        Err(HostError::NotFound { ref code, .. }) if code == "mission_run_not_found"
    ));
}

#[test]
fn artifact_content_supports_text_paging_binary_metadata_and_stable_errors() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);

    let first = client
        .artifact_content(&run_id, "planner-log", None, Some(4096))
        .unwrap();
    assert_eq!(first.offset, 0);
    assert_eq!(first.next_offset, Some(4096));
    assert!(!first.eof);
    assert!(
        first
            .content
            .as_deref()
            .unwrap()
            .contains("expanded 42 states")
    );
    let final_page = client
        .artifact_content(&run_id, "planner-log", Some(4096), Some(4096))
        .unwrap();
    assert_eq!(final_page.offset, 4096);
    assert!(final_page.eof);
    assert_eq!(final_page.next_offset, None);
    let binary = client
        .artifact_content(&run_id, "detection-frame", None, Some(4096))
        .unwrap();
    assert_eq!(binary.classification, "binary");
    assert_eq!(binary.byte_size, Some(204800));
    assert_eq!(binary.content, None);
    assert!(matches!(
        client.artifact_content(&run_id, "unknown", None, Some(4096)),
        Err(HostError::NotFound { ref code, .. }) if code == "artifact_not_found"
    ));
    assert!(matches!(
        client.artifact_content(&run_id, "planner-log", Some(999999), Some(4096)),
        Err(HostError::InvalidRequest { ref code, .. }) if code == "invalid_request"
    ));
}

#[test]
fn conversation_entries_support_paging_gaps_refs_and_stable_errors() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate_fixture_run(&client);

    let first = client
        .conversation_entries(&run_id, "operator-conversation", None)
        .unwrap();
    assert_eq!(
        first
            .entries
            .iter()
            .map(|entry| entry.sequence)
            .collect::<Vec<_>>(),
        vec![1, 2, 4]
    );
    let reference = first.entries[2].content_ref.as_ref().unwrap();
    assert_eq!(reference.path, "evidence/rationale-4.txt");
    let next = first.next_cursor.expect("entries page cursor");
    let empty = client
        .conversation_entries(&run_id, "operator-conversation", Some(&next))
        .unwrap();
    assert!(empty.entries.is_empty());
    assert_eq!(empty.next_cursor, None);
    let all = client
        .all_conversation_entries(&run_id, "operator-conversation")
        .unwrap();
    assert_eq!(all.items.len(), 3);
    assert!(matches!(
        client.conversation_entries(&run_id, "operator-conversation", Some("bogus")),
        Err(HostError::InvalidCursor { ref code, .. }) if code == "invalid_cursor"
    ));
}
