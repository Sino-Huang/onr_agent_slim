//! Slice A: state machine and keyboard behavior tests.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use operator_console::app::{App, AppState, HostCommand, HostMessage, MIN_HEIGHT, MIN_WIDTH};
use operator_console::host::{
    ActivationAccepted, ActivationOutcome, CurrentRun, Health, HostError, RunRecord,
};

fn health() -> Health {
    Health {
        status: "ok".to_string(),
        api_version: operator_console::host::ApiVersion { major: 1, minor: 0 },
    }
}

fn connected_app() -> App {
    let mut app = App::new("http://127.0.0.1:8787".to_string());
    assert_eq!(app.state, AppState::Connecting);
    assert_eq!(app.take_commands(), vec![HostCommand::Connect]);
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    assert_eq!(app.state, AppState::Editing);
    app
}

fn poll_command(app: &App) -> HostCommand {
    HostCommand::PollCurrent {
        credential: app.session.credential.clone(),
    }
}

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn alt_enter() -> KeyEvent {
    KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)
}

fn type_text(app: &mut App, text: &str) {
    for c in text.chars() {
        app.handle_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
    }
}

fn accepted() -> ActivationAccepted {
    ActivationAccepted {
        activation_request_id: "req-1".to_string(),
        mission_id: "mission-1".to_string(),
        mission_run_id: "run-1".to_string(),
        status: "queued".to_string(),
        created_at: "2026-08-24T12:00:00Z".to_string(),
    }
}

#[test]
fn new_app_connects_first() {
    let app = App::new("http://127.0.0.1:8787".to_string());
    assert_eq!(app.state, AppState::Connecting);
    assert!(!app.session.session_id.is_empty());
    assert!(app.session.credential.len() >= 32);
}

#[test]
fn connect_failure_enters_retryable_error() {
    let mut app = App::new("http://127.0.0.1:8787".to_string());
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Err(HostError::Transport(
        "refused".to_string(),
    ))));
    assert_eq!(
        app.state,
        AppState::Error {
            message: "Cannot reach Runtime Host at http://127.0.0.1:8787: transport error: refused"
                .to_string(),
            retry_connect: true,
        }
    );
}

#[test]
fn incompatible_host_version_is_rejected_before_mutation() {
    let mut app = App::new("http://127.0.0.1:8787".to_string());
    app.take_commands();
    let mut bad = health();
    bad.api_version.major = 2;
    app.handle_host_message(HostMessage::Connected(Ok(bad)));
    match app.state {
        AppState::Error { retry_connect, .. } => assert!(retry_connect),
        other => panic!("expected Error, got {other:?}"),
    }
    assert!(app.take_commands().is_empty());
}

#[test]
fn error_r_retries_connection() {
    let mut app = App::new("http://127.0.0.1:8787".to_string());
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Err(HostError::Transport(
        "refused".to_string(),
    ))));
    app.handle_key(key(KeyCode::Char('r')));
    assert_eq!(app.state, AppState::Connecting);
    assert_eq!(app.take_commands(), vec![HostCommand::Connect]);
}

#[test]
fn bare_enter_inserts_newline_in_editor() {
    let mut app = connected_app();
    type_text(&mut app, "line one");
    app.handle_key(key(KeyCode::Enter));
    type_text(&mut app, "line two");
    assert_eq!(app.intent, "line one\nline two");
    assert_eq!(app.state, AppState::Editing);
}

#[test]
fn editing_supports_cursor_movement_and_deletion() {
    let mut app = connected_app();
    type_text(&mut app, "ab\ncd");
    assert_eq!(app.cursor_line_col(), (1, 2));
    app.handle_key(key(KeyCode::Up));
    assert_eq!(app.cursor_line_col(), (0, 2));
    app.handle_key(key(KeyCode::Home));
    assert_eq!(app.cursor_line_col(), (0, 0));
    app.handle_key(key(KeyCode::Delete));
    assert_eq!(app.intent, "b\ncd");
    app.handle_key(key(KeyCode::End));
    app.handle_key(key(KeyCode::Backspace));
    assert_eq!(app.intent, "\ncd");
}

#[test]
fn alt_enter_opens_review_for_nonempty_intent() {
    let mut app = connected_app();
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::ReviewActivation);
}

#[test]
fn alt_enter_on_empty_intent_stays_editing_with_hint() {
    let mut app = connected_app();
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::Editing);
    assert!(app.hint.is_some());
}

#[test]
fn escape_in_review_returns_to_editing() {
    let mut app = connected_app();
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.state, AppState::Editing);
    assert!(app.take_commands().is_empty());
}

#[test]
fn confirm_submits_activation_exactly_once() {
    let mut app = connected_app();
    type_text(&mut app, "survey\nthe ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.state, AppState::Submitting);
    // Repeated confirms while submitting must not enqueue more activations.
    app.handle_key(key(KeyCode::Enter));
    app.handle_key(alt_enter());
    let commands = app.take_commands();
    assert_eq!(commands.len(), 1);
    match &commands[0] {
        HostCommand::Submit {
            request,
            credential,
        } => {
            assert_eq!(request.mission_intent, "survey\nthe ridge");
            assert_eq!(request.console_session_id, app.session.session_id);
            assert_eq!(request.source_authority, "operator_console");
            assert!(!request.activation_request_id.is_empty());
            assert_eq!(credential, &app.session.credential);
        }
        other => panic!("expected Submit, got {other:?}"),
    }
}

#[test]
fn retry_of_same_intent_reuses_activation_request_id() {
    let mut app = connected_app();
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    let first = match app.take_commands().remove(0) {
        HostCommand::Submit { request, .. } => request.activation_request_id,
        other => panic!("expected Submit, got {other:?}"),
    };
    app.handle_host_message(HostMessage::Activated(Err(HostError::Transport(
        "lost connection".to_string(),
    ))));
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.state, AppState::Editing);
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    let second = match app.take_commands().remove(0) {
        HostCommand::Submit { request, .. } => request.activation_request_id,
        other => panic!("expected Submit, got {other:?}"),
    };
    assert_eq!(first, second);
}

#[test]
fn editing_after_failed_submit_keeps_intent() {
    let mut app = connected_app();
    type_text(&mut app, "hold position");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Rejected {
        code: "mission_run_active".to_string(),
        message: "a Mission Run is already active".to_string(),
    })));
    match &app.state {
        AppState::Error {
            message,
            retry_connect,
        } => {
            assert!(message.contains("mission_run_active"));
            assert!(!retry_connect);
        }
        other => panic!("expected Error, got {other:?}"),
    }
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.intent, "hold position");
}

#[test]
fn accepted_activation_enters_run_and_polls() {
    let mut app = connected_app();
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    assert_eq!(app.state, AppState::Run);
    assert_eq!(app.take_commands(), vec![poll_command(&app)]);
    let run = app
        .run
        .as_ref()
        .expect("run snapshot seeded from acceptance");
    assert_eq!(run.mission_id, "mission-1");
    assert_eq!(run.mission_run_id, "run-1");
    assert_eq!(run.status, "queued");
}

#[test]
fn poll_updates_run_snapshot_and_notice_on_failure() {
    let mut app = connected_app();
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    let record = RunRecord {
        mission_id: "mission-1".to_string(),
        mission_run_id: "run-1".to_string(),
        status: "running".to_string(),
        created_at: Some("2026-08-24T12:00:00Z".to_string()),
        started_at: Some("2026-08-24T12:00:03Z".to_string()),
        finished_at: None,
        terminal_classification: None,
    };
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(record),
    })));
    assert_eq!(app.run.as_ref().unwrap().status, "running");
    assert!(app.notice.is_none());
    app.handle_host_message(HostMessage::Current(Err(HostError::Transport(
        "timeout".to_string(),
    ))));
    assert!(app.notice.is_some());
    assert_eq!(app.run.as_ref().unwrap().status, "running");
}

#[test]
fn poll_requests_only_fire_in_run_state() {
    let mut app = connected_app();
    app.request_poll();
    assert!(app.take_commands().is_empty());
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    app.request_poll();
    assert_eq!(app.take_commands(), vec![poll_command(&app)]);
}

#[test]
fn resize_below_minimum_overlays_resize_required_and_recovers() {
    let mut app = connected_app();
    app.handle_resize(MIN_WIDTH - 1, MIN_HEIGHT);
    match &app.state {
        AppState::ResizeRequired { resume } => assert_eq!(**resume, AppState::Editing),
        other => panic!("expected ResizeRequired, got {other:?}"),
    }
    assert_eq!(app.logical_state_name(), "Editing");
    // Keys are swallowed while too small.
    type_text(&mut app, "ignored");
    assert_eq!(app.intent, "");
    app.handle_resize(MIN_WIDTH, MIN_HEIGHT);
    assert_eq!(app.state, AppState::Editing);
}

#[test]
fn resize_overlay_preserves_submitting_then_run_transition() {
    let mut app = connected_app();
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_resize(80, 24);
    assert_eq!(app.state.name(), "ResizeRequired");
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    assert_eq!(app.logical_state_name(), "Run");
    app.handle_resize(MIN_WIDTH, MIN_HEIGHT);
    assert_eq!(app.state, AppState::Run);
}

#[test]
fn ctrl_c_quits_from_any_state() {
    let mut app = connected_app();
    app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
    assert!(app.should_quit());
}
