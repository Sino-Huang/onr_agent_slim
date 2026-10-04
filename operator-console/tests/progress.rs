//! Consumer-visible hierarchy, cursor replay, navigation and search contracts.
mod common;

use std::fs;
use std::path::PathBuf;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use operator_console::app::progress::ProgressView;
use operator_console::app::{CancellationState, RunTab};
use operator_console::host::{OperatorProgress, ProgressNode};
use operator_console::ui::progress::draw_progress;
use operator_console::ui::theme::{Importance, Theme};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::json;

fn fixture() -> OperatorProgress {
    let value: serde_json::Value = serde_json::from_str(include_str!(
        "../../docs/design/operator-console/contract/v1.2/mission-run-operator-progress.response.json"
    )).unwrap();
    serde_json::from_value(value["progress"].clone()).unwrap()
}

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn record(sequence: u64, parent: &str, importance: &str) -> ProgressNode {
    let mut node = fixture().nodes[1].clone();
    node.node_id = format!("log:{sequence}");
    node.parent_id = Some(parent.to_string());
    node.importance = importance.to_string();
    node.title = format!("FSM transition {sequence}");
    node.details = json!({"sequence": sequence, "target": "patrol.leg_3"});
    node
}

fn delta(nodes: Vec<ProgressNode>) -> OperatorProgress {
    let mut page = fixture();
    page.nodes = nodes;
    page
}

fn render(view: &mut ProgressView, width: u16, height: u16, theme: Theme) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| {
            let area = frame.area();
            draw_progress(frame, area, view, theme, common::UNIX_ORIGIN);
        })
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

#[test]
fn contract_replay_retains_narrative_summary_counts_and_full_record_details() {
    let mut view = ProgressView::default();
    view.ingest(fixture());
    view.ingest(fixture());
    assert_eq!(view.nodes.len(), 4);
    assert_eq!(view.selected_id(), Some("log:388"));
    let screen = render(&mut view, 160, 45, Theme::new(false));
    assert!(screen.contains("Legs 1-2 complete; ship 2's report was altered"));
    assert!(screen.contains("(31 records)"));
    assert!(screen.contains("\"from_state\": \"patrol.leg_2\""));
    assert!(!screen.contains("Context Coordination heartbeat"));
    view.select_node("summary:7");
    let screen = render(&mut view, 160, 45, Theme::new(false));
    assert!(screen.contains("Replan after report alteration on ship 2;"));
    assert!(screen.contains("patrol.leg_3."));
    assert!(screen.contains("AI · non-authoritative"));
}

#[test]
fn summary_reparents_in_place_without_duplicate_records_and_keeps_selection() {
    let mut view = ProgressView::default();
    view.ingest(delta(vec![record(400, "live", "notable")]));
    view.handle_key(key(KeyCode::Up));
    view.select_node("log:400");
    assert!(!view.follow_newest);
    assert_eq!(
        view.visible_path("log:400").unwrap(),
        ["root", "live", "log:400"]
    );
    let mut summary = fixture().nodes[0].clone();
    summary.node_id = "summary:8".to_string();
    summary.child_count = 1;
    summary.title = "Summary eight".to_string();
    view.ingest(delta(vec![
        summary.clone(),
        record(400, "summary:8", "notable"),
    ]));
    view.ingest(delta(vec![summary, record(400, "summary:8", "notable")]));
    assert_eq!(view.selected_id(), Some("log:400"));
    assert_eq!(
        view.visible_path("log:400").unwrap(),
        ["root", "summary:8", "log:400"]
    );
    assert!(
        view.tree_state
            .opened()
            .contains(&vec!["root".to_string(), "summary:8".to_string()])
    );
    let screen = render(&mut view, 160, 45, Theme::new(false));
    // One row in the tree and one heading in selected evidence, never a third
    // duplicate under the old live parent.
    assert_eq!(screen.matches("FSM transition 400").count(), 2);
    assert_eq!(
        view.nodes
            .values()
            .filter(|node| node.node_id == "log:400")
            .count(),
        1
    );
}

#[test]
fn collapsed_summary_and_selected_id_survive_cursor_updates() {
    let mut view = ProgressView::default();
    view.ingest(fixture());
    view.handle_key(key(KeyCode::Home));
    view.select_node("summary:7");
    view.handle_key(key(KeyCode::Left));
    let summary_path = vec!["root".to_string(), "summary:7".to_string()];
    assert!(!view.tree_state.opened().contains(&summary_path));
    let mut update = fixture().nodes[0].clone();
    update.text = Some("Final summary includes the terminal result.".to_string());
    view.ingest(delta(vec![update]));
    assert_eq!(view.selected_id(), Some("summary:7"));
    assert!(!view.tree_state.opened().contains(&summary_path));
    assert!(
        render(&mut view, 160, 45, Theme::new(false))
            .contains("Final summary includes the terminal result.")
    );
}

#[test]
fn follow_tracks_latest_sequence_navigation_pauses_and_f_resumes() {
    let mut view = ProgressView::default();
    view.ingest(delta(vec![
        record(400, "live", "routine"),
        record(399, "live", "routine"),
    ]));
    assert_eq!(view.selected_id(), Some("log:400"));
    view.handle_key(key(KeyCode::Up));
    let selected = view.selected_id().unwrap().to_string();
    view.ingest(delta(vec![record(401, "live", "notable")]));
    assert_eq!(view.selected_id(), Some(selected.as_str()));
    assert!(!view.follow_newest);
    view.handle_key(key(KeyCode::Char('f')));
    assert_eq!(view.selected_id(), Some("log:401"));
    assert!(view.follow_newest);
    // Replay of an older record does not steal follow selection.
    let mut old = record(399, "live", "routine");
    old.title = "Updated old record".to_string();
    view.ingest(delta(vec![old]));
    assert_eq!(view.selected_id(), Some("log:401"));
}

#[test]
fn debug_filter_cycles_and_retains_hidden_ancestors_with_visible_children() {
    let mut view = ProgressView::default();
    view.ingest(fixture());
    assert_eq!(view.minimum, Importance::Routine);
    assert!(view.visible_path("log:389").is_none());
    view.handle_key(key(KeyCode::Char('i')));
    assert_eq!(view.minimum, Importance::Debug);
    assert!(view.visible_path("log:389").is_some());
    assert_eq!(view.selected_id(), Some("log:389"));
    let mut summary = fixture().nodes[0].clone();
    summary.importance = "debug".to_string();
    view.ingest(delta(vec![summary, record(410, "summary:7", "critical")]));
    view.handle_key(key(KeyCode::Char('i')));
    assert_eq!(view.minimum, Importance::Critical);
    assert_eq!(
        view.visible_path("log:410").unwrap(),
        ["root", "summary:7", "log:410"]
    );
    assert!(view.visible_path("log:388").is_none());
    assert!(render(&mut view, 160, 45, Theme::new(false)).contains("[filtered parent]"));
}

#[test]
fn search_matches_details_narrative_and_collapsed_records_with_next_previous() {
    let mut view = ProgressView::default();
    view.ingest(delta(vec![
        record(400, "summary:7", "notable"),
        record(401, "summary:7", "routine"),
        fixture().nodes[0].clone(),
    ]));
    view.handle_key(key(KeyCode::Char('/')));
    for character in "patrol.leg_3".chars() {
        view.handle_key(key(KeyCode::Char(character)));
    }
    view.handle_key(key(KeyCode::Enter));
    assert_eq!(view.match_count(), 3); // Two details objects and the summary text.
    let first = view.selected_id().unwrap().to_string();
    view.handle_key(key(KeyCode::Char('n')));
    assert_ne!(view.selected_id(), Some(first.as_str()));
    view.handle_key(key(KeyCode::Char('N')));
    assert_eq!(view.selected_id(), Some(first.as_str()));
    assert!(!view.follow_newest);
    view.handle_key(key(KeyCode::Char('/')));
    view.handle_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
    for character in "Legs 1-2 complete".chars() {
        view.handle_key(key(KeyCode::Char(character)));
    }
    assert_eq!(view.selected_id(), Some("root"));
    assert_eq!(view.match_count(), 1);
}

#[test]
fn search_typing_does_not_invoke_global_actions_but_managed_exit_still_works() {
    let (mut app, _clock) = common::run_app("progress-search-global-keys");
    app.view.tab = RunTab::Progress;
    app.view.progress.ingest(fixture());
    app.handle_key(key(KeyCode::Char('/')));
    for character in "qcif17".chars() {
        app.handle_key(key(KeyCode::Char(character)));
    }
    app.handle_key(key(KeyCode::Tab));
    assert_eq!(app.view.tab, RunTab::Progress);
    assert_eq!(app.view.progress.search, "qcif17");
    assert_eq!(app.view.progress.minimum, Importance::Routine);
    assert!(!app.view.progress.follow_newest);
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert!(!app.should_quit());
    app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
    assert_eq!(app.cancellation, CancellationState::Confirming);
}

fn assert_snapshot(name: &str, actual: &str) {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../docs/design/operator-console/frames")
        .join(name);
    if std::env::var_os("UPDATE_FRAMES").is_some() {
        fs::write(path, actual).unwrap();
        return;
    }
    let expected =
        fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    assert_eq!(
        actual.lines().map(str::trim_end).collect::<Vec<_>>(),
        expected.lines().map(str::trim_end).collect::<Vec<_>>(),
        "snapshot {name}"
    );
}

#[test]
fn progress_snapshots_at_compact_and_standard_sizes() {
    for (width, height) in [(100, 30), (160, 45)] {
        let mut view = ProgressView::default();
        let mut page = fixture();
        for (sequence, title, time) in [
            (5, "First patrol leg completed", "2026-08-27T14:00:40Z"),
            (6, "Investigating ship 2 report", "2026-08-27T14:01:10Z"),
        ] {
            let mut summary = page.nodes[0].clone();
            summary.node_id = format!("summary:{sequence}");
            summary.title = title.to_string();
            summary.text = Some(format!("{title}; operational evidence is nested below."));
            summary.time_end = Some(time.to_string());
            summary.importance = "notable".to_string();
            summary.child_count = 1;
            page.nodes
                .push(record(sequence + 370, &summary.node_id, "notable"));
            page.nodes.push(summary);
        }
        view.ingest(page);
        assert_snapshot(
            &format!("progress-tree-{width}x{height}.txt"),
            &render(&mut view, width, height, Theme::new(true)),
        );
        view.handle_key(key(KeyCode::Char('i')));
        assert_snapshot(
            &format!("progress-tree-debug-{width}x{height}.txt"),
            &render(&mut view, width, height, Theme::new(false)),
        );
        let mut summary = fixture().nodes[0].clone();
        summary.node_id = "summary:8".to_string();
        summary.title = "Terminal summary: patrol complete".to_string();
        summary.text =
            Some("All patrol legs completed; the final report is available.".to_string());
        summary.child_count = 1;
        view.ingest(delta(vec![summary, record(389, "summary:8", "notable")]));
        view.select_node("summary:8");
        assert_snapshot(
            &format!("progress-tree-reparented-{width}x{height}.txt"),
            &render(&mut view, width, height, Theme::new(false)),
        );
    }
}

#[test]
fn recovered_history_backfills_summaries_without_losing_forward_updates_or_selection() {
    use operator_console::app::{App, OwnerSessionState};
    use operator_console::host::{HostCommand, HostMessage, OperatorCursor, OperatorSection};
    let file = common::state_file("progress-history-recovery");
    file.save(&OwnerSessionState {
        host_authority: common::HOST.to_string(),
        host_api_major: 1,
        mission_run_id: common::RUN_ID.to_string(),
        console_session_id: "history-owner".to_string(),
        credential: "history-credential".to_string(),
    })
    .unwrap();
    let mut app = App::new_with_session_file(common::HOST.to_string(), file.clone());
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(common::health())));
    app.take_commands();
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(common::running_run()),
        },
    )));
    app.request_poll();
    let commands = app.take_commands();
    let section = OperatorSection::Progress;
    let mut summary = fixture().nodes[0].clone();
    summary.node_id = "summary:2".to_string();
    let mut initial: Vec<_> = (151..250).map(|n| record(n, "live", "notable")).collect();
    initial
        .iter_mut()
        .find(|node| node.node_id == "log:200")
        .unwrap()
        .parent_id = Some("summary:2".to_string());
    initial.push(summary);
    app.handle_host_message(common::section_reply(
        section,
        common::section_request_id(&commands, section),
        Some("\"initial\""),
        |page| {
            page["next_cursor"] = json!("initial-300");
            page["before_cursor"] = json!("before-151");
            page["has_more"] = json!(true);
            page["progress"]["nodes"] = json!(initial);
        },
    ));
    app.view.progress.handle_key(key(KeyCode::Up));
    app.view.progress.select_node("log:200");
    let commands = app.take_commands();
    assert!(commands.iter().any(|command| matches!(command, HostCommand::FetchOperatorView { cursor: OperatorCursor::Before(cursor), etag: None, .. } if cursor == "before-151")));
    let id = common::section_request_id(&commands, section);
    // A transient failure must retry this same history boundary.
    app.handle_host_message(HostMessage::OperatorView {
        mission_run_id: common::RUN_ID.to_string(),
        section,
        request_id: id,
        result: Err(operator_console::host::HostError::Transport(
            "restart".to_string(),
        )),
    });
    app.request_poll();
    let commands = app.take_commands();
    assert!(commands.iter().any(|command| matches!(command, HostCommand::FetchOperatorView { cursor: OperatorCursor::Before(cursor), .. } if cursor == "before-151")));
    app.handle_host_message(common::section_reply(
        section,
        common::section_request_id(&commands, section),
        None,
        |page| {
            page["next_cursor"] = json!("concurrent-400");
            page["before_cursor"] = json!("before-51");
            page["has_more"] = json!(true);
            page["progress"]["nodes"] = json!(
                (51..151)
                    .map(|n| record(n, "live", "notable"))
                    .collect::<Vec<_>>()
            );
        },
    ));
    let commands = app.take_commands();
    let mut oldest: Vec<_> = (1..51).map(|n| record(n, "summary:1", "notable")).collect();
    let mut early_summary = fixture().nodes[0].clone();
    early_summary.node_id = "summary:1".to_string();
    oldest.push(early_summary);
    oldest.push(record(200, "live", "notable")); // old journal version
    app.handle_host_message(common::section_reply(
        section,
        common::section_request_id(&commands, section),
        None,
        |page| {
            page["next_cursor"] = json!("concurrent-450");
            page["before_cursor"] = serde_json::Value::Null;
            page["has_more"] = json!(false);
            page["progress"]["nodes"] = json!(oldest);
        },
    ));
    assert_eq!(
        app.view.progress.nodes["log:200"].parent_id.as_deref(),
        Some("summary:2")
    );
    assert_eq!(app.view.progress.selected_id(), Some("log:200"));
    assert!(!app.view.progress.follow_newest);
    assert_eq!(app.view.cursor(section), Some("initial-300"));
    let commands = app.take_commands();
    assert!(commands.iter().any(|command| matches!(command, HostCommand::FetchOperatorView { cursor: OperatorCursor::After(cursor), etag: None, .. } if cursor == "initial-300")));
    let mut final_summary = fixture().nodes[0].clone();
    final_summary.node_id = "summary:3".to_string();
    app.handle_host_message(common::section_reply(
        section,
        common::section_request_id(&commands, section),
        None,
        |page| {
            page["next_cursor"] = json!("concurrent-500");
            page["progress"]["nodes"] = json!([
                final_summary,
                record(25, "summary:3", "notable"),
                record(250, "live", "notable")
            ]);
        },
    ));
    assert_eq!(
        app.view
            .progress
            .nodes
            .values()
            .filter(|node| node.level == "record")
            .count(),
        250
    );
    assert!(app.view.progress.nodes.contains_key("summary:1"));
    assert_eq!(
        app.view.progress.nodes["log:25"].parent_id.as_deref(),
        Some("summary:3")
    );
    assert_eq!(app.view.progress.selected_id(), Some("log:200"));
    app.view.progress.handle_key(key(KeyCode::Char('f')));
    assert_eq!(app.view.progress.selected_id(), Some("log:250"));
    file.remove().unwrap();
}

#[test]
fn recovered_pages_keep_summary_record_and_search_navigation_chronological() {
    let summary = |sequence| {
        let mut node = fixture().nodes[0].clone();
        node.node_id = format!("summary:{sequence}");
        node.title = format!("Window {sequence}");
        node
    };
    let mut view = ProgressView::default();
    view.ingest(delta(vec![
        summary(20),
        record(200, "summary:20", "notable"),
        summary(10),
        record(100, "summary:10", "notable"),
    ]));
    view.ingest(delta(vec![
        summary(2),
        record(23, "summary:2", "notable"),
        record(20, "summary:2", "notable"),
        record(22, "summary:2", "notable"),
    ]));
    assert_eq!(view.selected_id(), Some("log:200"));
    view.handle_key(key(KeyCode::Home));
    view.handle_key(key(KeyCode::Down));
    assert_eq!(view.selected_id(), Some("summary:2"));
    view.handle_key(key(KeyCode::Right));
    view.handle_key(key(KeyCode::Down));
    assert_eq!(view.selected_id(), Some("log:20"));
    view.handle_key(key(KeyCode::Down));
    assert_eq!(view.selected_id(), Some("log:22"));
    view.handle_key(key(KeyCode::Down));
    assert_eq!(view.selected_id(), Some("log:23"));
    assert!(view.select_node("log:20"));
    view.handle_key(key(KeyCode::Char('/')));
    for character in "FSM transition".chars() {
        view.handle_key(key(KeyCode::Char(character)));
    }
    view.handle_key(key(KeyCode::Enter));
    assert!(view.select_node("log:20"));
    for expected in ["log:22", "log:23", "log:100", "log:200"] {
        view.handle_key(key(KeyCode::Char('n')));
        assert_eq!(view.selected_id(), Some(expected));
    }
    view.handle_key(key(KeyCode::Char('N')));
    assert_eq!(view.selected_id(), Some("log:100"));
}
