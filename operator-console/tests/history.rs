//! Run history browser (#76 U10): F3 lists the Host's runs, Enter opens one
//! read-only, the owner session and the current run stay untouched, and Esc
//! returns.

mod common;

use std::sync::Arc;

use common::*;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use operator_console::app::{App, AttentionKind, CancellationState, SessionStateFile};
use operator_console::host::{
    CurrentRun, HostCommand, HostError, HostMessage, OperatorSection, RunRecord,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;

fn screen(app: &mut App, width: u16, height: u16) -> String {
    app.handle_resize(width, height);
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| operator_console::ui::draw(frame, app))
        .unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| {
            (0..width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// An owned running run on a v1.5 Host, hydrated once, with its session file.
fn owned_run(name: &str) -> (App, Arc<ManualClock>, SessionStateFile) {
    let file = state_file(name);
    let (mut app, clock) = run_app_with_file(name, file.clone());
    app.health = Some(health_v1_5());
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(running_run()),
    })));
    app.request_poll();
    let commands = app.take_commands();
    for section in sections(&commands) {
        let id = section_request_id(&commands, section);
        app.handle_host_message(section_reply(section, id, None, |_| {}));
    }
    app.take_commands();
    (app, clock, file)
}

/// F3, select the succeeded historical run, Enter; returns the commands.
fn open_historical(app: &mut App) -> Vec<HostCommand> {
    open_history(app);
    select_history_row(app, HISTORICAL_RUN_ID);
    app.handle_key(key(KeyCode::Enter));
    app.take_commands()
}

fn operator_views(commands: &[HostCommand], run_id: &str) -> Vec<OperatorSection> {
    commands
        .iter()
        .filter_map(|command| match command {
            HostCommand::FetchOperatorView {
                mission_run_id,
                section,
                ..
            } if mission_run_id == run_id => Some(*section),
            _ => None,
        })
        .collect()
}

fn polls_current(commands: &[HostCommand]) -> bool {
    commands
        .iter()
        .any(|command| matches!(command, HostCommand::PollCurrent { .. }))
}

#[test]
fn f3_lists_the_hosts_runs_newest_first_with_their_columns() {
    let (mut app, _clock, _file) = owned_run("history-list");
    open_history(&mut app);
    assert!(app.history.open);
    assert_eq!(app.history.rows.len(), 4);
    assert_eq!(app.history.selected.as_deref(), Some(RUN_ID));
    let text = screen(&mut app, 160, 45);
    assert!(text.contains("Run history (F3)"), "{text}");
    assert!(text.contains("Started (UTC)"));
    assert!(text.contains("2026-10-03 13:22 mission1-harbor"), "{text}");
    assert!(text.contains("✔ succeeded"));
    assert!(text.contains("✖ failed · stack_failed"));
    assert!(text.contains("5:12"), "succeeded run's wall time:\n{text}");
    assert!(text.contains("● current"));
    assert!(text.contains("✖ no root"));
    assert!(text.contains("mission1-harbor +AirSim"));
    // The owned run's detail line carries the toggles the columns omit.
    assert!(text.contains("coordinator_driven · limit 600.0 s · Run Root on disk"));
    // Never the Mission Intent.
    assert!(!text.contains("Survey"));
    // Esc closes the overlay; the Run screen is untouched.
    app.handle_key(key(KeyCode::Esc));
    assert!(!app.history.open);
    assert!(!app.viewing_history());
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, RUN_ID);
}

#[test]
fn viewing_history_never_touches_the_owner_session_or_owned_run() {
    let (mut app, _clock, file) = owned_run("history-owner");
    let saved = std::fs::read(file.path()).unwrap();
    let activation = app.activation.clone();

    let commands = open_historical(&mut app);
    assert!(app.viewing_history());
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, HISTORICAL_RUN_ID);
    assert_eq!(app.current_run().unwrap().mission_run_id, RUN_ID);
    // Every section of the historical run is read through the per-run routes.
    assert_eq!(
        operator_views(&commands, HISTORICAL_RUN_ID).len(),
        OperatorSection::ALL.len()
    );
    answer_sections_for(
        &mut app,
        &commands,
        HISTORICAL_RUN_ID,
        "succeeded",
        |_, _| {},
    );
    assert_eq!(app.activation, activation);
    assert!(
        !app.ownership_available(),
        "a historical run is never owned"
    );
    assert_eq!(std::fs::read(file.path()).unwrap(), saved);

    app.handle_key(key(KeyCode::Esc));
    assert!(!app.viewing_history());
    assert_eq!(app.logical_state_name(), "Run");
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, RUN_ID);
    assert_eq!(app.activation, activation);
    assert!(app.ownership_available());
    assert_eq!(std::fs::read(file.path()).unwrap(), saved);
    // The current run's view survived; returning polls it as usual.
    assert!(app.view.overview.is_some());
    let commands = app.take_commands();
    assert!(polls_current(&commands));
    assert!(operator_views(&commands, HISTORICAL_RUN_ID).is_empty());
}

#[test]
fn the_current_run_keeps_being_polled_and_alerting_while_history_is_viewed() {
    let (mut app, clock, _file) = owned_run("history-polls");
    app.take_attention_events();
    let commands = open_historical(&mut app);
    // The parked current run is polled from the first tick on.
    assert!(polls_current(&commands));
    assert_eq!(
        operator_views(&commands, RUN_ID),
        [OperatorSection::Overview, OperatorSection::Stack]
    );
    answer_sections_for(
        &mut app,
        &commands,
        HISTORICAL_RUN_ID,
        "succeeded",
        |_, _| {},
    );
    for section in [OperatorSection::Overview, OperatorSection::Stack] {
        let id = section_request_id(
            &commands
                .iter()
                .filter(|command| {
                    matches!(command, HostCommand::FetchOperatorView { mission_run_id, .. } if mission_run_id == RUN_ID)
                })
                .cloned()
                .collect::<Vec<_>>(),
            section,
        );
        app.handle_host_message(section_reply(section, id, None, |_| {}));
    }
    // A terminal historical run reaches no attention signal.
    assert!(app.take_attention_events().is_empty());

    clock.advance(std::time::Duration::from_secs(1));
    app.request_poll();
    let commands = app.take_commands();
    assert!(polls_current(&commands), "{commands:?}");
    assert!(!operator_views(&commands, RUN_ID).is_empty());

    // The current run fails while the operator reads history: the alert
    // fires once, the display stays on the historical run.
    let failed = RunRecord {
        status: "failed".to_string(),
        finished_at: Some("2026-08-24T12:04:00Z".to_string()),
        terminal_classification: Some("worker_failed".to_string()),
        ..running_run()
    };
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(failed),
    })));
    let events = app.take_attention_events();
    assert_eq!(events.len(), 1, "{events:?}");
    assert_eq!(events[0].kind, AttentionKind::Terminal);
    assert_eq!(events[0].mission_run_id, RUN_ID);
    assert!(
        app.window_title().contains("worker_failed"),
        "{}",
        app.window_title()
    );
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, HISTORICAL_RUN_ID);
    assert_eq!(app.current_run().unwrap().status, "failed");

    // Esc lands on the current run as it is now.
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.run.as_ref().unwrap().status, "failed");
    assert!(app.failure_open());
}

#[test]
fn a_missing_run_root_shows_an_explicit_message() {
    let (mut app, _clock, _file) = owned_run("history-missing-root");
    open_history(&mut app);
    select_history_row(&mut app, MISSING_ROOT_RUN_ID);
    // Reaching the last loaded row asks for the next older page.
    assert_eq!(
        app.take_commands(),
        [HostCommand::FetchRunHistory {
            before: Some("run-1".to_string()),
            limit: 50,
        }]
    );
    app.handle_key(key(KeyCode::Enter));
    assert!(app.take_commands().is_empty(), "nothing to read");
    assert!(!app.viewing_history());
    assert!(app.history.open);
    let message = app.history.message.clone().unwrap();
    assert!(message.contains("Run Root of run-1 is missing on this Host"));
    for (width, height) in [(100, 30), (160, 45)] {
        let text = screen(&mut app, width, height);
        assert!(text.contains("✖ Run Root of run-1 is missing"), "{text}");
        assert!(text.contains("Run Root missing"), "detail line:\n{text}");
    }

    // A Run Root removed after listing: the Host's answer is explicit too.
    app.handle_key(key(KeyCode::Esc));
    let commands = open_historical(&mut app);
    let request_id = commands
        .iter()
        .find_map(|command| match command {
            HostCommand::FetchOperatorView {
                mission_run_id,
                section: OperatorSection::Overview,
                request_id,
                ..
            } if mission_run_id == HISTORICAL_RUN_ID => Some(*request_id),
            _ => None,
        })
        .unwrap();
    app.handle_host_message(HostMessage::OperatorView {
        mission_run_id: HISTORICAL_RUN_ID.to_string(),
        section: OperatorSection::Overview,
        request_id,
        result: Err(HostError::NotFound {
            code: "run_root_unavailable".to_string(),
            message:
                "Run Root of this Mission Run is missing on this Host; its evidence cannot be read"
                    .to_string(),
        }),
    });
    let text = screen(&mut app, 160, 45);
    assert!(text.contains("HISTORICAL  Run Root missing"), "{text}");
    assert!(text.contains("✖ Run Root unavailable: Run Root of this Mission Run is missing"));
}

#[test]
fn a_historical_run_refuses_every_mutating_key() {
    let (mut app, _clock, file) = owned_run("history-read-only");
    let saved = std::fs::read(file.path()).unwrap();
    let commands = open_historical(&mut app);
    answer_sections_for(
        &mut app,
        &commands,
        HISTORICAL_RUN_ID,
        "succeeded",
        |section, page| {
            if section == OperatorSection::Overview {
                page["overview"]["receipt"] = receipt_example(|_| {});
            }
        },
    );
    let text = screen(&mut app, 160, 45);
    assert!(
        text.contains("HISTORICAL read-only · Esc back to current run · 1-7/Tab tabs"),
        "{text}"
    );
    assert!(
        text.contains("Export:    not exported · read-only in history"),
        "{text}"
    );
    for code in ['c', 'q', 'e', 'x'] {
        app.handle_key(key(KeyCode::Char(code)));
        assert!(app.hint.as_deref().unwrap().contains("read-only"), "{code}");
        let commands = app.take_commands();
        assert!(
            !commands.iter().any(|command| matches!(
                command,
                HostCommand::Cancel { .. } | HostCommand::ExportReceipt { .. }
            )),
            "{code}: {commands:?}"
        );
        assert!(app.viewing_history(), "{code} stays in history");
        assert_eq!(app.cancellation, CancellationState::Idle);
        assert!(!app.should_quit());
        assert_eq!(app.take_clean_exit_action(), None);
    }
    assert_eq!(std::fs::read(file.path()).unwrap(), saved);
    // Tabs stay browsable.
    app.handle_key(key(KeyCode::Char('6')));
    assert!(screen(&mut app, 160, 45).contains("HISTORICAL"));
    // Ctrl+C leaves the history first, then acts on the current run.
    app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
    assert!(!app.viewing_history());
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, RUN_ID);
}

#[test]
fn opening_the_current_run_from_history_shows_it_live() {
    let (mut app, _clock, _file) = owned_run("history-current-row");
    open_history(&mut app);
    select_history_row(&mut app, RUN_ID);
    app.handle_key(key(KeyCode::Enter));
    assert!(!app.history.open);
    assert!(!app.viewing_history());
    assert_eq!(
        app.hint.as_deref(),
        Some("run-fixture-001 is the current run")
    );
}

#[test]
fn esc_returns_to_launch_when_history_was_opened_there() {
    let (mut app, _clock) = ready_launch_app("history-launch");
    app.health = Some(health_v1_5());
    let commands = open_historical(&mut app);
    assert_eq!(app.logical_state_name(), "Run");
    assert!(app.viewing_history());
    // Nothing is current behind it: no `/current` poll from Launch.
    assert!(!polls_current(&commands));
    assert!(!operator_views(&commands, HISTORICAL_RUN_ID).is_empty());
    answer_sections_for(
        &mut app,
        &commands,
        HISTORICAL_RUN_ID,
        "succeeded",
        |_, _| {},
    );
    assert!(screen(&mut app, 100, 30).contains("Esc back to Launch"));
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.logical_state_name(), "Launch");
    assert!(app.run.is_none());
}

#[test]
fn another_history_row_replaces_the_historical_run_but_esc_still_returns() {
    let (mut app, _clock, _file) = owned_run("history-replace");
    let commands = open_historical(&mut app);
    answer_sections_for(
        &mut app,
        &commands,
        HISTORICAL_RUN_ID,
        "succeeded",
        |_, _| {},
    );
    open_history(&mut app);
    let failed = "run-dd8e174d-cc1f-4016-8ebf-84ac13e8d495";
    select_history_row(&mut app, failed);
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, failed);
    assert!(!operator_views(&app.take_commands(), failed).is_empty());
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.run.as_ref().unwrap().mission_run_id, RUN_ID);
    assert!(!app.viewing_history());
}

#[test]
fn filters_cycle_over_loaded_rows_and_scrolling_past_the_end_loads_older_pages() {
    let (mut app, _clock, _file) = owned_run("history-paging");
    open_history(&mut app);
    // Status: all -> succeeded.
    app.handle_key(key(KeyCode::Char('s')));
    let visible: Vec<_> = app
        .history
        .visible()
        .iter()
        .map(|row| row.mission_run.mission_run_id.clone())
        .collect();
    assert_eq!(visible, [HISTORICAL_RUN_ID]);
    assert_eq!(app.history.selected.as_deref(), Some(HISTORICAL_RUN_ID));
    // Only one row passes and it is selected: the next older page is wanted.
    assert_eq!(
        app.take_commands(),
        [HostCommand::FetchRunHistory {
            before: Some("run-1".to_string()),
            limit: 50,
        }]
    );
    // A stale newest-page answer is ignored while the older page is in flight.
    app.handle_host_message(HostMessage::RunHistory {
        before: None,
        result: Ok(history_page()),
    });
    assert_eq!(app.history.rows.len(), 4);
    let mut older = history_page();
    older.mission_runs.truncate(2);
    older.mission_runs[0].mission_run.mission_run_id = "run-older-succeeded".to_string();
    older.mission_runs[0].mission_run.status = "succeeded".to_string();
    older.mission_runs[0].current = false;
    older.mission_runs[1].mission_run.mission_run_id = HISTORICAL_RUN_ID.to_string();
    older.next_before = None;
    app.handle_host_message(HostMessage::RunHistory {
        before: Some("run-1".to_string()),
        result: Ok(older),
    });
    assert_eq!(app.history.rows.len(), 5, "deduplicated by run id");
    assert!(app.history.complete);
    app.handle_key(key(KeyCode::End));
    assert_eq!(app.history.selected.as_deref(), Some("run-older-succeeded"));
    assert!(app.take_commands().is_empty(), "the oldest page arrived");
    assert!(screen(&mut app, 100, 30).contains("Status succeeded (s)"));
    // Presets cycle over the loaded rows, then back to all.
    app.handle_key(key(KeyCode::Char('s')));
    app.handle_key(key(KeyCode::Char('s')));
    app.handle_key(key(KeyCode::Char('s')));
    app.handle_key(key(KeyCode::Char('s')));
    assert_eq!(app.history.status_filter.label(), "all");
    app.handle_key(key(KeyCode::Char('p')));
    assert_eq!(
        app.history.preset_filter.as_deref(),
        Some("mission1-harbor")
    );
    assert_eq!(
        app.history.visible().len(),
        4,
        "the legacy row has no preset"
    );
    app.handle_key(key(KeyCode::Char('p')));
    assert_eq!(app.history.preset_filter, None);
}

#[test]
fn an_older_host_says_the_history_needs_api_v1_5() {
    let (mut app, _clock) = ready_launch_app("history-old-host");
    app.handle_key(key(KeyCode::F(3)));
    assert!(app.take_commands().is_empty());
    let error = app.history.error.clone().unwrap();
    assert!(
        error.contains("needs Runtime Host API v1.5; this Host reports v1.3"),
        "{error}"
    );
    assert!(screen(&mut app, 100, 30).contains("needs Runtime Host API v1.5"));
    // Typing in the intent editor is unaffected once the overlay closes.
    app.handle_key(key(KeyCode::Esc));
    assert!(!app.history.open);
}

fn view_request_id(commands: &[HostCommand], run_id: &str, wanted: OperatorSection) -> u64 {
    commands
        .iter()
        .find_map(|command| match command {
            HostCommand::FetchOperatorView {
                mission_run_id,
                section,
                request_id,
                ..
            } if mission_run_id == run_id && *section == wanted => Some(*request_id),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no {wanted:?} read of {run_id}: {commands:?}"))
}

fn fail_view(app: &mut App, commands: &[HostCommand], section: OperatorSection, error: HostError) {
    app.handle_host_message(HostMessage::OperatorView {
        mission_run_id: HISTORICAL_RUN_ID.to_string(),
        section,
        request_id: view_request_id(commands, HISTORICAL_RUN_ID, section),
        result: Err(error),
    });
}

fn timeout() -> HostError {
    HostError::Timeout("timeout: global".to_string())
}

/// The Host rebuilds a run it has not served since it started from disk
/// (ADR 0015); a large run outlasts the console's request limit. That is
/// loading, not a failed poll, and nothing of it outlives Esc.
#[test]
fn a_cold_historical_run_shows_loading_and_its_notices_stay_with_it() {
    let (mut app, clock, _file) = owned_run("history-cold-open");
    // The current run already carries a notice of its own.
    app.handle_host_message(HostMessage::Current(Err(HostError::Transport(
        "down".to_string(),
    ))));
    let current_notice = app.notice.clone().unwrap();
    assert!(
        current_notice.contains("Host poll failed"),
        "{current_notice}"
    );

    let commands = open_historical(&mut app);
    assert_eq!(app.notice, None, "the current run's notice stays with it");
    let text = screen(&mut app, 160, 45);
    assert!(text.contains("HISTORICAL  loading from disk 0 s"), "{text}");
    assert!(
        text.contains("Loading from disk · 0 s · first open rebuilds the Host's view"),
        "{text}"
    );

    // The first read outlasts the request limit while the Host rebuilds.
    clock.advance(std::time::Duration::from_secs(5));
    fail_view(&mut app, &commands, OperatorSection::Overview, timeout());
    assert_eq!(app.notice, None);
    for (width, height) in [(100, 30), (160, 45)] {
        let text = screen(&mut app, width, height);
        assert!(text.contains("HISTORICAL  loading from disk 5 s"), "{text}");
        assert!(
            text.contains(
                "Loading from disk · 5 s · first open rebuilds the Host's view · 1 read timed out, retrying"
            ),
            "{text}"
        );
        assert!(!text.contains("poll failed"), "{text}");
    }

    // A genuine failure is still reported while loading.
    fail_view(
        &mut app,
        &commands,
        OperatorSection::Agents,
        HostError::UnexpectedStatus(500, "boom".to_string()),
    );
    let historical_notice = app.notice.clone().unwrap();
    assert!(
        historical_notice.contains("Host agents poll failed (unexpected status 500"),
        "{historical_notice}"
    );
    assert!(screen(&mut app, 160, 45).contains("agents poll failed"));

    // The timed-out read is retried.
    clock.advance(std::time::Duration::from_secs(1));
    app.request_poll();
    let retry = app.take_commands();
    assert!(
        operator_views(&retry, HISTORICAL_RUN_ID).contains(&OperatorSection::Overview),
        "{retry:?}"
    );

    // Esc drops the historical view's notice and loading state and brings
    // back the current run's notice.
    app.handle_key(key(KeyCode::Esc));
    assert!(!app.viewing_history());
    assert_eq!(app.notice.as_deref(), Some(current_notice.as_str()));
    let text = screen(&mut app, 160, 45);
    assert!(!text.contains("agents poll failed"), "{text}");
    assert!(!text.contains("loading from disk"), "{text}");
    assert!(!text.contains("HISTORICAL"), "{text}");
}

#[test]
fn the_first_overview_page_ends_loading_and_a_later_timeout_is_a_failed_poll() {
    let (mut app, _clock, _file) = owned_run("history-cold-loaded");
    let commands = open_historical(&mut app);
    let only = |wanted: OperatorSection| -> Vec<HostCommand> {
        commands
            .iter()
            .filter(|command| {
                matches!(
                    command,
                    HostCommand::FetchOperatorView {
                        mission_run_id,
                        section,
                        ..
                    } if mission_run_id == HISTORICAL_RUN_ID && *section == wanted
                )
            })
            .cloned()
            .collect()
    };
    // Sections read while the Host rebuilds can answer first; the run is
    // still loading until its Overview arrives.
    answer_sections_for(
        &mut app,
        &only(OperatorSection::Stack),
        HISTORICAL_RUN_ID,
        "succeeded",
        |_, _| {},
    );
    assert!(app.historical_loading().is_some());
    answer_sections_for(
        &mut app,
        &only(OperatorSection::Overview),
        HISTORICAL_RUN_ID,
        "succeeded",
        |_, _| {},
    );
    assert_eq!(app.historical_loading(), None);
    let text = screen(&mut app, 160, 45);
    assert!(!text.contains("loading from disk"), "{text}");
    assert!(!text.contains("Loading from disk"), "{text}");

    fail_view(&mut app, &commands, OperatorSection::Agents, timeout());
    let notice = app.notice.clone().unwrap();
    assert!(
        notice.contains("Host agents poll failed (request timed out: timeout: global)"),
        "{notice}"
    );
}

/// A timeout of the parked current run's read is that run's failed poll,
/// never historical loading.
#[test]
fn a_parked_current_run_timeout_is_its_own_failed_poll() {
    let (mut app, _clock, _file) = owned_run("history-cold-parked");
    let commands = open_historical(&mut app);
    app.handle_host_message(HostMessage::OperatorView {
        mission_run_id: RUN_ID.to_string(),
        section: OperatorSection::Overview,
        request_id: view_request_id(&commands, RUN_ID, OperatorSection::Overview),
        result: Err(timeout()),
    });
    assert_eq!(app.notice, None, "not shown on the historical view");
    assert_eq!(
        app.historical_loading().map(|(_, timed_out)| timed_out),
        Some(0)
    );
    app.handle_key(key(KeyCode::Esc));
    let notice = app.notice.clone().unwrap();
    assert!(
        notice.contains("Host overview poll failed (request timed out"),
        "{notice}"
    );
}
