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
    match (method, path) {
        ("GET", "/api/v1/health") => (
            "200 OK",
            r#"{"status":"ok","api_version":{"major":1,"minor":0}}"#.to_string(),
        ),
        ("POST", "/api/v1/mission-activations") => activate(authorization, body, state),
        ("GET", "/api/v1/mission-runs/current") => {
            let state = state.lock().unwrap();
            let run = state.run.as_ref().map(run_json);
            ("200 OK", json!({ "mission_run": run }).to_string())
        }
        _ => (
            "404 Not Found",
            json!({"error": {"code": "not_found", "message": "unknown route"}}).to_string(),
        ),
    }
}

fn run_json(run: &FixtureRun) -> Value {
    json!({
        "mission_id": run.mission_id,
        "mission_run_id": run.mission_run_id,
        "status": run.status,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "terminal_classification": run.terminal_classification,
    })
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
            json!({"error": {"code": "invalid_request", "message": "request body must be strict JSON"}})
                .to_string(),
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
            json!({"error": {"code": "invalid_request", "message": "missing or non-string activation fields"}})
                .to_string(),
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
        return (
            "409 Conflict",
            json!({"error": {"code": "activation_request_conflict", "message": "activation_request_id was reused with a different body or credential"}})
                .to_string(),
        );
    }

    if state
        .run
        .as_ref()
        .is_some_and(|run| !TERMINAL_STATUSES.contains(&run.status.as_str()))
    {
        return (
            "409 Conflict",
            json!({"error": {"code": "mission_run_active", "message": "a non-terminal Mission Run already exists"}})
                .to_string(),
        );
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
    let response_body = json!({
        "activation_request_id": request_id,
        "mission_id": run.mission_id,
        "mission_run_id": run.mission_run_id,
        "status": run.status,
        "created_at": run.created_at,
    })
    .to_string();
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
