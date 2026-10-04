//! A deterministic in-process fixture of the Runtime Host v1.5 HTTP contract.
//!
//! Used by contract and worker tests so the console client is proven without
//! a Python process. Static bodies are the committed contract examples;
//! dynamic bodies substitute fixture values into those examples so the
//! fixture cannot drift from the contract.
//!
//! - `GET /api/v1/health` -> v1.5 health.
//! - `GET /api/v1/stack/presets`, `GET /api/v1/stack/preflight`.
//! - `POST /api/v1/mission-activations` -> `202` queued acceptance; same
//!   request id + body (including `stack`) + credential replays the original
//!   acceptance; conflicting reuse -> `409 activation_request_conflict`;
//!   another non-terminal run -> `409 mission_run_active`.
//! - `GET /api/v1/mission-runs/current` -> `{"mission_run":null}` or the record
//!   with `stack` and `terminal_detail`.
//! - `GET /api/v1/mission-runs?limit&before` -> the v1.5 history example; an
//!   empty last page `before=run-1`; `invalid_cursor` for any other `before`.
//! - `GET .../operator-view?section=` for every section, with `ETag` and
//!   `If-None-Match` -> `304`.
//! - Artifact content (planner text/binary pages and a growing service log),
//!   conversation entries, owner intent, cancellation, world frames.

#![allow(dead_code)]

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::Arc;

use parking_lot::Mutex;

use serde_json::{Value, json};

macro_rules! example {
    ($version:literal, $name:literal) => {
        include_str!(concat!(
            "../../../docs/design/operator-console/contract/",
            $version,
            "/",
            $name
        ))
    };
}

const HEALTH_RESPONSE: &str = example!("v1.5", "health.response.json");
const PRESETS_RESPONSE: &str = example!("v1.5", "stack-presets.response.json");
const PREFLIGHT_RESPONSE: &str = example!("v1.5", "stack-preflight.response.json");
const ACCEPTED_RESPONSE: &str = example!("v1", "mission-activation.accepted.response.json");
const CONFLICT_RESPONSE: &str = example!("v1", "mission-activation.conflict.response.json");
const RUN_ACTIVE_RESPONSE: &str = example!("v1", "mission-activation.run-active.response.json");
const INVALID_RESPONSE: &str = example!("v1", "mission-activation.invalid.response.json");
const CURRENT_NONE_RESPONSE: &str = example!("v1", "mission-runs.current.none.response.json");
const CURRENT_ACTIVE_RESPONSE: &str = example!("v1.2", "mission-runs.current.active.response.json");
const INTENT_RESPONSE: &str = example!("v1", "mission-intent.response.json");
const CANCELLATION_ACCEPTED_RESPONSE: &str =
    example!("v1", "mission-run-cancellation.accepted.response.json");
const CANCELLATION_CONFLICT_RESPONSE: &str =
    example!("v1", "mission-run-cancellation.conflict.response.json");
const AUTHORIZATION_FAILED_RESPONSE: &str =
    example!("v1", "mission-run-owner.authorization-failed.response.json");
const INVALID_CURSOR_RESPONSE: &str = example!(
    "v1",
    "mission-run-observations.invalid-cursor.response.json"
);
const RUN_NOT_FOUND_RESPONSE: &str = example!("v1", "mission-run.not-found.response.json");
const HISTORY_RESPONSE: &str = example!("v1.5", "mission-runs.response.json");
const ARTIFACT_CONTENT_TEXT_PAGE_RESPONSE: &str =
    example!("v1", "mission-run-artifact-content.text-page.response.json");
const ARTIFACT_CONTENT_TEXT_FINAL_RESPONSE: &str = example!(
    "v1",
    "mission-run-artifact-content.text-final.response.json"
);
const ARTIFACT_CONTENT_BINARY_RESPONSE: &str =
    example!("v1", "mission-run-artifact-content.binary.response.json");
const ARTIFACT_ENTRIES_PAGE_RESPONSE: &str =
    example!("v1", "mission-run-artifact-entries.page.response.json");
const ARTIFACT_ENTRIES_EMPTY_RESPONSE: &str =
    example!("v1", "mission-run-artifact-entries.empty.response.json");
const ARTIFACT_NOT_FOUND_RESPONSE: &str =
    example!("v1", "mission-run-artifact.not-found.response.json");
const FRAME_NOT_FOUND_RESPONSE: &str =
    example!("v1.2", "mission-run-world-frame.not-found.response.json");

pub const SERVICE_LOG_ARTIFACT: &str = "service-log-physical-runtime";

fn section_example(section: &str) -> Option<&'static str> {
    Some(match section {
        "overview" => example!("v1.2", "mission-run-operator-overview.response.json"),
        "progress" => example!("v1.2", "mission-run-operator-progress.response.json"),
        "agents" => example!("v1.1", "mission-run-operator-agents.response.json"),
        "beliefs" => example!("v1.2", "mission-run-operator-beliefs.response.json"),
        "context" => example!("v1.2", "mission-run-operator-context.response.json"),
        "world" => example!("v1.2", "mission-run-operator-world.response.json"),
        "environment" => example!("v1.1", "mission-run-operator-environment.response.json"),
        "stack" => example!("v1.2", "mission-run-operator-stack.response.json"),
        "artifacts" => example!("v1.1", "mission-run-operator-artifacts.response.json"),
        _ => return None,
    })
}

/// Substitute top-level (or `mission_run.*`) keys into a committed example;
/// panics if a substituted key is missing from the example.
fn from_example(example: &str, substitutions: &[(&str, Value)]) -> String {
    let mut value: Value =
        serde_json::from_str(example).expect("committed contract example parses");
    let target = if let Some(run) = value.get_mut("mission_run") {
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
    stack: Value,
    terminal_detail: Value,
}

#[derive(Debug, Clone)]
struct StoredActivation {
    console_session_id: String,
    mission_intent: String,
    source_authority: String,
    stack: Value,
    credential: String,
    response_body: String,
}

#[derive(Debug, Default)]
struct State {
    activations: HashMap<String, StoredActivation>,
    run: Option<FixtureRun>,
    counter: u32,
    last_authorization: Option<String>,
    last_activation_body: Option<Value>,
    cancellation: Option<(String, String, String)>,
    endless_evidence: bool,
    preflight_fails: bool,
    service_log: String,
    world_frame: Option<Vec<u8>>,
    requests: Vec<String>,
}

/// One HTTP reply.
struct Reply {
    status: &'static str,
    content_type: &'static str,
    headers: Vec<(&'static str, String)>,
    body: Vec<u8>,
}

impl Reply {
    fn json(status: &'static str, body: impl Into<String>) -> Self {
        Reply {
            status,
            content_type: "application/json",
            headers: Vec::new(),
            body: body.into().into_bytes(),
        }
    }

    fn example(status: &'static str, example: &str) -> Self {
        Self::json(status, example.trim_end())
    }
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
                    Ok(stream) => {
                        let state = Arc::clone(&worker_state);
                        std::thread::spawn(move || {
                            let _ = handle_request(stream, &state);
                        });
                    }
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

    /// The last `Authorization` header the fixture observed on activation.
    pub fn last_authorization(&self) -> Option<String> {
        self.state.lock().last_authorization.clone()
    }

    /// The last activation request body.
    pub fn last_activation_body(&self) -> Option<Value> {
        self.state.lock().last_activation_body.clone()
    }

    /// Paths (with query) of every request served so far.
    pub fn requests(&self) -> Vec<String> {
        self.state.lock().requests.clone()
    }

    /// Move the current run to `awaiting_human_decision`.
    pub fn await_human_decision(&self) {
        self.update_run(|run| {
            run.status = "awaiting_human_decision".to_string();
            run.started_at = Some("2026-08-24T12:00:03Z".to_string());
            run.finished_at = None;
            run.terminal_classification = None;
        });
    }

    /// Move the current run to `running`.
    pub fn promote_to_running(&self) {
        self.update_run(|run| {
            run.status = "running".to_string();
            run.started_at = Some("2026-08-24T12:00:03Z".to_string());
        });
    }

    /// Move the current run to a terminal status with a classification.
    pub fn finish_run(&self, status: &str, terminal_classification: Option<&str>) {
        self.update_run(|run| {
            run.status = status.to_string();
            run.finished_at = Some("2026-08-24T12:05:00Z".to_string());
            run.terminal_classification = terminal_classification.map(str::to_string);
        });
    }

    /// End the current run as a Hyper Agent intent rejection.
    pub fn reject_run(&self, reason: &str) {
        self.finish_run("failed", Some("mission_rejected"));
        self.update_run(|run| {
            run.terminal_detail =
                json!({"kind": "mission_rejected", "stage": "intent", "reason": reason});
        });
    }

    /// Make paged evidence requests return another non-terminal page.
    pub fn enable_endless_evidence(&self) {
        self.state.lock().endless_evidence = true;
    }

    /// Make preflight report a failing check.
    pub fn set_preflight_fails(&self, fails: bool) {
        self.state.lock().preflight_fails = fails;
    }

    /// Append text to the physical-runtime service log.
    pub fn append_service_log(&self, text: &str) {
        self.state.lock().service_log.push_str(text);
    }

    /// Serve `bytes` as the current world frame.
    pub fn set_world_frame(&self, bytes: &[u8]) {
        self.state.lock().world_frame = Some(bytes.to_vec());
    }

    fn update_run(&self, update: impl FnOnce(&mut FixtureRun)) {
        if let Some(run) = self.state.lock().run.as_mut() {
            update(run);
        }
    }
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
    let mut if_none_match = None;
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
            } else if name.eq_ignore_ascii_case("if-none-match") {
                if_none_match = Some(value.to_string());
            }
        }
    }
    let mut body = vec![0u8; content_length];
    reader.read_exact(&mut body)?;

    state.lock().requests.push(path.clone());
    let reply = route(
        &method,
        &path,
        authorization,
        if_none_match.as_deref(),
        &body,
        state,
    );
    let mut head = format!(
        "HTTP/1.1 {}\r\ncontent-type: {}\r\ncontent-length: {}\r\nconnection: close\r\n",
        reply.status,
        reply.content_type,
        reply.body.len()
    );
    for (name, value) in &reply.headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    head.push_str("\r\n");
    let mut stream = stream;
    stream.write_all(head.as_bytes())?;
    stream.write_all(&reply.body)?;
    stream.flush()
}

fn route(
    method: &str,
    path: &str,
    authorization: Option<String>,
    if_none_match: Option<&str>,
    body: &[u8],
    state: &Arc<Mutex<State>>,
) -> Reply {
    let (route_path, query) = path
        .split_once('?')
        .map_or((path, None), |(path, query)| (path, Some(query)));
    match (method, route_path) {
        ("GET", "/api/v1/health") => Reply::example("200 OK", HEALTH_RESPONSE),
        ("GET", "/api/v1/stack/presets") => Reply::example("200 OK", PRESETS_RESPONSE),
        ("GET", "/api/v1/stack/preflight") => preflight(query, state),
        ("POST", "/api/v1/mission-activations") => activate(authorization, body, state),
        ("GET", "/api/v1/mission-runs/current") => current(state),
        ("GET", "/api/v1/mission-runs") => match query_value(query, "before") {
            None => Reply::example("200 OK", HISTORY_RESPONSE),
            Some("run-1") => Reply::json(
                "200 OK",
                json!({"mission_runs": [], "next_before": null}).to_string(),
            ),
            Some(_) => Reply::example("422 Unprocessable Entity", INVALID_CURSOR_RESPONSE),
        },
        ("GET", path) if path.ends_with("/mission-intent") => {
            owner_intent(path, authorization, state)
        }
        ("GET", path) if path.ends_with("/operator-view") => {
            operator_view(path, query, if_none_match, state)
        }
        ("GET", path) if path.ends_with("/world-frame") => world_frame(path, if_none_match, state),
        ("GET", path) if path.ends_with("/content") && path.contains("/artifacts/") => {
            artifact_content(path, query, state)
        }
        ("GET", path) if path.ends_with("/entries") && path.contains("/artifacts/") => {
            artifact_entries(path, query, state)
        }
        ("POST", path) if path.ends_with("/cancellations") => {
            cancel(path, authorization, body, state)
        }
        _ => Reply::json(
            "404 Not Found",
            json!({"error": {"code": "not_found", "message": "unknown route"}}).to_string(),
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

fn run_matches(state: &State, mission_run_id: &str) -> bool {
    state
        .run
        .as_ref()
        .is_some_and(|run| run.mission_run_id == mission_run_id)
}

fn run_not_found() -> Reply {
    Reply::example("404 Not Found", RUN_NOT_FOUND_RESPONSE)
}

fn invalid() -> Reply {
    Reply::example("422 Unprocessable Entity", INVALID_RESPONSE)
}

fn current(state: &Arc<Mutex<State>>) -> Reply {
    let state = state.lock();
    let Some(run) = state.run.as_ref() else {
        return Reply::example("200 OK", CURRENT_NONE_RESPONSE);
    };
    Reply::json(
        "200 OK",
        from_example(
            CURRENT_ACTIVE_RESPONSE,
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
                ("stack", run.stack.clone()),
                ("terminal_detail", run.terminal_detail.clone()),
            ],
        ),
    )
}

fn preflight(query: Option<&str>, state: &Arc<Mutex<State>>) -> Reply {
    let (Some(preset_id), Some(airsim), Some(perception), Some(update_ownership)) = (
        query_value(query, "preset_id"),
        query_value(query, "airsim"),
        query_value(query, "perception"),
        query_value(query, "update_ownership"),
    ) else {
        return invalid();
    };
    let fails = state.lock().preflight_fails;
    let mut value: Value = serde_json::from_str(PREFLIGHT_RESPONSE).unwrap();
    value["preset_id"] = json!(preset_id);
    value["toggles"] = json!({
        "airsim": airsim == "true",
        "perception": perception,
        "update_ownership": update_ownership,
    });
    value["launchable"] = json!(!fails);
    if !fails {
        value["checks"]
            .as_array_mut()
            .unwrap()
            .retain(|check| check["status"] != "fail");
    }
    Reply::json("200 OK", value.to_string())
}

fn operator_view(
    path: &str,
    query: Option<&str>,
    if_none_match: Option<&str>,
    state: &Arc<Mutex<State>>,
) -> Reply {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/operator-view");
    let state = state.lock();
    if !run_matches(&state, mission_run_id) {
        return run_not_found();
    }
    let Some(section) = query_value(query, "section") else {
        return invalid();
    };
    let Some(example) = section_example(section) else {
        return invalid();
    };
    let run = state.run.as_ref().unwrap();
    let etag = format!("\"{section}-{}\"", run.status);
    if if_none_match == Some(etag.as_str()) {
        let mut reply = Reply::json("304 Not Modified", "");
        reply.headers.push(("etag", etag));
        return reply;
    }
    let body = from_example(
        example,
        &[
            ("mission_id", json!(run.mission_id)),
            ("mission_run_id", json!(run.mission_run_id)),
            ("run_status", json!(run.status)),
        ],
    );
    let mut reply = Reply::json("200 OK", body);
    reply.headers.push(("etag", etag));
    reply
}

fn world_frame(path: &str, if_none_match: Option<&str>, state: &Arc<Mutex<State>>) -> Reply {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/world-frame");
    let state = state.lock();
    if !run_matches(&state, mission_run_id) {
        return run_not_found();
    }
    let Some(bytes) = state.world_frame.clone() else {
        return Reply::example("404 Not Found", FRAME_NOT_FOUND_RESPONSE);
    };
    let etag = format!("\"frame-{}\"", bytes.len());
    if if_none_match == Some(etag.as_str()) {
        let mut reply = Reply::json("304 Not Modified", "");
        reply.headers.push(("etag", etag));
        return reply;
    }
    Reply {
        status: "200 OK",
        content_type: "image/png",
        headers: vec![
            ("etag", etag),
            ("x-frame-sequence", "812".to_string()),
            ("x-mission-time", "143.5".to_string()),
        ],
        body: bytes,
    }
}

fn artifact_route_ids<'a>(path: &'a str, suffix: &str) -> Option<(&'a str, &'a str)> {
    let rest = path.strip_prefix("/api/v1/mission-runs/")?;
    let (mission_run_id, rest) = rest.split_once("/artifacts/")?;
    let artifact_id = rest.strip_suffix(suffix)?;
    Some((mission_run_id, artifact_id))
}

fn artifact_content(path: &str, query: Option<&str>, state: &Arc<Mutex<State>>) -> Reply {
    let Some((mission_run_id, artifact_id)) = artifact_route_ids(path, "/content") else {
        return Reply::example("404 Not Found", ARTIFACT_NOT_FOUND_RESPONSE);
    };
    let state = state.lock();
    if !run_matches(&state, mission_run_id) {
        return run_not_found();
    }
    let offset = query_value(query, "offset")
        .map(str::parse::<u64>)
        .transpose();
    let limit = query_value(query, "limit")
        .map(str::parse::<u64>)
        .transpose();
    let (Ok(offset), Ok(limit)) = (offset, limit) else {
        return invalid();
    };
    let offset = offset.unwrap_or(0);
    let limit = limit.unwrap_or(4096);
    if !(1..=16384).contains(&limit) {
        return invalid();
    }
    match (artifact_id, offset) {
        ("planner-log", 0) => Reply::example("200 OK", ARTIFACT_CONTENT_TEXT_PAGE_RESPONSE),
        ("planner-log", 4096) => Reply::example("200 OK", ARTIFACT_CONTENT_TEXT_FINAL_RESPONSE),
        ("detection-frame", 0) => Reply::example("200 OK", ARTIFACT_CONTENT_BINARY_RESPONSE),
        ("planner-log" | "detection-frame", _) => invalid(),
        (SERVICE_LOG_ARTIFACT, offset) => {
            let log = state.service_log.as_bytes();
            let size = log.len() as u64;
            if offset > size {
                return invalid();
            }
            let end = (offset + limit).min(size);
            let eof = end == size;
            let run = state.run.as_ref().unwrap();
            Reply::json(
                "200 OK",
                json!({
                    "schema_version": 1,
                    "mission_id": run.mission_id,
                    "mission_run_id": run.mission_run_id,
                    "artifact_id": SERVICE_LOG_ARTIFACT,
                    "classification": "service_log",
                    "media_type": "text/plain",
                    "byte_size": size,
                    "offset": offset,
                    "next_offset": if eof { Value::Null } else { json!(end) },
                    "eof": eof,
                    "truncated": false,
                    "content": String::from_utf8_lossy(&log[offset as usize..end as usize]),
                })
                .to_string(),
            )
        }
        _ => Reply::example("404 Not Found", ARTIFACT_NOT_FOUND_RESPONSE),
    }
}

fn artifact_entries(path: &str, query: Option<&str>, state: &Arc<Mutex<State>>) -> Reply {
    let Some((mission_run_id, artifact_id)) = artifact_route_ids(path, "/entries") else {
        return Reply::example("404 Not Found", ARTIFACT_NOT_FOUND_RESPONSE);
    };
    let state = state.lock();
    if !run_matches(&state, mission_run_id) {
        return run_not_found();
    }
    if artifact_id != "operator-conversation" {
        return Reply::example("404 Not Found", ARTIFACT_NOT_FOUND_RESPONSE);
    }
    if state.endless_evidence {
        return Reply::example("200 OK", ARTIFACT_ENTRIES_PAGE_RESPONSE);
    }
    match query_value(query, "cursor") {
        None => Reply::example("200 OK", ARTIFACT_ENTRIES_PAGE_RESPONSE),
        Some("eyJ2IjoxLCJydW4iOiJydW4tZml4dHVyZS0wMDEiLCJzZXEiOjR9") => {
            Reply::example("200 OK", ARTIFACT_ENTRIES_EMPTY_RESPONSE)
        }
        Some(_) => Reply::example("422 Unprocessable Entity", INVALID_CURSOR_RESPONSE),
    }
}

fn owner_intent(path: &str, authorization: Option<String>, state: &Arc<Mutex<State>>) -> Reply {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/mission-intent");
    let state = state.lock();
    let owner = state.activations.values().find(|activation| {
        run_matches(&state, mission_run_id)
            && authorization.as_deref() == Some(activation.credential.as_str())
    });
    let Some(owner) = owner else {
        return Reply::example("403 Forbidden", AUTHORIZATION_FAILED_RESPONSE);
    };
    Reply::json(
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
) -> Reply {
    let mission_run_id = path
        .trim_start_matches("/api/v1/mission-runs/")
        .trim_end_matches("/cancellations");
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return invalid();
    };
    let Some(request_id) = value.get("cancellation_request_id").and_then(Value::as_str) else {
        return invalid();
    };
    let mut state = state.lock();
    let authorized = state.activations.values().any(|activation| {
        run_matches(&state, mission_run_id)
            && authorization.as_deref() == Some(activation.credential.as_str())
    });
    if !authorized {
        return Reply::example("403 Forbidden", AUTHORIZATION_FAILED_RESPONSE);
    }
    if let Some((stored_id, stored_run_id, response)) = state.cancellation.as_ref() {
        if stored_id == request_id && stored_run_id == mission_run_id {
            return Reply::json("202 Accepted", response.clone());
        }
        if stored_id == request_id {
            return Reply::example("409 Conflict", CANCELLATION_CONFLICT_RESPONSE);
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
    Reply::json("202 Accepted", response)
}

fn activate(authorization: Option<String>, body: &[u8], state: &Arc<Mutex<State>>) -> Reply {
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return invalid();
    };
    let fields = [
        "activation_request_id",
        "console_session_id",
        "mission_intent",
        "source_authority",
    ];
    if fields
        .iter()
        .any(|field| value.get(*field).and_then(Value::as_str).is_none())
    {
        return invalid();
    }
    let stack = value.get("stack").cloned().unwrap_or(Value::Null);
    if !stack.is_null() && !stack.is_object() {
        return invalid();
    }
    let credential = authorization.unwrap_or_default();
    let request_id = value["activation_request_id"].as_str().unwrap().to_string();

    let mut state = state.lock();
    state.last_authorization = Some(credential.clone());
    state.last_activation_body = Some(value.clone());

    if let Some(stored) = state.activations.get(&request_id) {
        let same = stored.console_session_id == value["console_session_id"].as_str().unwrap()
            && stored.mission_intent == value["mission_intent"].as_str().unwrap()
            && stored.source_authority == value["source_authority"].as_str().unwrap()
            && stored.stack == stack
            && stored.credential == credential;
        if same {
            return Reply::json("202 Accepted", stored.response_body.clone());
        }
        return Reply::example("409 Conflict", CONFLICT_RESPONSE);
    }

    if state
        .run
        .as_ref()
        .is_some_and(|run| !TERMINAL_STATUSES.contains(&run.status.as_str()))
    {
        return Reply::example("409 Conflict", RUN_ACTIVE_RESPONSE);
    }

    state.counter += 1;
    let n = state.counter;
    let run_stack = if stack.is_null() {
        json!({"preset_id": "mission1-harbor", "airsim": false, "perception": "off"})
    } else {
        json!({
            "preset_id": stack["preset_id"],
            "airsim": stack["airsim"],
            "perception": stack["perception"],
        })
    };
    let run = FixtureRun {
        mission_id: format!("mission-fixture-{n:03}"),
        mission_run_id: format!("run-fixture-{n:03}"),
        status: "queued".to_string(),
        created_at: Some("2026-08-24T12:00:00Z".to_string()),
        started_at: None,
        finished_at: None,
        terminal_classification: None,
        stack: run_stack,
        terminal_detail: Value::Null,
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
            stack,
            credential,
            response_body: response_body.clone(),
        },
    );
    state.run = Some(run);
    Reply::json("202 Accepted", response_body)
}
