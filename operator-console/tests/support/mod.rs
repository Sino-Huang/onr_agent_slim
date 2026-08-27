//! A deterministic in-process fixture of the Runtime Host v1 HTTP contract.
//!
//! Used by contract tests so the console client is proven without a Python
//! process. Behavior mirrors the bounded v1 surface from issue #27:
//!
//! - `GET /api/v1/health` -> `200 {"status":"ok","api_version":{"major":1,"minor":0}}`
//! - `POST /api/v1/mission-activations` -> `202` queued acceptance; same
//!   request id + body + credential replays the original acceptance; conflicting
//!   reuse -> `409 activation_request_conflict`; another non-terminal run ->
//!   `409 mission_run_active`.
//! - `GET /api/v1/mission-runs/current` -> `{"mission_run":null}` or the record.
//! - Invalid strict JSON -> `422` with a stable machine-readable code.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::{Arc, Mutex};

use serde_json::{Value, json};

/// Committed #27 v1 contract examples; the fixture serves these bytes (static
/// bodies) or shapes (dynamic bodies) so it cannot drift from the contract.
const HEALTH_RESPONSE: &str =
    include_str!("../../../docs/design/operator-console/contract/v1/health.response.json");
const ACCEPTED_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-activation.accepted.response.json"
);
const CONFLICT_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-activation.conflict.response.json"
);
const RUN_ACTIVE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-activation.run-active.response.json"
);
const INVALID_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-activation.invalid.response.json"
);
const CURRENT_NONE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-runs.current.none.response.json"
);
const CURRENT_ACTIVE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-runs.current.active.response.json"
);
const CURRENT_AWAITING_HUMAN_DECISION_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-runs.current.awaiting-human-decision.response.json"
);
const INTENT_RESPONSE: &str =
    include_str!("../../../docs/design/operator-console/contract/v1/mission-intent.response.json");
const CANCELLATION_ACCEPTED_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-cancellation.accepted.response.json"
);
const CANCELLATION_CONFLICT_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-cancellation.conflict.response.json"
);
const AUTHORIZATION_FAILED_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-owner.authorization-failed.response.json"
);
const OBSERVATIONS_PAGE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-observations.page.response.json"
);
const OBSERVATIONS_EMPTY_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-observations.empty.response.json"
);
const ACTIVITIES_PAGE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-activities.page.response.json"
);
const ACTIVITIES_EMPTY_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-activities.empty.response.json"
);
const INVALID_CURSOR_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-observations.invalid-cursor.response.json"
);
const RUN_NOT_FOUND_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run.not-found.response.json"
);
const NARRATIVE_AVAILABLE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-narrative.available.response.json"
);
const ARTIFACTS_PAGE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifacts.page.response.json"
);
const ARTIFACTS_EMPTY_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifacts.empty.response.json"
);
const ARTIFACT_CONTENT_TEXT_PAGE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifact-content.text-page.response.json"
);
const ARTIFACT_CONTENT_TEXT_FINAL_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifact-content.text-final.response.json"
);
const ARTIFACT_CONTENT_BINARY_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifact-content.binary.response.json"
);
const ARTIFACT_ENTRIES_PAGE_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifact-entries.page.response.json"
);
const ARTIFACT_ENTRIES_EMPTY_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifact-entries.empty.response.json"
);
const ARTIFACT_NOT_FOUND_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1/mission-run-artifact.not-found.response.json"
);
const OPERATOR_OVERVIEW_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1.1/mission-run-operator-overview.response.json"
);
const OPERATOR_AGENTS_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1.1/mission-run-operator-agents.response.json"
);
const OPERATOR_ENVIRONMENT_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1.1/mission-run-operator-environment.response.json"
);
const OPERATOR_ARTIFACTS_RESPONSE: &str = include_str!(
    "../../../docs/design/operator-console/contract/v1.1/mission-run-operator-artifacts.response.json"
);

/// Build a dynamic body by substituting fixture values into a committed
/// contract example; panics if a substituted key is missing from the example.
fn from_example(example: &str, substitutions: &[(&str, Value)]) -> String {
    let mut value: Value =
        serde_json::from_str(example).expect("committed contract example parses");
    let target = if let Some(run) = value.get_mut("mission_run") {
        if run.is_null() {
            *run = json!({});
        }
        run
    } else {
        &mut value
    };
    for (key, replacement) in substitutions {
        let object = target
            .as_object_mut()
            .expect("contract example is an object");
        assert!(object.contains_key(*key), "contract example has key {key}");
        object.insert((*key).to_string(), replacement.clone());
    }
    value.to_string()
}

const TERMINAL_STATUSES: [&str; 3] = ["succeeded", "failed", "cancelled"];

#[derive(Debug, Clone)]
struct FixtureRun {
    mission_id: String,
    mission_run_id: String,
    status: String,
    created_at: Option<String>,
    started_at: Option<String>,
    finished_at: Option<String>,
    terminal_classification: Option<String>,
}

#[derive(Debug, Clone)]
struct StoredActivation {
    console_session_id: String,
    mission_intent: String,
    source_authority: String,
    credential: String,
    response_body: String,
}

#[derive(Debug, Default)]
struct State {
    activations: HashMap<String, StoredActivation>,
    run: Option<FixtureRun>,
    counter: u32,
    last_authorization: Option<String>,
    cancellation: Option<(String, String, String)>,
    endless_evidence: bool,
}

/// A running fixture host bound to an ephemeral loopback port.
pub struct FixtureHost {
    addr: SocketAddr,
    state: Arc<Mutex<State>>,
}

impl FixtureHost {
    /// Bind and serve on `127.0.0.1:0` in a background thread.
    pub fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind fixture host");
        let addr = listener.local_addr().expect("fixture addr");
        let state = Arc::new(Mutex::new(State::default()));
        let worker_state = Arc::clone(&state);
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                match stream {
                    Ok(stream) => handle_connection(stream, &worker_state),
                    Err(_) => break,
                }
            }
        });
        FixtureHost { addr, state }
    }

    /// Base URL like `http://127.0.0.1:PORT`.
    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    /// The last `Authorization` header the fixture observed.
    pub fn last_authorization(&self) -> Option<String> {
        self.state.lock().unwrap().last_authorization.clone()
    }

    /// Move the current run to `awaiting_human_decision`.
    pub fn await_human_decision(&self) {
        let mut state = self.state.lock().unwrap();
        if let Some(run) = state.run.as_mut() {
            run.status = "awaiting_human_decision".to_string();
            run.started_at = Some("2026-08-24T12:00:03Z".to_string());
            run.finished_at = None;
            run.terminal_classification = None;
        }
    }

    /// Move the current run to `running`.
    pub fn promote_to_running(&self) {
        let mut state = self.state.lock().unwrap();
        if let Some(run) = state.run.as_mut() {
            run.status = "running".to_string();
            run.started_at = Some("2026-08-24T12:00:03Z".to_string());
        }
    }

    /// Move the current run to a terminal status with a classification.
    pub fn finish_run(&self, status: &str, terminal_classification: Option<&str>) {
        let mut state = self.state.lock().unwrap();
        if let Some(run) = state.run.as_mut() {
            run.status = status.to_string();
            run.finished_at = Some("2026-08-24T12:05:00Z".to_string());
            run.terminal_classification = terminal_classification.map(str::to_string);
        }
    }

    /// Make every evidence request return another non-terminal page.
    pub fn enable_endless_evidence(&self) {
        self.state.lock().unwrap().endless_evidence = true;
    }
}

fn handle_connection(stream: TcpStream, state: &Arc<Mutex<State>>) {
    let _ = handle_request(stream, state);
}

fn handle_request(stream: TcpStream, state: &Arc<Mutex<State>>) -> std::io::Result<()> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line)? == 0 {
        return Ok(());
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let path = parts.next().unwrap_or_default().to_string();

    let mut content_length = 0usize;
    let mut authorization = None;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 || line == "\r\n" {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            let value = value.trim();
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.parse().unwrap_or(0);
            } else if name.eq_ignore_ascii_case("authorization") {
                authorization = Some(value.to_string());
            }
        }
    }
    let mut body = vec![0u8; content_length];
    reader.read_exact(&mut body)?;

    let (status, payload) = route(&method, &path, authorization, &body, state);
    let response = format!(
        "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{payload}",
        payload.len()
    );
    let mut stream = stream;
    stream.write_all(response.as_bytes())?;
    stream.flush()
}

fn route(
    method: &str,
    path: &str,
    authorization: Option<String>,
    body: &[u8],
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let (route_path, query) = path
        .split_once('?')
        .map_or((path, None), |(path, query)| (path, Some(query)));
    match (method, route_path) {
        ("GET", "/api/v1/health") => ("200 OK", HEALTH_RESPONSE.trim_end().to_string()),
        ("POST", "/api/v1/mission-activations") => activate(authorization, body, state),
        ("GET", "/api/v1/mission-runs/current") => {
            let state = state.lock().unwrap();
            let payload = match state.run.as_ref() {
                None => CURRENT_NONE_RESPONSE.trim_end().to_string(),
                Some(run) => {
                    let example = if run.status == "awaiting_human_decision" {
                        CURRENT_AWAITING_HUMAN_DECISION_RESPONSE
                    } else {
                        CURRENT_ACTIVE_RESPONSE
                    };
                    from_example(
                        example,
                        &[
                            ("mission_id", json!(run.mission_id)),
                            ("mission_run_id", json!(run.mission_run_id)),
                            ("status", json!(run.status)),
                            ("created_at", json!(run.created_at)),
                            ("started_at", json!(run.started_at)),
                            ("finished_at", json!(run.finished_at)),
                            (
                                "terminal_classification",
                                json!(run.terminal_classification),
                            ),
                        ],
                    )
                }
            };
            ("200 OK", payload)
        }
        ("GET", path) if path.ends_with("/mission-intent") => {
            owner_intent(path, authorization, state)
        }
        ("GET", path) if path.ends_with("/observations") => evidence_page(
            path,
            query,
            "observations",
            OBSERVATIONS_PAGE_RESPONSE,
            OBSERVATIONS_EMPTY_RESPONSE,
            state,
        ),
        ("GET", path) if path.ends_with("/activities") => evidence_page(
            path,
            query,
            "activities",
            ACTIVITIES_PAGE_RESPONSE,
            ACTIVITIES_EMPTY_RESPONSE,
            state,
        ),
        ("GET", path) if path.ends_with("/narrative") => narrative(path, state),
        ("GET", path) if path.ends_with("/operator-view") => operator_view(path, query, state),
        ("GET", path) if path.ends_with("/artifacts") => evidence_page(
            path,
            query,
            "artifacts",
            ARTIFACTS_PAGE_RESPONSE,
            ARTIFACTS_EMPTY_RESPONSE,
            state,
        ),
        ("GET", path) if path.ends_with("/content") && path.contains("/artifacts/") => {
            artifact_content(path, query, state)
        }
        ("GET", path) if path.ends_with("/entries") && path.contains("/artifacts/") => {
            artifact_entries(path, query, state)
        }
        ("POST", path) if path.ends_with("/cancellations") => {
            cancel(path, authorization, body, state)
        }
        _ => (
            "404 Not Found",
            json!({"error": {"code": "not_found", "message": "unknown route"}}).to_string(),
        ),
    }
}

fn operator_view(
    path: &str,
    query: Option<&str>,
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/operator-view");
    let state = state.lock().unwrap();
    if state
        .run
        .as_ref()
        .is_none_or(|run| run.mission_run_id != mission_run_id)
    {
        return (
            "404 Not Found",
            RUN_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    }
    let payload = match query_value(query, "section") {
        Some("overview") => OPERATOR_OVERVIEW_RESPONSE,
        Some("agents") => OPERATOR_AGENTS_RESPONSE,
        Some("environment") => OPERATOR_ENVIRONMENT_RESPONSE,
        Some("artifacts") => OPERATOR_ARTIFACTS_RESPONSE,
        _ => {
            return (
                "422 Unprocessable Entity",
                INVALID_RESPONSE.trim_end().to_string(),
            );
        }
    };
    ("200 OK", payload.trim_end().to_string())
}

fn narrative(path: &str, state: &Arc<Mutex<State>>) -> (&'static str, String) {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/narrative");
    let state = state.lock().unwrap();
    if state
        .run
        .as_ref()
        .is_none_or(|run| run.mission_run_id != mission_run_id)
    {
        return (
            "404 Not Found",
            RUN_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    }
    (
        "200 OK",
        NARRATIVE_AVAILABLE_RESPONSE.trim_end().to_string(),
    )
}

fn evidence_page(
    path: &str,
    query: Option<&str>,
    suffix: &str,
    page: &str,
    empty: &str,
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches(&format!("/{suffix}"));
    let state = state.lock().unwrap();
    if state
        .run
        .as_ref()
        .is_none_or(|run| run.mission_run_id != mission_run_id)
    {
        return (
            "404 Not Found",
            RUN_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    }
    let cursor = query.and_then(|query| {
        query.split('&').find_map(|part| {
            let (name, value) = part.split_once('=')?;
            (name == "cursor").then_some(value)
        })
    });
    if state.endless_evidence {
        return ("200 OK", page.trim_end().to_string());
    }
    let expected_cursor = match suffix {
        "observations" => "eyJ2IjoxLCJydW4iOiJydW4tZml4dHVyZS0wMDEiLCJzZXEiOjN9",
        "activities" => "eyJ2IjoxLCJydW4iOiJydW4tZml4dHVyZS0wMDEiLCJzZXEiOjJ9",
        "artifacts" => "eyJ2IjoxLCJydW4iOiJydW4tZml4dHVyZS0wMDEiLCJzZXEiOjN9",
        _ => unreachable!("known evidence route"),
    };
    match cursor {
        None => ("200 OK", page.trim_end().to_string()),
        Some(cursor) if cursor == expected_cursor => ("200 OK", empty.trim_end().to_string()),
        Some(_) => (
            "422 Unprocessable Entity",
            INVALID_CURSOR_RESPONSE.trim_end().to_string(),
        ),
    }
}

fn query_value<'a>(query: Option<&'a str>, name: &str) -> Option<&'a str> {
    query.and_then(|query| {
        query.split('&').find_map(|part| {
            let (candidate, value) = part.split_once('=')?;
            (candidate == name).then_some(value)
        })
    })
}

fn artifact_route_ids<'a>(path: &'a str, suffix: &str) -> Option<(&'a str, &'a str)> {
    let rest = path.strip_prefix("/api/v1/mission-runs/")?;
    let (mission_run_id, rest) = rest.split_once("/artifacts/")?;
    let artifact_id = rest.strip_suffix(suffix)?;
    Some((mission_run_id, artifact_id))
}

fn artifact_content(
    path: &str,
    query: Option<&str>,
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let Some((mission_run_id, artifact_id)) = artifact_route_ids(path, "/content") else {
        return (
            "404 Not Found",
            ARTIFACT_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    };
    let state = state.lock().unwrap();
    if state
        .run
        .as_ref()
        .is_none_or(|run| run.mission_run_id != mission_run_id)
    {
        return (
            "404 Not Found",
            RUN_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    }
    let offset = query_value(query, "offset")
        .map(str::parse::<u64>)
        .transpose();
    let limit = query_value(query, "limit")
        .map(str::parse::<u64>)
        .transpose();
    if offset.is_err()
        || limit.is_err()
        || limit.is_ok_and(|limit| limit.is_some_and(|limit| !(1..=16384).contains(&limit)))
    {
        return (
            "422 Unprocessable Entity",
            INVALID_RESPONSE.trim_end().to_string(),
        );
    }
    let offset = offset.ok().flatten().unwrap_or(0);
    match (artifact_id, offset) {
        ("planner-log", 0) => (
            "200 OK",
            ARTIFACT_CONTENT_TEXT_PAGE_RESPONSE.trim_end().to_string(),
        ),
        ("planner-log", 4096) => (
            "200 OK",
            ARTIFACT_CONTENT_TEXT_FINAL_RESPONSE.trim_end().to_string(),
        ),
        ("planner-log", _) => (
            "422 Unprocessable Entity",
            INVALID_RESPONSE.trim_end().to_string(),
        ),
        ("detection-frame", 0) => (
            "200 OK",
            ARTIFACT_CONTENT_BINARY_RESPONSE.trim_end().to_string(),
        ),
        ("detection-frame", _) => (
            "422 Unprocessable Entity",
            INVALID_RESPONSE.trim_end().to_string(),
        ),
        _ => (
            "404 Not Found",
            ARTIFACT_NOT_FOUND_RESPONSE.trim_end().to_string(),
        ),
    }
}

fn artifact_entries(
    path: &str,
    query: Option<&str>,
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let Some((mission_run_id, artifact_id)) = artifact_route_ids(path, "/entries") else {
        return (
            "404 Not Found",
            ARTIFACT_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    };
    let state = state.lock().unwrap();
    if state
        .run
        .as_ref()
        .is_none_or(|run| run.mission_run_id != mission_run_id)
    {
        return (
            "404 Not Found",
            RUN_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    }
    if artifact_id != "operator-conversation" {
        return (
            "404 Not Found",
            ARTIFACT_NOT_FOUND_RESPONSE.trim_end().to_string(),
        );
    }
    if state.endless_evidence {
        return (
            "200 OK",
            ARTIFACT_ENTRIES_PAGE_RESPONSE.trim_end().to_string(),
        );
    }
    match query_value(query, "cursor") {
        None => (
            "200 OK",
            ARTIFACT_ENTRIES_PAGE_RESPONSE.trim_end().to_string(),
        ),
        Some("eyJ2IjoxLCJydW4iOiJydW4tZml4dHVyZS0wMDEiLCJzZXEiOjR9") => (
            "200 OK",
            ARTIFACT_ENTRIES_EMPTY_RESPONSE.trim_end().to_string(),
        ),
        Some(_) => (
            "422 Unprocessable Entity",
            INVALID_CURSOR_RESPONSE.trim_end().to_string(),
        ),
    }
}

fn owner_intent(
    path: &str,
    authorization: Option<String>,
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/mission-intent");
    let state = state.lock().unwrap();
    let owner = state.activations.values().find(|activation| {
        state
            .run
            .as_ref()
            .is_some_and(|run| run.mission_run_id == mission_run_id)
            && authorization.as_deref() == Some(activation.credential.as_str())
    });
    let Some(owner) = owner else {
        return (
            "403 Forbidden",
            AUTHORIZATION_FAILED_RESPONSE.trim_end().to_string(),
        );
    };
    (
        "200 OK",
        from_example(
            INTENT_RESPONSE,
            &[
                ("mission_run_id", json!(mission_run_id)),
                ("mission_intent", json!(owner.mission_intent)),
                ("source_authority", json!(owner.source_authority)),
            ],
        ),
    )
}

fn cancel(
    path: &str,
    authorization: Option<String>,
    body: &[u8],
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/cancellations");
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return (
            "422 Unprocessable Entity",
            INVALID_RESPONSE.trim_end().to_string(),
        );
    };
    let Some(request_id) = value.get("cancellation_request_id").and_then(Value::as_str) else {
        return (
            "422 Unprocessable Entity",
            INVALID_RESPONSE.trim_end().to_string(),
        );
    };
    let mut state = state.lock().unwrap();
    let authorized = state.activations.values().any(|activation| {
        state
            .run
            .as_ref()
            .is_some_and(|run| run.mission_run_id == mission_run_id)
            && authorization.as_deref() == Some(activation.credential.as_str())
    });
    if !authorized {
        return (
            "403 Forbidden",
            AUTHORIZATION_FAILED_RESPONSE.trim_end().to_string(),
        );
    }
    if let Some((stored_id, stored_run_id, response)) = state.cancellation.as_ref() {
        if stored_id == request_id && stored_run_id == mission_run_id {
            return ("202 Accepted", response.clone());
        }
        if stored_id == request_id {
            return (
                "409 Conflict",
                CANCELLATION_CONFLICT_RESPONSE.trim_end().to_string(),
            );
        }
    }
    let status = state
        .run
        .as_ref()
        .map_or("cancelled", |run| run.status.as_str());
    let response = from_example(
        CANCELLATION_ACCEPTED_RESPONSE,
        &[
            ("mission_run_id", json!(mission_run_id)),
            ("cancellation_request_id", json!(request_id)),
            ("status", json!(status)),
        ],
    );
    state.cancellation = Some((
        request_id.to_string(),
        mission_run_id.to_string(),
        response.clone(),
    ));
    ("202 Accepted", response)
}

fn activate(
    authorization: Option<String>,
    body: &[u8],
    state: &Arc<Mutex<State>>,
) -> (&'static str, String) {
    let parsed: Result<Value, _> = serde_json::from_slice(body);
    let Ok(value) = parsed else {
        return (
            "422 Unprocessable Entity",
            INVALID_RESPONSE.trim_end().to_string(),
        );
    };
    let fields = [
        "activation_request_id",
        "console_session_id",
        "mission_intent",
        "source_authority",
    ];
    if fields
        .iter()
        .any(|f| value.get(*f).and_then(Value::as_str).is_none())
    {
        return (
            "422 Unprocessable Entity",
            INVALID_RESPONSE.trim_end().to_string(),
        );
    }
    let credential = authorization.unwrap_or_default();
    let request_id = value["activation_request_id"].as_str().unwrap().to_string();

    let mut state = state.lock().unwrap();
    state.last_authorization = Some(credential.clone());

    if let Some(stored) = state.activations.get(&request_id) {
        let same = stored.console_session_id == value["console_session_id"].as_str().unwrap()
            && stored.mission_intent == value["mission_intent"].as_str().unwrap()
            && stored.source_authority == value["source_authority"].as_str().unwrap()
            && stored.credential == credential;
        if same {
            return ("202 Accepted", stored.response_body.clone());
        }
        return ("409 Conflict", CONFLICT_RESPONSE.trim_end().to_string());
    }

    if state
        .run
        .as_ref()
        .is_some_and(|run| !TERMINAL_STATUSES.contains(&run.status.as_str()))
    {
        return ("409 Conflict", RUN_ACTIVE_RESPONSE.trim_end().to_string());
    }

    state.counter += 1;
    let n = state.counter;
    let run = FixtureRun {
        mission_id: format!("mission-fixture-{n:03}"),
        mission_run_id: format!("run-fixture-{n:03}"),
        status: "queued".to_string(),
        created_at: Some("2026-08-24T12:00:00Z".to_string()),
        started_at: None,
        finished_at: None,
        terminal_classification: None,
    };
    let response_body = from_example(
        ACCEPTED_RESPONSE,
        &[
            ("activation_request_id", json!(request_id)),
            ("mission_id", json!(run.mission_id)),
            ("mission_run_id", json!(run.mission_run_id)),
            ("status", json!(run.status)),
            ("created_at", json!(run.created_at)),
        ],
    );
    state.activations.insert(
        request_id,
        StoredActivation {
            console_session_id: value["console_session_id"].as_str().unwrap().to_string(),
            mission_intent: value["mission_intent"].as_str().unwrap().to_string(),
            source_authority: value["source_authority"].as_str().unwrap().to_string(),
            credential,
            response_body: response_body.clone(),
        },
    );
    state.run = Some(run);
    ("202 Accepted", response_body)
}
