//! Shared builders for state-machine and render tests: a manual clock, apps
//! driven to the Launch and Run screens, and Host replies built from the
//! committed contract examples.

#![allow(dead_code)]

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use operator_console::app::{App, Clock, LaunchField, SessionStateFile};
use operator_console::host::{
    ActivationAccepted, ActivationOutcome, ApiVersion, ArtifactContentPage, ContentPurpose,
    Fetched, Health, HostCommand, HostMessage, OperatorSection, OperatorViewPage, PreflightQuery,
    RunRecord, StackPreflight, StackPresets,
};
use parking_lot::Mutex;
use serde_json::{Value, json};

pub const HOST: &str = "http://127.0.0.1:8787";
pub const RUN_ID: &str = "run-fixture-001";
pub const MISSION_ID: &str = "mission-fixture-001";
/// Wall clock at the manual clock's origin: 2026-08-24T12:00:43Z.
pub const UNIX_ORIGIN: i64 = 1_787_572_843;

/// Deterministic monotonic and wall clock.
#[derive(Debug)]
pub struct ManualClock {
    origin: Instant,
    now: Mutex<Instant>,
}

impl ManualClock {
    pub fn new() -> Arc<Self> {
        let origin = Instant::now();
        Arc::new(Self {
            origin,
            now: Mutex::new(origin),
        })
    }

    pub fn advance(&self, duration: Duration) {
        *self.now.lock() += duration;
    }
}

impl Clock for ManualClock {
    fn now(&self) -> Instant {
        *self.now.lock()
    }

    fn unix_now(&self) -> i64 {
        UNIX_ORIGIN + self.now().duration_since(self.origin).as_secs() as i64
    }
}

/// Scratch directory under Cargo's integration-test tmp dir (never `/tmp`).
pub fn scratch_dir(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("operator-console-{name}-{}", uuid::Uuid::new_v4()))
}

pub fn state_file(name: &str) -> SessionStateFile {
    SessionStateFile::at(scratch_dir(name).join("operator-console/session.json"))
}

pub fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

pub fn alt_enter() -> KeyEvent {
    KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)
}

pub fn type_text(app: &mut App, text: &str) {
    for c in text.chars() {
        app.handle_key(key(KeyCode::Char(c)));
    }
}

/// Tab to a Launch screen field.
pub fn focus(app: &mut App, field: LaunchField) {
    for _ in 0..LaunchField::ALL.len() {
        if app.launch.focus == field {
            return;
        }
        app.handle_key(key(KeyCode::Tab));
    }
    panic!("could not focus {field:?}");
}

pub fn health() -> Health {
    Health {
        status: "ok".to_string(),
        api_version: ApiVersion { major: 1, minor: 3 },
    }
}

pub fn contract(version: &str, name: &str) -> Value {
    let path = format!(
        "{}/../docs/design/operator-console/contract/{version}/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap()
}

pub fn presets() -> StackPresets {
    serde_json::from_value(contract("v1.3", "stack-presets.response.json")).unwrap()
}

/// Preflight for `query`: the committed example, minus its failing check
/// when `pass`.
pub fn preflight(query: &PreflightQuery, pass: bool) -> StackPreflight {
    let mut preflight: StackPreflight =
        serde_json::from_value(contract("v1.2", "stack-preflight.response.json")).unwrap();
    preflight.preset_id = query.preset_id.clone();
    preflight.toggles = query.toggles.clone();
    preflight.launchable = pass;
    if pass {
        preflight.checks.retain(|check| check.status != "fail");
    }
    preflight
}

/// The only command must be a preflight; returns its id and query.
pub fn single_preflight(app: &mut App) -> (u64, PreflightQuery) {
    let commands = app.take_commands();
    match &commands[..] {
        [HostCommand::Preflight { request_id, query }] => (*request_id, query.clone()),
        other => panic!("expected one preflight, got {other:?}"),
    }
}

pub fn app_with_clock(name: &str) -> (App, Arc<ManualClock>) {
    app_with_file(state_file(name))
}

pub fn app_with_file(file: SessionStateFile) -> (App, Arc<ManualClock>) {
    let clock = ManualClock::new();
    let mut app = App::new_with_session_file_and_clock(HOST.to_string(), file, clock.clone());
    app.session.session_id = "c0ns01e0-0000-4000-8000-5e5510n5a1d0".to_string();
    app.take_commands();
    (app, clock)
}

pub fn connect_with_presets(app: &mut App) {
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.take_commands();
    app.handle_host_message(HostMessage::Presets(Ok(presets())));
}

/// Launch screen with presets loaded and a passing preflight.
pub fn ready_launch_app(name: &str) -> (App, Arc<ManualClock>) {
    ready_launch_app_with_file(state_file(name))
}

pub fn ready_launch_app_with_file(file: SessionStateFile) -> (App, Arc<ManualClock>) {
    let (mut app, clock) = app_with_file(file);
    connect_with_presets(&mut app);
    app.check_deadlines();
    let (request_id, query) = single_preflight(&mut app);
    app.handle_host_message(HostMessage::Preflight {
        request_id,
        result: Ok(preflight(&query, true)),
    });
    (app, clock)
}

/// Review, confirm, and accept the activation (commands are not drained).
pub fn accept(app: &mut App) {
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        ActivationAccepted {
            activation_request_id: app.review_request_id().unwrap().to_string(),
            mission_id: MISSION_ID.to_string(),
            mission_run_id: RUN_ID.to_string(),
            status: "queued".to_string(),
            created_at: "2026-08-24T12:00:00Z".to_string(),
        },
    ))));
}

/// An owned run on the Run screen with commands drained.
pub fn run_app(name: &str) -> (App, Arc<ManualClock>) {
    run_app_with_file(name, state_file(name))
}

pub fn run_app_with_file(_name: &str, file: SessionStateFile) -> (App, Arc<ManualClock>) {
    let (mut app, clock) = ready_launch_app_with_file(file);
    accept(&mut app);
    for command in app.take_commands() {
        if let HostCommand::FetchOperatorView {
            section,
            request_id,
            ..
        } = command
        {
            app.handle_host_message(section_reply(section, request_id, None, |page| {
                // A newly activated run has not issued invocations or artifacts.
                if section == OperatorSection::Agents {
                    page["agents"] = json!([]);
                } else if section == OperatorSection::Artifacts {
                    page["artifacts"] = json!([]);
                }
            }));
        }
    }
    app.take_commands();
    (app, clock)
}

pub fn running_run() -> RunRecord {
    let value = contract("v1.2", "mission-runs.current.active.response.json");
    let mut run: RunRecord = serde_json::from_value(value["mission_run"].clone()).unwrap();
    run.mission_id = MISSION_ID.to_string();
    run.mission_run_id = RUN_ID.to_string();
    run
}

pub fn rejected_run() -> RunRecord {
    let value = contract("v1.2", "mission-runs.current.rejected.response.json");
    let mut run: RunRecord = serde_json::from_value(value["mission_run"].clone()).unwrap();
    run.mission_id = MISSION_ID.to_string();
    run.mission_run_id = RUN_ID.to_string();
    run
}

/// Operator-view sections requested by `commands`, in order.
pub fn sections(commands: &[HostCommand]) -> Vec<OperatorSection> {
    commands
        .iter()
        .filter_map(|command| match command {
            HostCommand::FetchOperatorView { section, .. } => Some(*section),
            _ => None,
        })
        .collect()
}

fn section_command(commands: &[HostCommand], wanted: OperatorSection) -> &HostCommand {
    commands
        .iter()
        .find(|command| {
            matches!(command, HostCommand::FetchOperatorView { section, .. } if *section == wanted)
        })
        .unwrap_or_else(|| panic!("no {wanted:?} request in {commands:?}"))
}

pub fn section_request_id(commands: &[HostCommand], section: OperatorSection) -> u64 {
    match section_command(commands, section) {
        HostCommand::FetchOperatorView { request_id, .. } => *request_id,
        _ => unreachable!(),
    }
}

pub fn section_cursor_etag(
    commands: &[HostCommand],
    section: OperatorSection,
) -> (Option<String>, Option<String>) {
    match section_command(commands, section) {
        HostCommand::FetchOperatorView { cursor, etag, .. } => (
            match cursor {
                operator_console::host::OperatorCursor::Latest => None,
                operator_console::host::OperatorCursor::After(cursor)
                | operator_console::host::OperatorCursor::Before(cursor) => Some(cursor.clone()),
            },
            etag.clone(),
        ),
        _ => unreachable!(),
    }
}

/// A fresh section page built from its committed example.
pub fn section_page(section: OperatorSection, mutate: impl FnOnce(&mut Value)) -> OperatorViewPage {
    let (version, name) = match section {
        OperatorSection::Overview => ("v1.2", "mission-run-operator-overview.response.json"),
        OperatorSection::Progress => ("v1.2", "mission-run-operator-progress.response.json"),
        OperatorSection::Agents => ("v1.1", "mission-run-operator-agents.response.json"),
        OperatorSection::Beliefs => ("v1.2", "mission-run-operator-beliefs.response.json"),
        OperatorSection::Context => ("v1.2", "mission-run-operator-context.response.json"),
        OperatorSection::World => ("v1.2", "mission-run-operator-world.response.json"),
        OperatorSection::Environment => ("v1.1", "mission-run-operator-environment.response.json"),
        OperatorSection::Stack => ("v1.2", "mission-run-operator-stack.response.json"),
        OperatorSection::Artifacts => ("v1.1", "mission-run-operator-artifacts.response.json"),
    };
    let mut value = contract(version, name);
    value["mission_id"] = json!(MISSION_ID);
    value["mission_run_id"] = json!(RUN_ID);
    mutate(&mut value);
    OperatorViewPage::decode(section, value.to_string().as_bytes()).unwrap()
}

pub fn section_reply(
    section: OperatorSection,
    request_id: u64,
    etag: Option<&str>,
    mutate: impl FnOnce(&mut Value),
) -> HostMessage {
    HostMessage::OperatorView {
        mission_run_id: RUN_ID.to_string(),
        section,
        request_id,
        result: Ok(Fetched::Fresh {
            value: section_page(section, mutate),
            etag: etag.map(str::to_string),
        }),
    }
}

pub fn overview_reply(request_id: u64, etag: Option<&str>) -> HostMessage {
    section_reply(OperatorSection::Overview, request_id, etag, |_| {})
}

/// Poll once and answer the stack section from its example.
pub fn stack_reply(app: &mut App) -> HostMessage {
    app.request_poll();
    let id = section_request_id(&app.take_commands(), OperatorSection::Stack);
    section_reply(OperatorSection::Stack, id, None, |_| {})
}

/// Agents page with one invocation per id, in start order.
pub fn agents_reply(request_id: u64, ids: &[&str]) -> HostMessage {
    section_reply(OperatorSection::Agents, request_id, None, |value| {
        let template = value["agents"][0].clone();
        value["agents"] = ids
            .iter()
            .map(|id| {
                let n: u32 = id.rsplit('-').next().unwrap().parse().unwrap();
                let mut agent = template.clone();
                agent["stable_id"] = json!(id);
                agent["started_at"] = json!(format!("2026-08-24T12:00:{n:02}Z"));
                agent
            })
            .collect();
    })
}

pub fn artifacts_reply(request_id: u64) -> HostMessage {
    section_reply(OperatorSection::Artifacts, request_id, None, |_| {})
}

/// Artifact content from the committed text examples, retargeted.
pub fn content_reply(artifact_id: &str, offset: u64, example: &str) -> HostMessage {
    let mut value = contract(
        "v1",
        &format!("mission-run-artifact-content.{example}.response.json"),
    );
    value["mission_run_id"] = json!(RUN_ID);
    value["artifact_id"] = json!(artifact_id);
    HostMessage::ArtifactContent {
        purpose: ContentPurpose::Inspector,
        mission_run_id: RUN_ID.to_string(),
        artifact_id: artifact_id.to_string(),
        requested_offset: offset,
        result: Ok(serde_json::from_value(value).unwrap()),
    }
}

/// `(artifact_id, offset)` of the service-log fetch in `commands`.
pub fn service_log_request(commands: &[HostCommand]) -> Option<(String, u64)> {
    commands.iter().find_map(|command| match command {
        HostCommand::FetchArtifactContent {
            purpose: ContentPurpose::ServiceLog,
            artifact_id,
            offset,
            ..
        } => Some((artifact_id.clone(), *offset)),
        _ => None,
    })
}

pub fn service_log_reply(offset: u64, content: &str, byte_size: u64) -> HostMessage {
    let end = offset + content.len() as u64;
    let eof = end >= byte_size;
    HostMessage::ArtifactContent {
        purpose: ContentPurpose::ServiceLog,
        mission_run_id: RUN_ID.to_string(),
        artifact_id: "service-log-physical-runtime".to_string(),
        requested_offset: offset,
        result: Ok(ArtifactContentPage {
            schema_version: 1,
            mission_id: MISSION_ID.to_string(),
            mission_run_id: RUN_ID.to_string(),
            artifact_id: "service-log-physical-runtime".to_string(),
            classification: "service_log".to_string(),
            media_type: "text/plain".to_string(),
            byte_size: Some(byte_size),
            offset,
            next_offset: (!eof).then_some(end),
            eof,
            truncated: false,
            content: Some(content.to_string()),
        }),
    }
}
