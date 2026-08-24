//! Slice B: Ratatui `TestBackend` render tests.
//!
//! The required state frames are committed as readable plain-text captures
//! under `docs/design/operator-console/frames/` and treated as test fixtures.
//! Regenerate after an intentional layout change with:
//!
//! ```sh
//! UPDATE_FRAMES=1 cargo test --test render
//! ```

use std::fs;
use std::path::PathBuf;

use operator_console::app::{App, AppState, HostMessage, MIN_HEIGHT, MIN_WIDTH};
use operator_console::host::{ActivationAccepted, CurrentRun, RunRecord};
use ratatui::Terminal;
use ratatui::backend::TestBackend;

const SESSION_ID: &str = "c0ns01e0-0000-4000-8000-5e5510n5a1d0";

fn frames_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../docs/design/operator-console/frames")
}

fn render(app: &App, width: u16, height: u16) -> String {
    let backend = TestBackend::new(width, height);
    let mut terminal = Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| operator_console::ui::draw(frame, app))
        .unwrap();
    let buffer = terminal.backend().buffer();
    let mut lines: Vec<String> = (0..height)
        .map(|y| {
            let mut line = String::new();
            for x in 0..width {
                line.push_str(buffer[(x, y)].symbol());
            }
            line.trim_end().to_string()
        })
        .collect();
    lines.push(String::new());
    lines.join("\n")
}

fn assert_frame(name: &str, actual: String) {
    let path = frames_dir().join(name);
    if std::env::var_os("UPDATE_FRAMES").is_some() {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, &actual).unwrap();
        return;
    }
    let expected = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("missing fixture frame {}: {e}", path.display()));
    let expected: Vec<String> = expected.lines().map(|l| l.trim_end().to_string()).collect();
    let actual_lines: Vec<String> = actual.lines().map(|l| l.trim_end().to_string()).collect();
    assert_eq!(
        actual_lines, expected,
        "frame mismatch for {name}; run UPDATE_FRAMES=1 cargo test --test render to regenerate"
    );
}

fn editing_app(intent: &str) -> App {
    let mut app = App::new("http://127.0.0.1:8787".to_string());
    app.session.session_id = SESSION_ID.to_string();
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(operator_console::host::Health {
        status: "ok".to_string(),
        api_version: operator_console::host::ApiVersion { major: 1, minor: 0 },
    })));
    app.intent = intent.to_string();
    app.cursor = intent.chars().count();
    app
}

fn review_app() -> App {
    let mut app = editing_app("Hold the ridge line.\nReport obstacles by grid square.");
    app.handle_key(crossterm::event::KeyEvent::new(
        crossterm::event::KeyCode::Enter,
        crossterm::event::KeyModifiers::ALT,
    ));
    app.pin_review_request_id("req-8f3a1c2e-4b5d-4e6f-9a0b-1c2d3e4f5a6b");
    assert_eq!(app.state, AppState::ReviewActivation);
    app
}

fn run_app(record: RunRecord) -> App {
    let mut app = review_app();
    app.handle_key(crossterm::event::KeyEvent::new(
        crossterm::event::KeyCode::Enter,
        crossterm::event::KeyModifiers::NONE,
    ));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(
        operator_console::host::ActivationOutcome::Accepted(ActivationAccepted {
            activation_request_id: "req".to_string(),
            mission_id: record.mission_id.clone(),
            mission_run_id: record.mission_run_id.clone(),
            status: record.status.clone(),
            created_at: record.created_at.clone().unwrap_or_default(),
        }),
    )));
    app.take_commands();
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(record),
    })));
    assert_eq!(app.state, AppState::Run);
    app
}

fn running_record() -> RunRecord {
    RunRecord {
        mission_id: "mission-7c1f9a2e".to_string(),
        mission_run_id: "run-51d3b84c".to_string(),
        status: "running".to_string(),
        created_at: Some("2026-08-24T12:00:00Z".to_string()),
        started_at: Some("2026-08-24T12:00:03Z".to_string()),
        finished_at: None,
        terminal_classification: None,
    }
}

#[test]
fn editing_frame_matches_committed_capture() {
    let app = editing_app("Hold the ridge line.\nReport obstacles by grid square.");
    assert_frame("editing-100x30.txt", render(&app, MIN_WIDTH, MIN_HEIGHT));
}

#[test]
fn review_frame_matches_committed_capture() {
    let app = review_app();
    assert_frame(
        "review-activation-100x30.txt",
        render(&app, MIN_WIDTH, MIN_HEIGHT),
    );
}

#[test]
fn run_dashboard_frame_matches_committed_capture() {
    let app = run_app(running_record());
    assert_frame(
        "run-dashboard-100x30.txt",
        render(&app, MIN_WIDTH, MIN_HEIGHT),
    );
}

#[test]
fn resize_required_frame_matches_committed_capture() {
    let mut app = run_app(running_record());
    app.handle_resize(80, 24);
    assert_eq!(app.state.name(), "ResizeRequired");
    assert_frame("resize-required-80x24.txt", render(&app, 80, 24));
}

#[test]
fn connecting_frame_reports_host() {
    let mut app = App::new("http://127.0.0.1:8787".to_string());
    app.session.session_id = SESSION_ID.to_string();
    let frame = render(&app, MIN_WIDTH, MIN_HEIGHT);
    assert!(frame.contains("Connecting to Runtime Host at http://127.0.0.1:8787"));
}

#[test]
fn submitting_frame_shows_pending_acknowledgement() {
    let mut app = review_app();
    app.handle_key(crossterm::event::KeyEvent::new(
        crossterm::event::KeyCode::Enter,
        crossterm::event::KeyModifiers::NONE,
    ));
    assert_eq!(app.state, AppState::Submitting);
    let frame = render(&app, MIN_WIDTH, MIN_HEIGHT);
    assert!(frame.contains("Submitting Mission Activation"));
}

#[test]
fn error_frame_shows_message_and_recovery_hints() {
    let mut app = editing_app("hold position");
    app.handle_host_message(HostMessage::Activated(Err(
        operator_console::host::HostError::Transport("connection lost".to_string()),
    )));
    let frame = render(&app, MIN_WIDTH, MIN_HEIGHT);
    assert!(frame.contains("Activation failed: transport error: connection lost"));
    assert!(frame.contains("Esc: return to editing"));
}

#[test]
fn dashboard_marks_terminal_classification() {
    let mut record = running_record();
    record.status = "failed".to_string();
    record.finished_at = Some("2026-08-24T12:05:00Z".to_string());
    record.terminal_classification = Some("host_interrupted".to_string());
    let app = run_app(record);
    let frame = render(&app, MIN_WIDTH, MIN_HEIGHT);
    assert!(frame.contains("failed"));
    assert!(frame.contains("host_interrupted"));
}

#[test]
fn below_minimum_renders_only_resize_required() {
    let app = run_app(running_record());
    let frame = render(&app, MIN_WIDTH - 1, MIN_HEIGHT - 1);
    assert!(frame.contains("Terminal too small"));
    assert!(!frame.contains("Run Activities"));
    assert!(!frame.contains("Mission Intent"));
}
