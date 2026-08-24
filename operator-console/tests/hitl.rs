//! Issue #32 acceptance tests: reserve the Human Decision Request console view.
//!
//! The permanent Human Decisions pane is the console's HITL tab. Its only input
//! is the Runtime Host's public, versioned Mission Run Status from
//! `GET /api/v1/mission-runs/current`; it never carries a Human Decision
//! Request endpoint, permitted-action payload, checkpoint identity, submission
//! command, or resume operation.
//!
//! Covered seams:
//!
//! - Versioned wire status -> client DTO -> app reducer: the empty ->
//!   `awaiting human decision` -> non-awaiting transitions, including clearing
//!   the placeholder after the Host leaves the awaiting status.
//! - Fixed 100x30 `TestBackend` rendering of the empty and awaiting states
//!   against the committed plain-text captures in
//!   `docs/design/operator-console/frames/`.
//! - The view is read-only for everyone: no key produces a `HostCommand` or a
//!   state change, the footer gains no keybindings, and owner and observer
//!   consoles render the identical placeholder.
//!
//! These tests are the Phase 1 (test-first) acceptance spec: they compile
//! against the current public API. The placeholder-copy and capture assertions
//! fail until the #32 view ships; the no-mutation, no-keybinding, and
//! owner/observer parity assertions pin invariants that must hold before and
//! after. Regenerate captures after an intentional layout change with:
//!
//! ```sh
//! UPDATE_FRAMES=1 cargo test --test hitl
//! ```

use std::fs;
use std::path::PathBuf;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use operator_console::app::{
    App, AppState, CancellationState, HostMessage, MIN_HEIGHT, MIN_WIDTH, SessionStateFile,
};
use operator_console::host::{
    ActivationAccepted, ActivationOutcome, ApiVersion, CurrentRun, Health, NarrativeResponse,
    RunRecord,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;

const SESSION_ID: &str = "c0ns01e0-0000-4000-8000-5e5510n5a1d0";

const AWAITING_EXAMPLE: &str = include_str!(
    "../../docs/design/operator-console/contract/v1/mission-runs.current.awaiting-human-decision.response.json"
);

// Self-contained fixtures, mirroring tests/render.rs: the committed captures
// are the reviewable spec, and UPDATE_FRAMES=1 regenerates them.

fn frames_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../docs/design/operator-console/frames")
}

fn test_session_file(name: &str) -> SessionStateFile {
    SessionStateFile::at(
        std::env::temp_dir()
            .join(format!(
                "operator-console-hitl-{name}-{}",
                uuid::Uuid::new_v4()
            ))
            .join("operator-console/session.json"),
    )
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
        "frame mismatch for {name}; run UPDATE_FRAMES=1 cargo test --test hitl to regenerate"
    );
}

/// The seven rows of the bottom-right Human Decisions pane (border included),
/// sliced out of a 100x30 frame so owner/observer output can be compared
/// without pinning unrelated panes.
fn hitl_panel_lines(frame: &str) -> Vec<String> {
    frame
        .lines()
        .skip(20)
        .take(7)
        .map(|line| line.chars().skip(49).collect())
        .collect()
}

fn hitl_panel_text(frame: &str) -> String {
    hitl_panel_lines(frame).join("\n")
}

/// The visible footer key-hint line (row 29 of 30).
fn footer_hint_line(frame: &str) -> String {
    frame.lines().nth(28).unwrap_or_default().to_string()
}

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
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

fn awaiting_record() -> RunRecord {
    RunRecord {
        status: "awaiting_human_decision".to_string(),
        ..running_record()
    }
}

fn connected_app(tag: &str) -> App {
    let mut app =
        App::new_with_session_file("http://127.0.0.1:8787".to_string(), test_session_file(tag));
    app.session.session_id = SESSION_ID.to_string();
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(Health {
        status: "ok".to_string(),
        api_version: ApiVersion { major: 1, minor: 0 },
    })));
    assert_eq!(app.state, AppState::Editing);
    app
}

/// A console that activated the Mission Run itself: ownership is available.
fn owner_run_app(record: RunRecord) -> App {
    let mut app = connected_app("owner");
    app.intent = "Hold the ridge line.\nReport obstacles by grid square.".to_string();
    app.cursor = app.intent.chars().count();
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT));
    app.pin_review_request_id("req-8f3a1c2e-4b5d-4e6f-9a0b-1c2d3e4f5a6b");
    app.handle_key(key(KeyCode::Enter));
    app.take_commands(); // drop the Submit command
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Accepted(
        ActivationAccepted {
            activation_request_id: "req-8f3a1c2e-4b5d-4e6f-9a0b-1c2d3e4f5a6b".to_string(),
            mission_id: record.mission_id.clone(),
            mission_run_id: record.mission_run_id.clone(),
            status: record.status.clone(),
            created_at: record.created_at.clone().unwrap_or_default(),
        },
    ))));
    app.take_commands(); // drop the activation poll fan-out
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(record.clone()),
    })));
    app.handle_host_message(HostMessage::Narrative {
        mission_run_id: record.mission_run_id.clone(),
        result: Ok(
            serde_json::from_str::<NarrativeResponse>(include_str!(
                "../../docs/design/operator-console/contract/v1/mission-run-narrative.none.response.json"
            ))
            .expect("narrative none fixture parses"),
        ),
    });
    assert_eq!(app.state, AppState::Run);
    assert!(app.ownership_available());
    app
}

/// A console observing the same Mission Run without the owning Console
/// Session: ownership is not available.
fn observer_run_app(record: RunRecord) -> App {
    let mut app = connected_app("observer");
    app.run = Some(record);
    app.state = AppState::Run;
    assert!(!app.ownership_available());
    app
}

#[test]
fn awaiting_human_decision_example_round_trips_through_client_dto() {
    let value: serde_json::Value =
        serde_json::from_str(AWAITING_EXAMPLE.trim_end()).expect("committed example parses");
    let current: CurrentRun =
        serde_json::from_value(value.clone()).expect("example decodes through the client DTO");
    // The DTO covers exactly the example's fields - no missing, no extra.
    assert_eq!(
        serde_json::to_value(&current).unwrap(),
        value,
        "awaiting example must round-trip exactly through the DTO"
    );
    let run = current.mission_run.expect("awaiting example carries a run");
    assert_eq!(run.status, "awaiting_human_decision");
    assert!(!run.mission_id.is_empty());
    assert!(!run.mission_run_id.is_empty());
    // Awaiting human decision is non-terminal: no finish time, no classification.
    assert_eq!(run.finished_at, None);
    assert_eq!(run.terminal_classification, None);
}

#[test]
fn versioned_awaiting_status_flows_from_wire_to_reducer_placeholder() {
    let current: CurrentRun =
        serde_json::from_str(AWAITING_EXAMPLE.trim_end()).expect("committed example parses");
    let mut app = connected_app("wire");
    app.state = AppState::Run;
    app.handle_host_message(HostMessage::Current(Ok(current)));
    assert_eq!(
        app.run.as_ref().map(|run| run.status.as_str()),
        Some("awaiting_human_decision")
    );
    let panel = hitl_panel_text(&render(&app, MIN_WIDTH, MIN_HEIGHT));
    assert!(
        panel.contains("AWAITING HUMAN DECISION"),
        "the Host-reported status must be unambiguous in the HITL view\n{panel}"
    );
    assert!(panel.contains("awaiting_human_decision"));
    assert!(
        !panel.contains("require action"),
        "the empty placeholder must clear while a decision is awaited\n{panel}"
    );
    // Reducer display work emitted no host effect.
    assert!(app.take_commands().is_empty());
}

#[test]
fn hitl_placeholder_tracks_empty_awaiting_and_cleared_states() {
    let mut app = owner_run_app(running_record());
    assert!(app.take_commands().is_empty());

    // Empty: the Host reports a non-awaiting status.
    let panel = hitl_panel_text(&render(&app, MIN_WIDTH, MIN_HEIGHT));
    assert!(panel.contains("No Human Decision Requests require action."));
    assert!(!panel.contains("AWAITING HUMAN DECISION"));

    // The Host reports Mission Run Status awaiting human decision.
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(awaiting_record()),
    })));
    assert_eq!(
        app.run.as_ref().map(|run| run.status.as_str()),
        Some("awaiting_human_decision")
    );
    assert!(app.take_commands().is_empty());
    let panel = hitl_panel_text(&render(&app, MIN_WIDTH, MIN_HEIGHT));
    assert!(
        panel.contains("AWAITING HUMAN DECISION"),
        "awaiting human decision must be unambiguous\n{panel}"
    );
    assert!(
        !panel.contains("require action"),
        "the empty placeholder must clear while a decision is awaited\n{panel}"
    );

    // The Host leaves the awaiting status: the placeholder clears.
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(running_record()),
    })));
    assert_eq!(
        app.run.as_ref().map(|run| run.status.as_str()),
        Some("running")
    );
    let panel = hitl_panel_text(&render(&app, MIN_WIDTH, MIN_HEIGHT));
    assert!(panel.contains("No Human Decision Requests require action."));
    assert!(
        !panel.contains("AWAITING HUMAN DECISION"),
        "the awaiting placeholder must clear after exit\n{panel}"
    );
    assert!(app.take_commands().is_empty());
}

#[test]
fn hitl_placeholder_is_empty_without_a_current_run() {
    let mut app = connected_app("no-run");
    app.state = AppState::Run;
    let panel = hitl_panel_text(&render(&app, MIN_WIDTH, MIN_HEIGHT));
    assert!(panel.contains("No Human Decision Requests require action."));
    assert!(panel.contains("no decision controls"));
    assert!(!panel.contains("AWAITING HUMAN DECISION"));
}

#[test]
fn hitl_empty_frame_matches_committed_capture() {
    let app = owner_run_app(running_record());
    assert_frame("hitl-empty-100x30.txt", render(&app, MIN_WIDTH, MIN_HEIGHT));
}

#[test]
fn hitl_awaiting_frame_matches_committed_capture() {
    let app = owner_run_app(awaiting_record());
    let frame = render(&app, MIN_WIDTH, MIN_HEIGHT);
    assert!(frame.contains("awaiting_human_decision"));
    assert_frame("hitl-awaiting-100x30.txt", frame);
}

#[test]
fn hitl_view_binds_no_keys_and_emits_no_host_commands() {
    // Candidate decision affordances. The Run dashboard's own `c`/`q`
    // cancellation flow is covered by app_state.rs and render.rs; the HITL
    // view itself binds no keys at all.
    const CANDIDATE_KEYS: [KeyCode; 19] = [
        KeyCode::Enter,
        KeyCode::Char(' '),
        KeyCode::Char('d'),
        KeyCode::Char('a'),
        KeyCode::Char('y'),
        KeyCode::Char('n'),
        KeyCode::Char('s'),
        KeyCode::Char('1'),
        KeyCode::Char('2'),
        KeyCode::Char('3'),
        KeyCode::Char('4'),
        KeyCode::Char('5'),
        KeyCode::Tab,
        KeyCode::BackTab,
        KeyCode::Up,
        KeyCode::Down,
        KeyCode::Left,
        KeyCode::Right,
        KeyCode::Esc,
    ];
    for record in [running_record(), awaiting_record()] {
        let mut app = owner_run_app(record);
        let _ = render(&app, MIN_WIDTH, MIN_HEIGHT); // drawing reads, never enqueues
        for code in CANDIDATE_KEYS {
            app.handle_key(key(code));
        }
        assert_eq!(app.state, AppState::Run);
        assert_eq!(app.cancellation, CancellationState::Idle);
        assert!(app.inspector.is_none());
        assert!(app.selected_activity.is_none());
        assert!(
            app.take_commands().is_empty(),
            "the status-only HITL view must not emit HostCommand values"
        );
    }
}

#[test]
fn hitl_footer_gains_no_keybindings_when_awaiting() {
    let empty = render(&owner_run_app(running_record()), MIN_WIDTH, MIN_HEIGHT);
    let awaiting = render(&owner_run_app(awaiting_record()), MIN_WIDTH, MIN_HEIGHT);
    assert_eq!(
        footer_hint_line(&empty),
        footer_hint_line(&awaiting),
        "the awaiting state must not add interaction affordances"
    );
    assert!(
        !footer_hint_line(&awaiting)
            .to_lowercase()
            .contains("decision")
    );
}

#[test]
fn owner_and_observer_render_the_identical_hitl_placeholder() {
    let owner = owner_run_app(awaiting_record());
    let observer = observer_run_app(awaiting_record());
    assert!(owner.ownership_available());
    assert!(!observer.ownership_available());
    let owner_panel = hitl_panel_lines(&render(&owner, MIN_WIDTH, MIN_HEIGHT));
    let observer_panel = hitl_panel_lines(&render(&observer, MIN_WIDTH, MIN_HEIGHT));
    assert_eq!(
        owner_panel, observer_panel,
        "owners and observers must receive the same read-only presentation"
    );
    let text = owner_panel.join("\n");
    assert!(text.contains("AWAITING HUMAN DECISION"));
    assert!(text.contains("no decision controls"));
    assert!(!text.contains("require action"));
}
