//! Slice A: state machine and keyboard behavior tests.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use operator_console::app::{
    App, AppState, CancellationState, CleanExitAction, Clock, HostCommand, HostMessage, Liveness,
    LivenessThresholds, MIN_HEIGHT, MIN_WIDTH, OwnerSessionState, SessionStateFile,
};
use operator_console::host::{
    ActivationAccepted, ActivationOutcome, ActivitiesPage, CancellationAccepted,
    CancellationOutcome, CurrentRun, EvidencePage, Health, HostError, MissionIntent,
    ObservationEnvelope, ObservationsPage, RunActivity, RunRecord,
};
use std::fs;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Debug)]
struct ManualClock {
    now: Mutex<Instant>,
}

impl ManualClock {
    fn new(now: Instant) -> Self {
        Self {
            now: Mutex::new(now),
        }
    }

    fn advance(&self, duration: Duration) {
        let mut now = self.now.lock().unwrap();
        *now += duration;
    }
}

impl Clock for ManualClock {
    fn now(&self) -> Instant {
        *self.now.lock().unwrap()
    }
}

fn health() -> Health {
    Health {
        status: "ok".to_string(),
        api_version: operator_console::host::ApiVersion { major: 1, minor: 0 },
    }
}

fn connected_app() -> App {
    let mut app = App::new_with_session_file(
        "http://127.0.0.1:8787".to_string(),
        temp_state_file("connected"),
    );
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

fn poll_commands(app: &App) -> Vec<HostCommand> {
    let run_id = app.run.as_ref().unwrap().mission_run_id.clone();
    vec![
        poll_command(app),
        HostCommand::FetchActivities {
            mission_run_id: run_id.clone(),
        },
        HostCommand::FetchObservations {
            mission_run_id: run_id,
        },
    ]
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

fn activities() -> Vec<RunActivity> {
    serde_json::from_str::<ActivitiesPage>(include_str!(
        "../../docs/design/operator-console/contract/v1/mission-run-activities.page.response.json"
    ))
    .unwrap()
    .activities
}

fn observations() -> Vec<ObservationEnvelope> {
    serde_json::from_str::<ObservationsPage>(include_str!(
        "../../docs/design/operator-console/contract/v1/mission-run-observations.page.response.json"
    ))
    .unwrap()
    .observations
}

fn evidence<T>(items: Vec<T>) -> EvidencePage<T> {
    EvidencePage {
        items,
        truncated: false,
    }
}

fn active_run_app_with_clock(clock: Arc<ManualClock>) -> App {
    let mut app = App::new_with_session_file_and_clock(
        "http://127.0.0.1:8787".to_string(),
        temp_state_file("liveness"),
        clock,
    )
    .with_liveness_thresholds(LivenessThresholds {
        stale: Duration::from_secs(5),
        offline: Duration::from_secs(30),
    });
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.intent = "survey the ridge".to_string();
    app.cursor = app.intent.len();
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    app
}

fn temp_state_file(name: &str) -> SessionStateFile {
    let path = std::env::temp_dir()
        .join(format!("operator-console-{name}-{}", uuid::Uuid::new_v4()))
        .join("operator-console/session.json");
    SessionStateFile::at(path)
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
    assert_eq!(app.take_commands(), poll_commands(&app));
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
    assert_eq!(app.take_commands(), poll_commands(&app));
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

#[test]
fn session_state_file_is_atomic_owner_only_and_round_trips_only_authority_fields() {
    let root = std::env::temp_dir().join(format!(
        "operator-console-round-trip-{}",
        uuid::Uuid::new_v4()
    ));
    let file = SessionStateFile::at(root.join("onr/operator-console/session.json"));
    let state = OwnerSessionState {
        host_authority: "http://127.0.0.1:8787".to_string(),
        host_api_major: 1,
        mission_run_id: "run-1".to_string(),
        console_session_id: "session-1".to_string(),
        credential: "credential-1".to_string(),
    };
    file.save(&state).unwrap();
    assert_eq!(file.load().unwrap(), Some(state));
    let value: serde_json::Value = serde_json::from_slice(&fs::read(file.path()).unwrap()).unwrap();
    assert_eq!(value.as_object().unwrap().len(), 5);
    assert!(!value.as_object().unwrap().contains_key("pid"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(file.path()).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(root.join("onr")).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(root.join("onr/operator-console"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
    }
    file.remove().unwrap();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn session_save_hardens_existing_final_directory_without_chmodding_ancestor() {
    let root = std::env::temp_dir().join(format!(
        "operator-console-existing-state-{}",
        uuid::Uuid::new_v4()
    ));
    let parent = root.join("operator-console");
    fs::create_dir_all(&parent).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let file = SessionStateFile::at(parent.join("session.json"));
    file.save(&OwnerSessionState {
        host_authority: "http://127.0.0.1:8787".to_string(),
        host_api_major: 1,
        mission_run_id: "run-1".to_string(),
        console_session_id: "session-1".to_string(),
        credential: "credential-1".to_string(),
    })
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&parent).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(&root).unwrap().permissions().mode() & 0o777,
            0o755
        );
    }
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn recovered_owner_reads_intent_through_host_authorization() {
    let file = temp_state_file("recover");
    file.save(&OwnerSessionState {
        host_authority: "http://127.0.0.1:8787".to_string(),
        host_api_major: 1,
        mission_run_id: "run-1".to_string(),
        console_session_id: "session-recovered".to_string(),
        credential: "credential-recovered".to_string(),
    })
    .unwrap();
    let mut app = App::new_with_session_file("http://127.0.0.1:8787".to_string(), file.clone());
    assert_eq!(app.session.session_id, "session-recovered");
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    let commands = app.take_commands();
    assert!(commands.contains(&HostCommand::FetchIntent {
        mission_run_id: "run-1".to_string(),
        credential: "credential-recovered".to_string(),
    }));
    assert!(commands.contains(&HostCommand::PollCurrent {
        credential: "credential-recovered".to_string(),
    }));
    app.handle_host_message(HostMessage::Intent(Ok(MissionIntent {
        mission_run_id: "run-1".to_string(),
        mission_intent: "hold the ridge".to_string(),
        source_authority: "operator_console".to_string(),
    })));
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            mission_id: "mission-1".to_string(),
            mission_run_id: "run-1".to_string(),
            status: "running".to_string(),
            created_at: None,
            started_at: None,
            finished_at: None,
            terminal_classification: None,
        }),
    })));
    assert_eq!(app.logical_state_name(), "Run");
    assert_eq!(app.intent, "hold the ridge");
    assert!(app.recovered_owner());
    file.remove().unwrap();
}

#[test]
fn recovered_owner_rejects_mismatched_current_run_and_keeps_session_record() {
    let file = temp_state_file("recover-mismatch");
    file.save(&OwnerSessionState {
        host_authority: "http://127.0.0.1:8787".to_string(),
        host_api_major: 1,
        mission_run_id: "run-owned".to_string(),
        console_session_id: "session-recovered".to_string(),
        credential: "credential-recovered".to_string(),
    })
    .unwrap();
    let mut app = App::new_with_session_file("http://127.0.0.1:8787".to_string(), file.clone());
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.take_commands();
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            mission_id: "mission-independent".to_string(),
            mission_run_id: "run-independent".to_string(),
            status: "cancelled".to_string(),
            created_at: None,
            started_at: None,
            finished_at: Some("2026-08-24T12:05:01Z".to_string()),
            terminal_classification: None,
        }),
    })));
    assert_eq!(app.logical_state_name(), "Connecting");
    assert!(app.run.is_none());
    assert!(file.path().exists());
    assert_eq!(app.take_clean_exit_action(), None);
    assert!(app.notice.as_deref().unwrap().contains("run-independent"));
    file.remove().unwrap();
}

#[test]
fn stale_recovery_authorization_keeps_session_record_and_does_not_exit() {
    let file = temp_state_file("recover-stale-credential");
    file.save(&OwnerSessionState {
        host_authority: "http://127.0.0.1:8787".to_string(),
        host_api_major: 1,
        mission_run_id: "run-owned".to_string(),
        console_session_id: "session-recovered".to_string(),
        credential: "stale-credential".to_string(),
    })
    .unwrap();
    let mut app = App::new_with_session_file("http://127.0.0.1:8787".to_string(), file.clone());
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.handle_host_message(HostMessage::Intent(Err(HostError::AuthorizationFailed {
        code: "authorization_failed".to_string(),
        message: "request is not authorized".to_string(),
    })));
    assert!(file.path().exists());
    assert_eq!(app.take_clean_exit_action(), None);
    assert!(matches!(app.state, AppState::Error { .. }));
    file.remove().unwrap();
}

#[test]
fn c_requires_confirmation_and_confirm_enqueues_one_idempotent_cancellation() {
    let file = temp_state_file("cancel");
    let mut app = connected_app();
    app.set_session_state_file(file);
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    app.handle_key(key(KeyCode::Char('c')));
    assert!(matches!(app.cancellation, CancellationState::Confirming));
    assert!(app.take_commands().is_empty());
    app.handle_key(key(KeyCode::Enter));
    let commands = app.take_commands();
    assert_eq!(commands.len(), 1);
    let HostCommand::Cancel {
        request,
        mission_run_id,
        credential,
    } = &commands[0]
    else {
        panic!("expected cancellation command");
    };
    assert_eq!(mission_run_id, "run-1");
    assert_eq!(credential, &app.session.credential);
    let request_id = request.cancellation_request_id.clone();
    app.handle_key(key(KeyCode::Enter));
    assert!(app.take_commands().is_empty());
    assert_eq!(app.cancellation, CancellationState::Confirming);
    app.handle_host_message(HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(
        CancellationAccepted {
            mission_run_id: "run-1".to_string(),
            cancellation_request_id: request_id.clone(),
            disposition: "cancellation_requested".to_string(),
            status: "running".to_string(),
            requested_at: "2026-08-24T12:05:00Z".to_string(),
        },
    ))));
    assert!(matches!(
        app.cancellation,
        CancellationState::Requested { ref cancellation_request_id } if cancellation_request_id == &request_id
    ));
}

#[test]
fn mismatched_cancellation_acceptance_never_enters_requested() {
    for (name, mission_run_id, cancellation_request_id, disposition) in [
        ("run-id", "run-other", "pending", "cancellation_requested"),
        (
            "request-id",
            "run-1",
            "cancel-other",
            "cancellation_requested",
        ),
        ("disposition", "run-1", "pending", "cancelled"),
    ] {
        let mut app = connected_app();
        type_text(&mut app, "survey the ridge");
        app.handle_key(alt_enter());
        app.handle_key(key(KeyCode::Enter));
        app.take_commands();
        app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
            accepted(),
        ))));
        app.take_commands();
        app.handle_key(key(KeyCode::Char('c')));
        app.handle_key(key(KeyCode::Enter));
        let pending = match app.take_commands().remove(0) {
            HostCommand::Cancel { request, .. } => request.cancellation_request_id,
            other => panic!("expected Cancel, got {other:?}"),
        };
        let response_request_id = if cancellation_request_id == "pending" {
            pending
        } else {
            cancellation_request_id.to_string()
        };
        app.handle_host_message(HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(
            CancellationAccepted {
                mission_run_id: mission_run_id.to_string(),
                cancellation_request_id: response_request_id,
                disposition: disposition.to_string(),
                status: "running".to_string(),
                requested_at: "2026-08-24T12:05:00Z".to_string(),
            },
        ))));
        assert_eq!(app.cancellation, CancellationState::Idle, "case {name}");
        assert!(
            app.notice
                .as_deref()
                .is_some_and(|notice| notice.contains("Cancellation contract failure")),
            "case {name}: {:?}",
            app.notice
        );
        assert_eq!(app.take_clean_exit_action(), None, "case {name}");
    }
}

#[test]
fn q_reuses_c_cancellation_confirmation_only_in_run_state() {
    fn active_run_app() -> App {
        let mut app = connected_app();
        type_text(&mut app, "survey the ridge");
        app.handle_key(alt_enter());
        app.handle_key(key(KeyCode::Enter));
        app.take_commands();
        app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
            accepted(),
        ))));
        app.take_commands();
        app
    }

    let mut c_app = active_run_app();
    let mut q_app = active_run_app();
    c_app.handle_key(key(KeyCode::Char('c')));
    q_app.handle_key(key(KeyCode::Char('q')));
    assert_eq!(q_app.cancellation, c_app.cancellation);
    assert_eq!(q_app.cancellation, CancellationState::Confirming);
    assert!(q_app.take_commands().is_empty());

    c_app.handle_key(key(KeyCode::Enter));
    q_app.handle_key(key(KeyCode::Enter));
    let c_command = c_app.take_commands().remove(0);
    let q_command = q_app.take_commands().remove(0);
    let (
        HostCommand::Cancel {
            mission_run_id: c_run,
            credential: c_credential,
            ..
        },
        HostCommand::Cancel {
            mission_run_id: q_run,
            credential: q_credential,
            ..
        },
    ) = (c_command, q_command)
    else {
        panic!("c and q must both enqueue cancellation");
    };
    assert_eq!(q_run, c_run);
    assert_eq!(q_credential, q_app.session.credential);
    assert_eq!(c_credential, c_app.session.credential);
    assert_eq!(q_app.cancellation, CancellationState::Confirming);

    let mut editing = connected_app();
    editing.handle_key(key(KeyCode::Char('q')));
    assert_eq!(editing.state, AppState::Editing);
    assert_eq!(editing.intent, "q");
    assert_eq!(editing.cancellation, CancellationState::Idle);
}

#[test]
fn q_cancellation_removes_state_and_exits_only_after_cancelled_poll() {
    let file = temp_state_file("terminal-cancelled");
    let mut app = connected_app();
    app.set_session_state_file(file.clone());
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    assert!(file.path().exists());
    app.take_commands();
    app.handle_key(key(KeyCode::Char('q')));
    app.handle_key(key(KeyCode::Enter));
    let request_id = match app.take_commands().remove(0) {
        HostCommand::Cancel { request, .. } => request.cancellation_request_id,
        other => panic!("expected Cancel, got {other:?}"),
    };
    app.handle_host_message(HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(
        CancellationAccepted {
            mission_run_id: "run-1".to_string(),
            cancellation_request_id: request_id,
            disposition: "cancellation_requested".to_string(),
            status: "running".to_string(),
            requested_at: "2026-08-24T12:05:00Z".to_string(),
        },
    ))));
    assert!(file.path().exists());
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            mission_id: "mission-1".to_string(),
            mission_run_id: "run-1".to_string(),
            status: "cancelled".to_string(),
            created_at: None,
            started_at: None,
            finished_at: Some("2026-08-24T12:05:01Z".to_string()),
            terminal_classification: None,
        }),
    })));
    assert!(!file.path().exists());
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::Cancelled)
    );
}

#[test]
fn c_cancellation_stays_open_after_host_reports_terminal_cancellation() {
    let file = temp_state_file("c-terminal-cancelled");
    let mut app = connected_app();
    app.set_session_state_file(file.clone());
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    app.handle_key(key(KeyCode::Char('c')));
    app.handle_key(key(KeyCode::Enter));
    let request_id = match app.take_commands().remove(0) {
        HostCommand::Cancel { request, .. } => request.cancellation_request_id,
        other => panic!("expected Cancel, got {other:?}"),
    };
    app.handle_host_message(HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(
        CancellationAccepted {
            mission_run_id: "run-1".to_string(),
            cancellation_request_id: request_id,
            disposition: "cancellation_requested".to_string(),
            status: "running".to_string(),
            requested_at: "2026-08-24T12:05:00Z".to_string(),
        },
    ))));
    app.take_commands();
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            mission_id: "mission-1".to_string(),
            mission_run_id: "run-1".to_string(),
            status: "cancelled".to_string(),
            created_at: None,
            started_at: None,
            finished_at: Some("2026-08-24T12:05:01Z".to_string()),
            terminal_classification: None,
        }),
    })));
    assert!(file.path().exists());
    assert_eq!(app.take_clean_exit_action(), None);
    assert_eq!(app.logical_state_name(), "Run");
    assert_eq!(app.cancellation, CancellationState::Idle);
    file.remove().unwrap();
}

#[test]
fn q_on_owned_terminal_run_exits_directly_without_cancellation_request() {
    let file = temp_state_file("q-terminal-run");
    let mut app = connected_app();
    app.set_session_state_file(file.clone());
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            mission_id: "mission-1".to_string(),
            mission_run_id: "run-1".to_string(),
            status: "succeeded".to_string(),
            created_at: None,
            started_at: None,
            finished_at: Some("2026-08-24T12:05:01Z".to_string()),
            terminal_classification: None,
        }),
    })));
    assert!(file.path().exists());
    app.handle_key(key(KeyCode::Char('q')));
    assert!(app.take_commands().is_empty());
    assert!(!file.path().exists());
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::TerminalRun)
    );
}

#[test]
fn confirmed_cancellation_times_out_at_ten_seconds_with_deterministic_clock() {
    let file = temp_state_file("cancel-timeout");
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = App::new_with_session_file_and_clock(
        "http://127.0.0.1:8787".to_string(),
        file.clone(),
        clock.clone(),
    );
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    app.handle_key(key(KeyCode::Char('q')));
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();

    clock.advance(Duration::from_secs(9));
    clock.advance(Duration::from_millis(999));
    app.check_deadlines();
    assert_eq!(app.take_clean_exit_action(), None);
    assert!(file.path().exists());

    clock.advance(Duration::from_millis(1));
    app.check_deadlines();
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::CancellationTimedOut)
    );
    assert!(file.path().exists());
    assert!(app.take_commands().is_empty());
    file.remove().unwrap();
}

#[test]
fn q_cancellation_submit_failure_keeps_deadline_and_exits_at_ten_seconds() {
    let file = temp_state_file("q-submit-failure-timeout");
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = App::new_with_session_file_and_clock(
        "http://127.0.0.1:8787".to_string(),
        file.clone(),
        clock.clone(),
    );
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    type_text(&mut app, "survey the ridge");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        accepted(),
    ))));
    app.take_commands();
    app.handle_key(key(KeyCode::Char('q')));
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Cancelled(Err(HostError::Transport(
        "timeout".to_string(),
    ))));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert!(
        app.notice
            .as_deref()
            .unwrap()
            .contains("Cancellation failed")
    );

    clock.advance(Duration::from_secs(10));
    app.check_deadlines();
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::CancellationTimedOut)
    );
    assert!(file.path().exists());
    file.remove().unwrap();
}

#[test]
fn liveness_uses_inclusive_response_receipt_boundaries() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let app = active_run_app_with_clock(clock.clone());
    assert!(app.last_host_response.is_some());
    assert_eq!(app.liveness(), Liveness::Live);
    clock.advance(Duration::from_secs(5));
    assert_eq!(app.liveness(), Liveness::Stale);
    clock.advance(Duration::from_secs(25));
    assert_eq!(app.liveness(), Liveness::Offline);
}

#[test]
fn evidence_and_mutations_survive_an_error_gap_and_recover() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = active_run_app_with_clock(clock.clone());
    app.handle_host_message(HostMessage::Activities(Ok(evidence(activities()))));
    app.handle_host_message(HostMessage::Observations(Ok(evidence(observations()))));
    let response_at = app.last_host_response;
    app.handle_host_message(HostMessage::Activities(Err(HostError::Transport(
        "timeout".to_string(),
    ))));
    app.handle_host_message(HostMessage::Current(Err(HostError::Transport(
        "timeout".to_string(),
    ))));
    assert_eq!(app.last_host_response, response_at);
    assert_eq!(app.activities.len(), 2);
    assert_eq!(app.observations.len(), 3);
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, "run-1");

    clock.advance(Duration::from_secs(5));
    app.handle_key(key(KeyCode::Char('c')));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert_eq!(
        app.notice.as_deref(),
        Some("Mutation controls disabled while the Host connection is stale or offline")
    );
    app.handle_key(key(KeyCode::Char('q')));
    assert_eq!(app.cancellation, CancellationState::Idle);

    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: app.run.clone(),
    })));
    assert_eq!(app.liveness(), Liveness::Live);
    assert_eq!(app.activities.len(), 2);
    assert_eq!(app.observations.len(), 3);
    app.handle_key(key(KeyCode::Char('c')));
    assert_eq!(app.cancellation, CancellationState::Confirming);
}

#[test]
fn run_poll_fans_out_to_current_activities_and_observations() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = active_run_app_with_clock(clock);
    app.request_poll();
    assert_eq!(
        app.take_commands(),
        vec![
            poll_command(&app),
            HostCommand::FetchActivities {
                mission_run_id: "run-1".to_string(),
            },
            HostCommand::FetchObservations {
                mission_run_id: "run-1".to_string(),
            },
        ]
    );
}

#[test]
fn activity_selection_is_stable_moves_and_drops_when_empty() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = active_run_app_with_clock(clock);
    let list = activities();
    app.handle_host_message(HostMessage::Activities(Ok(evidence(list.clone()))));
    assert_eq!(app.selected_activity().unwrap().0, 0);
    app.handle_key(key(KeyCode::Down));
    assert_eq!(app.selected_activity().unwrap().0, 1);
    app.handle_key(key(KeyCode::Down));
    assert_eq!(app.selected_activity().unwrap().0, 1);
    app.handle_key(key(KeyCode::Char('k')));
    assert_eq!(app.selected_activity().unwrap().0, 0);
    app.handle_key(key(KeyCode::Char('j')));
    let selected_id = app.selected_activity().unwrap().1.activity_id.clone();
    app.handle_host_message(HostMessage::Activities(Ok(evidence(list))));
    assert_eq!(app.selected_activity().unwrap().1.activity_id, selected_id);
    app.handle_host_message(HostMessage::Activities(Ok(evidence(Vec::new()))));
    assert!(app.selected_activity().is_none());
}

#[test]
fn selected_observations_returns_only_activity_links() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = active_run_app_with_clock(clock);
    app.handle_host_message(HostMessage::Activities(Ok(evidence(activities()))));
    app.handle_host_message(HostMessage::Observations(Ok(evidence(observations()))));
    app.handle_key(key(KeyCode::Down));
    let selected = app.selected_observations();
    assert_eq!(
        selected
            .iter()
            .map(|item| item.observation_sequence)
            .collect::<Vec<_>>(),
        vec![2, 3]
    );
}

#[test]
fn definitive_host_errors_refresh_liveness_but_transport_does_not() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = active_run_app_with_clock(clock.clone());

    for message in [
        HostMessage::Activities(Err(HostError::NotFound {
            code: "mission_run_not_found".to_string(),
            message: "missing".to_string(),
        })),
        HostMessage::Observations(Err(HostError::InvalidCursor {
            code: "invalid_cursor".to_string(),
            message: "invalid".to_string(),
        })),
        HostMessage::Cancelled(Err(HostError::AuthorizationFailed {
            code: "authorization_failed".to_string(),
            message: "denied".to_string(),
        })),
        HostMessage::Current(Err(HostError::UnexpectedStatus(
            503,
            "unavailable".to_string(),
        ))),
    ] {
        clock.advance(Duration::from_secs(5));
        app.handle_host_message(message);
        assert_eq!(app.liveness(), Liveness::Live);
    }

    app.handle_host_message(HostMessage::Current(Err(HostError::Transport(
        "timeout".to_string(),
    ))));
    clock.advance(Duration::from_secs(5));
    assert_eq!(app.liveness(), Liveness::Stale);
}

#[test]
fn mutations_require_matching_console_session_ownership() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = active_run_app_with_clock(clock);
    app.run.as_mut().unwrap().mission_run_id = "run-independent".to_string();
    assert_eq!(app.liveness(), Liveness::Live);
    assert!(!app.ownership_available());
    assert!(!app.mutations_enabled());

    app.handle_key(key(KeyCode::Char('c')));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert_eq!(
        app.notice.as_deref(),
        Some("Mutation controls disabled while the Host connection is stale or offline")
    );
    app.handle_key(key(KeyCode::Char('q')));
    assert_eq!(app.cancellation, CancellationState::Idle);

    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            mission_id: "mission-1".to_string(),
            mission_run_id: "run-1".to_string(),
            status: "running".to_string(),
            created_at: None,
            started_at: None,
            finished_at: None,
            terminal_classification: None,
        }),
    })));
    assert!(app.ownership_available());
    assert!(app.mutations_enabled());
    app.handle_key(key(KeyCode::Char('c')));
    assert_eq!(app.cancellation, CancellationState::Confirming);
}

#[test]
fn truncated_evidence_sets_flags_and_visible_notice() {
    let clock = Arc::new(ManualClock::new(Instant::now()));
    let mut app = active_run_app_with_clock(clock);
    app.handle_host_message(HostMessage::Activities(Ok(EvidencePage {
        items: activities(),
        truncated: true,
    })));
    assert!(app.activities_truncated);
    assert_eq!(app.activities.len(), 2);
    assert_eq!(
        app.notice.as_deref(),
        Some("Showing the first 2 evidence entries; the Host retains the full timeline")
    );
}
