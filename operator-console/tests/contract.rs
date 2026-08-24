//! Slice C: fixture HTTP contract tests for the Runtime Host client.
//!
//! These run against the Rust fixture server in `tests/support` - no Python
//! process is involved.

mod support;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use operator_console::host::{
    ActivationOutcome, ActivationRequest, HostClient, HostError, UreqHostClient,
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
