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
    Fetched, Health, HostCommand, HostMessage, MissionRunsPage, OperatorSection, OperatorViewPage,
    PreflightQuery, RunRecord, StackPreflight, StackPresets,
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

/// The v1.5 Host's presets: all eight catalog presets with descriptions and
/// toggle-choice descriptions.
pub fn presets() -> StackPresets {
    serde_json::from_value(contract("v1.5", "stack-presets.response.json")).unwrap()
}

/// An older (v1.3) Host's presets: no descriptions.
pub fn presets_v1_3() -> StackPresets {
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
    run_app_with(file, &|_| {})
}

fn run_app_with(file: SessionStateFile, progress: &dyn Fn(&mut Value)) -> (App, Arc<ManualClock>) {
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
                } else if section == OperatorSection::Progress {
                    progress(page);
                }
            }));
        }
    }
    app.take_commands();
    (app, clock)
}

/// An owned running run with one poll of every section answered from its
/// example; `progress` edits each progress page.
pub fn hydrated_run_app(name: &str, progress: impl Fn(&mut Value)) -> (App, Arc<ManualClock>) {
    let (mut app, clock) = run_app_with(state_file(name), &progress);
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(running_run()),
        },
    )));
    app.request_poll();
    let commands = app.take_commands();
    for section in sections(&commands) {
        let id = section_request_id(&commands, section);
        app.handle_host_message(section_reply(section, id, None, |page| {
            if section == OperatorSection::Progress {
                progress(page);
            }
        }));
    }
    (app, clock)
}

/// Answer progress with the v1.5 example: the narrative covers record #763,
/// the newest record is #781, and it was generated 12 s before the manual
/// clock's origin.
pub fn v1_5_progress(page: &mut Value) {
    page["progress"] =
        contract("v1.5", "mission-run-operator-progress.response.json")["progress"].clone();
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

/// A v1.5 `/current` failure example retargeted to the fixture run.
pub fn failed_run(name: &str) -> RunRecord {
    let value = contract("v1.5", name);
    let mut run: RunRecord = serde_json::from_value(value["mission_run"].clone()).unwrap();
    run.mission_id = MISSION_ID.to_string();
    run.mission_run_id = RUN_ID.to_string();
    run
}

/// `stack_failed`: perception never became healthy (v1.5 example).
pub fn stack_failed_run() -> RunRecord {
    failed_run("mission-runs.current.stack-failed.response.json")
}

/// `worker_failed` at the closed loop (v1.5 example).
pub fn worker_failed_run() -> RunRecord {
    failed_run("mission-runs.current.worker-failed.response.json")
}

/// `host_interrupted`: the Host records no terminal detail for it.
pub fn host_interrupted_run() -> RunRecord {
    RunRecord {
        status: "failed".to_string(),
        finished_at: Some("2026-08-24T12:05:00Z".to_string()),
        terminal_classification: Some("host_interrupted".to_string()),
        terminal_detail: None,
        ..running_run()
    }
}

/// `cancelled_by_owner`, finished 0:38 after the fixture run started.
pub fn cancelled_run() -> RunRecord {
    RunRecord {
        status: "cancelled".to_string(),
        finished_at: Some("2026-08-24T12:00:41Z".to_string()),
        terminal_classification: Some("cancelled_by_owner".to_string()),
        terminal_detail: None,
        ..running_run()
    }
}

/// `succeeded`, finished 5:12 after the fixture run started.
pub fn succeeded_run() -> RunRecord {
    RunRecord {
        status: "succeeded".to_string(),
        finished_at: Some("2026-08-24T12:05:15Z".to_string()),
        terminal_classification: None,
        terminal_detail: None,
        ..running_run()
    }
}

/// The v1.5 terminal receipt example (a succeeded lifecycle whose audit
/// artifact recorded FAIL), then `edit`.
pub fn receipt_example(edit: impl FnOnce(&mut Value)) -> Value {
    let mut receipt = contract(
        "v1.5",
        "mission-run-operator-overview.succeeded.response.json",
    )["overview"]["receipt"]
        .clone();
    edit(&mut receipt);
    receipt
}

/// A terminal run ended as `run` whose overview carries `receipt`, the
/// receipt's final FSM state and Mission time, and `phase` edits.
pub fn receipt_run_app(
    name: &str,
    run: RunRecord,
    receipt: Value,
    stack: Option<Value>,
    phase: impl Fn(&mut Value),
) -> (App, Arc<ManualClock>) {
    terminal_run_app(name, run, |section, page| match section {
        OperatorSection::Overview => {
            let overview = &mut page["overview"];
            overview["fsm"]["state"] = receipt["final"]["fsm_state"].clone();
            overview["environment"]["mission_time_seconds"] =
                receipt["final"]["mission_time_seconds"].clone();
            overview["receipt"] = receipt.clone();
            phase(overview);
        }
        OperatorSection::Stack => {
            if let Some(stack) = stack.as_ref() {
                page["stack"] = stack.clone();
            }
        }
        _ => {}
    })
}

/// Run Root the v1.5 overview example reports.
pub const RUN_ROOT: &str = "/srv/onr/var/runtime-host/runs/run-fixture-001";

/// Phase steps: `done` before `failed_at`, `failed` there, `pending` after,
/// and the Done step `failed` (a run that ended without succeeding).
pub fn failed_phase(overview: &mut Value, failed_at: &str, detail: &str) {
    overview["phase"]["current"] = json!("terminal");
    let mut reached = false;
    for step in overview["phase"]["steps"].as_array_mut().unwrap() {
        let id = step["id"].as_str().unwrap();
        let (status, text) = if id == failed_at {
            reached = true;
            ("failed", json!(detail))
        } else if id == "terminal" {
            ("failed", Value::Null)
        } else if reached {
            ("pending", Value::Null)
        } else {
            ("done", Value::Null)
        };
        step["status"] = json!(status);
        step["detail"] = text;
    }
}

/// An owned run that ended as `run`, then one terminal poll of every section
/// answered from the examples: v1.5 overview `run_root`, empty evidence lists,
/// and `edit` applied last to each page.
pub fn terminal_run_app(
    name: &str,
    run: RunRecord,
    edit: impl Fn(OperatorSection, &mut Value),
) -> (App, Arc<ManualClock>) {
    let (mut app, clock) = run_app(name);
    let status = run.status.clone();
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(run),
        },
    )));
    app.request_poll();
    let commands = app.take_commands();
    for section in sections(&commands) {
        let reply = section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |page| {
                page["run_status"] = json!(status);
                page["has_more"] = json!(false);
                match section {
                    OperatorSection::Artifacts => page["artifacts"] = json!([]),
                    OperatorSection::Agents => page["agents"] = json!([]),
                    OperatorSection::Overview => {
                        page["overview"]["run_root"] = json!(RUN_ROOT);
                        page["overview"]["narrative"]["terminal"] = json!(true);
                        page["overview"]["narrative"]["status"] = json!("unavailable");
                    }
                    _ => {}
                }
                edit(section, page);
            },
        );
        app.handle_host_message(reply);
    }
    app.take_commands();
    (app, clock)
}

/// The perception readiness timeout: engine stopped, perception failed while
/// its log's last line was still `loading YOLO weights`.
pub fn stack_failed_app(name: &str) -> (App, Arc<ManualClock>) {
    terminal_run_app(name, stack_failed_run(), |section, page| match section {
        OperatorSection::Stack => {
            let mut stack =
                stack_example("v1.5", "mission-run-operator-stack.starting.response.json");
            stack["services"][0]["state"] = json!("stopped");
            stack["services"][0]["exit_code"] = json!(0);
            stack["services"][1]["state"] = json!("failed");
            stack["services"][1]["importance"] = json!("critical");
            stack["services"][1]
                .as_object_mut()
                .unwrap()
                .remove("waiting_for");
            page["stack"] = stack;
        }
        OperatorSection::Overview => failed_phase(
            &mut page["overview"],
            "stack",
            "perception: perception producer was not healthy within 900 seconds",
        ),
        _ => {}
    })
}

/// The closed-loop worker failure: the stack is torn down, the worker log's
/// last line is the exception.
pub fn worker_failed_app(name: &str) -> (App, Arc<ManualClock>) {
    terminal_run_app(name, worker_failed_run(), |section, page| match section {
        OperatorSection::Stack => {
            let mut stack = stack_example("v1.2", "mission-run-operator-stack.response.json");
            stack["services"][0]["state"] = json!("stopped");
            stack["services"][1]["state"] = json!("failed");
            stack["services"][1]["importance"] = json!("critical");
            stack["services"][1]["last_line"] =
                json!("RuntimeError: external environment has no planning data");
            page["stack"] = stack;
        }
        OperatorSection::Overview => failed_phase(
            &mut page["overview"],
            "executing",
            "external environment has no planning data",
        ),
        _ => {}
    })
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

/// The `stack` object of a committed stack-section example.
pub fn stack_example(version: &str, name: &str) -> Value {
    contract(version, name)["stack"].clone()
}

/// The phase stepper at `current`: earlier steps done, later ones pending.
pub fn set_phase(overview: &mut Value, current: &str) {
    overview["phase"]["current"] = json!(current);
    let mut reached = false;
    for step in overview["phase"]["steps"].as_array_mut().unwrap() {
        let id = step["id"].as_str().unwrap();
        let status = if id == current {
            reached = true;
            "active"
        } else if reached {
            "pending"
        } else {
            "done"
        };
        step["status"] = json!(status);
        step["detail"] = Value::Null;
    }
}

/// A live Hyper Agent LLM call `stable_id` in the overview's latest agents.
pub fn set_live_llm_call(overview: &mut Value, stable_id: &str) {
    let mut call =
        contract("v1.1", "mission-run-operator-agents.response.json")["agents"][0].clone();
    call["stable_id"] = json!(stable_id);
    call["kind"] = json!("llm");
    call["name"] = json!("ChatOpenAI");
    call["completion_state"] = json!("live");
    call["started_at"] = json!("2026-08-24T12:00:31.250000+00:00");
    call["finished_at"] = Value::Null;
    call["duration_ms"] = Value::Null;
    overview["latest_agents"]["hyper_agent"] = call;
}

/// A running run whose next poll answers the overview at phase `current`
/// (then edited) and the stack section with `stack`.
pub fn waiting_run_app(
    name: &str,
    current: &str,
    stack: Value,
    edit_overview: impl FnOnce(&mut Value),
) -> (App, Arc<ManualClock>) {
    let (mut app, clock) = run_app(name);
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(running_run()),
        },
    )));
    let mut edit_overview = Some(edit_overview);
    app.request_poll();
    let commands = app.take_commands();
    for section in sections(&commands) {
        let stack = stack.clone();
        let edit = if section == OperatorSection::Overview {
            edit_overview.take()
        } else {
            None
        };
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            move |page| match section {
                OperatorSection::Stack => page["stack"] = stack,
                OperatorSection::Overview => {
                    set_phase(&mut page["overview"], current);
                    if let Some(edit) = edit {
                        edit(&mut page["overview"]);
                    }
                }
                _ => {}
            },
        ));
    }
    (app, clock)
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
    service_log_reply_for("service-log-physical-runtime", offset, content, byte_size)
}

pub fn service_log_reply_for(
    artifact_id: &str,
    offset: u64,
    content: &str,
    byte_size: u64,
) -> HostMessage {
    let end = offset + content.len() as u64;
    let eof = end >= byte_size;
    HostMessage::ArtifactContent {
        purpose: ContentPurpose::ServiceLog,
        mission_run_id: RUN_ID.to_string(),
        artifact_id: artifact_id.to_string(),
        requested_offset: offset,
        result: Ok(ArtifactContentPage {
            schema_version: 1,
            mission_id: MISSION_ID.to_string(),
            mission_run_id: RUN_ID.to_string(),
            artifact_id: artifact_id.to_string(),
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

/// The succeeded run of the v1.5 run history example.
pub const HISTORICAL_RUN_ID: &str = "run-0d14a731-6a9d-4b68-b612-4520619511c4";
/// The run of the v1.5 run history example whose Run Root is missing.
pub const MISSING_ROOT_RUN_ID: &str = "run-1";

/// A v1.5 Host, which serves the run history.
pub fn health_v1_5() -> Health {
    Health {
        status: "ok".to_string(),
        api_version: ApiVersion { major: 1, minor: 5 },
    }
}

/// The v1.5 run history example with its newest row retargeted to the
/// fixture's owned running run.
pub fn history_page() -> MissionRunsPage {
    let mut value = contract("v1.5", "mission-runs.response.json");
    let current = &mut value["mission_runs"][0];
    let run = &mut current["mission_run"];
    run["mission_id"] = json!(MISSION_ID);
    run["mission_run_id"] = json!(RUN_ID);
    run["status"] = json!("running");
    run["created_at"] = json!("2026-08-24T12:00:03Z");
    run["started_at"] = json!("2026-08-24T12:00:03Z");
    run["finished_at"] = Value::Null;
    run["terminal_classification"] = Value::Null;
    current["wall_seconds"] = Value::Null;
    serde_json::from_value(value).unwrap()
}

/// F3, then the newest history page answered from [`history_page`].
pub fn open_history(app: &mut App) {
    app.handle_key(key(KeyCode::F(3)));
    let commands = app.take_commands();
    assert_eq!(
        commands,
        [HostCommand::FetchRunHistory {
            before: None,
            limit: 50,
        }]
    );
    app.handle_host_message(HostMessage::RunHistory {
        before: None,
        result: Ok(history_page()),
    });
}

/// Move the history selection to `run_id`.
pub fn select_history_row(app: &mut App, run_id: &str) {
    app.handle_key(key(KeyCode::Home));
    for _ in 0..app.history.rows.len() {
        if app.history.selected.as_deref() == Some(run_id) {
            return;
        }
        app.handle_key(key(KeyCode::Down));
    }
    assert_eq!(app.history.selected.as_deref(), Some(run_id));
}

/// Answer every operator-view request in `commands` for `run_id` from the
/// examples as a terminal run with status `status`, then `edit` each page.
pub fn answer_sections_for(
    app: &mut App,
    commands: &[HostCommand],
    run_id: &str,
    status: &str,
    edit: impl Fn(OperatorSection, &mut Value),
) {
    for command in commands {
        let HostCommand::FetchOperatorView {
            mission_run_id,
            section,
            request_id,
            ..
        } = command
        else {
            continue;
        };
        if mission_run_id != run_id {
            continue;
        }
        let section = *section;
        let page = section_page(section, |page| {
            page["mission_run_id"] = json!(run_id);
            page["run_status"] = json!(status);
            page["has_more"] = json!(false);
            match section {
                OperatorSection::Artifacts => page["artifacts"] = json!([]),
                OperatorSection::Agents => page["agents"] = json!([]),
                OperatorSection::Overview => {
                    page["overview"]["narrative"]["terminal"] = json!(true);
                    page["overview"]["narrative"]["status"] = json!("unavailable");
                }
                _ => {}
            }
            edit(section, page);
        });
        app.handle_host_message(HostMessage::OperatorView {
            mission_run_id: run_id.to_string(),
            section,
            request_id: *request_id,
            result: Ok(Fetched::Fresh {
                value: page,
                etag: None,
            }),
        });
    }
}
