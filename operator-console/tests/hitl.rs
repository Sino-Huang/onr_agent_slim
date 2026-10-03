//! Issue #32: the status-only Human Decisions pane on the Overview tab.
//!
//! Its only input is the Runtime Host's public, versioned Mission Run Status
//! from `GET /api/v1/mission-runs/current`; it carries no Human Decision
//! Request endpoint, permitted-action payload, checkpoint identity,
//! submission command, or resume operation. The view is read-only for
//! everyone: no key produces a `HostCommand`, the footer gains no keybindings,
//! and owner and observer consoles render the identical placeholder.
//!
//! Regenerate captures after an intentional layout change with
//! `UPDATE_FRAMES=1 cargo test --test hitl`.

mod common;

use std::fs;
use std::path::PathBuf;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::{App, AppState, CancellationState, MIN_HEIGHT, MIN_WIDTH};
use operator_console::host::{CurrentRun, HostMessage, RunRecord};
use ratatui::Terminal;
use ratatui::backend::TestBackend;

const AWAITING_EXAMPLE: &str = include_str!(
    "../../docs/design/operator-console/contract/v1/mission-runs.current.awaiting-human-decision.response.json"
);

fn render(app: &mut App) -> Vec<String> {
    let mut terminal = Terminal::new(TestBackend::new(MIN_WIDTH, MIN_HEIGHT)).unwrap();
    terminal
        .draw(|frame| operator_console::ui::draw(frame, app))
        .unwrap();
    let buffer = terminal.backend().buffer();
    (0..MIN_HEIGHT)
        .map(|y| {
            (0..MIN_WIDTH)
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect::<String>()
                .trim_end()
                .to_string()
        })
        .collect()
}

fn assert_frame(name: &str, lines: &[String]) {
    let actual = format!("{}\n", lines.join("\n"));
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../docs/design/operator-console/frames")
        .join(name);
    if std::env::var_os("UPDATE_FRAMES").is_some() {
        fs::write(&path, &actual).unwrap();
        return;
    }
    let expected = fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("missing fixture frame {}: {error}", path.display()));
    assert_eq!(
        actual.lines().collect::<Vec<_>>(),
        expected.lines().map(str::trim_end).collect::<Vec<_>>(),
        "frame mismatch for {name}; run UPDATE_FRAMES=1 cargo test --test hitl to regenerate"
    );
}

/// Rows of the Human Decisions pane, located by its title.
fn hitl_panel(lines: &[String]) -> Vec<String> {
    let (row, column) = lines
        .iter()
        .enumerate()
        .find_map(|(row, line)| {
            let byte = line.find("┌ Human Decisions")?;
            Some((row, line[..byte].chars().count()))
        })
        .expect("the Overview shows the Human Decisions pane");
    lines[row..]
        .iter()
        .take_while(|line| line.chars().nth(column).is_some_and(|c| c != '─'))
        .map(|line| line.chars().skip(column).collect())
        .collect()
}

/// The pane's text with borders stripped and wrapped rows re-joined.
fn hitl_text(lines: &[String]) -> String {
    hitl_panel(lines)
        .iter()
        .skip(1)
        .map(|line| line.trim_matches(|c| c == '│' || c == ' ').to_string())
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

fn awaiting_run() -> RunRecord {
    let mut run = running_run();
    run.status = "awaiting_human_decision".to_string();
    run
}

fn owner_app(run: RunRecord) -> App {
    let (mut app, _clock) = run_app("hitl-owner");
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(run),
    })));
    app.take_commands();
    assert!(app.ownership_available());
    app
}

fn observer_app(run: RunRecord) -> App {
    let (mut app, _clock) = ready_launch_app("hitl-observer");
    app.state = AppState::Run;
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(run),
    })));
    app.take_commands();
    assert!(!app.ownership_available());
    app
}

#[test]
fn versioned_awaiting_status_flows_from_wire_to_the_placeholder() {
    let current: CurrentRun = serde_json::from_str(AWAITING_EXAMPLE.trim_end()).unwrap();
    let run = current.mission_run.clone().unwrap();
    assert_eq!(run.status, "awaiting_human_decision");
    assert!(!run.is_terminal());
    let (mut app, _clock) = ready_launch_app("hitl-wire");
    app.state = AppState::Run;
    app.handle_host_message(HostMessage::Current(Ok(current)));
    let text = hitl_text(&render(&mut app));
    assert!(text.contains("AWAITING HUMAN DECISION"), "{text}");
    assert!(text.contains("awaiting_human_decision"));
    assert!(!text.contains("require action"));
}

#[test]
fn placeholder_tracks_empty_awaiting_and_cleared_states() {
    let mut app = owner_app(running_run());
    let text = hitl_text(&render(&mut app));
    assert!(text.contains("No Human Decision Requests require action."));
    assert!(!text.contains("AWAITING"));

    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(awaiting_run()),
    })));
    let text = hitl_text(&render(&mut app));
    assert!(text.contains("AWAITING HUMAN DECISION"));
    assert!(!text.contains("require action"));

    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(running_run()),
    })));
    let text = hitl_text(&render(&mut app));
    assert!(text.contains("No Human Decision Requests require action."));
    assert!(!text.contains("AWAITING"));
    assert!(
        app.take_commands().is_empty(),
        "display work emits no effects"
    );
}

#[test]
fn placeholder_is_empty_without_a_current_run() {
    let (mut app, _clock) = ready_launch_app("hitl-no-run");
    app.state = AppState::Run;
    let text = hitl_text(&render(&mut app));
    assert!(text.contains("No Human Decision Requests require action."));
    assert!(text.contains("no decision controls"));
}

#[test]
fn empty_and_awaiting_frames_match_committed_captures() {
    assert_frame(
        "hitl-empty-100x30.txt",
        &render(&mut owner_app(running_run())),
    );
    assert_frame(
        "hitl-awaiting-100x30.txt",
        &render(&mut owner_app(awaiting_run())),
    );
}

#[test]
fn the_view_binds_no_decision_keys_and_emits_no_host_commands() {
    const CANDIDATE_KEYS: [KeyCode; 12] = [
        KeyCode::Enter,
        KeyCode::Char(' '),
        KeyCode::Char('d'),
        KeyCode::Char('a'),
        KeyCode::Char('y'),
        KeyCode::Char('n'),
        KeyCode::Char('s'),
        KeyCode::Up,
        KeyCode::Down,
        KeyCode::Left,
        KeyCode::Right,
        KeyCode::Esc,
    ];
    for run in [running_run(), awaiting_run()] {
        let mut app = owner_app(run);
        let _ = render(&mut app);
        for code in CANDIDATE_KEYS {
            app.handle_key(key(code));
        }
        assert_eq!(app.state, AppState::Run);
        assert_eq!(app.cancellation, CancellationState::Idle);
        assert!(app.view.inspector.is_none());
        assert!(app.take_commands().is_empty());
    }
}

#[test]
fn footer_gains_no_keybindings_when_awaiting() {
    let footer = |lines: Vec<String>| lines[lines.len() - 2].clone();
    let empty = footer(render(&mut owner_app(running_run())));
    let awaiting = footer(render(&mut owner_app(awaiting_run())));
    assert_eq!(empty, awaiting);
    assert!(!awaiting.to_lowercase().contains("decision"));
}

#[test]
fn owner_and_observer_render_the_identical_placeholder() {
    let owner = hitl_panel(&render(&mut owner_app(awaiting_run())));
    let observer = hitl_panel(&render(&mut observer_app(awaiting_run())));
    assert_eq!(owner, observer);
    let text = owner.join(" ");
    assert!(text.contains("AWAITING HUMAN DECISION"));
}
