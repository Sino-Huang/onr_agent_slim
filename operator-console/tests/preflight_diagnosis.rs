//! Preflight as a navigable diagnosis: the Launch screen's Preflight panel is
//! a focusable region listing failures, then warnings, then passes; ↑↓ select
//! a check (kept by `check_id` across refreshes) and Enter opens its full
//! detail, including the Host's copyable diagnostic command (API v1.5).

mod common;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::{App, LaunchField};
use operator_console::host::{HostMessage, StackPreflight};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};

fn screen(app: &mut App, width: u16, height: u16) -> Vec<String> {
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
        .collect()
}

fn row_of(screen: &[String], needle: &str) -> usize {
    screen
        .iter()
        .position(|row| row.contains(needle))
        .unwrap_or_else(|| panic!("{needle:?} not on screen:\n{}", screen.join("\n")))
}

/// Re-run preflight and answer it with the v1.5 example (12 checks; the
/// failing port check is 8th in the Host's order), edited by `edit`.
fn answer_preflight(app: &mut App, edit: impl FnOnce(&mut Vec<Value>)) {
    let focused = app.launch.focus;
    focus(app, LaunchField::Preflight);
    app.handle_key(key(KeyCode::Char('r')));
    focus(app, focused);
    app.check_deadlines();
    let (request_id, query) = single_preflight(app);
    let mut value = contract("v1.5", "stack-preflight.response.json");
    edit(value["checks"].as_array_mut().unwrap());
    let fails = value["checks"]
        .as_array()
        .unwrap()
        .iter()
        .any(|check| check["status"] == "fail");
    value["launchable"] = json!(!fails);
    let mut preflight: StackPreflight = serde_json::from_value(value).unwrap();
    preflight.preset_id = query.preset_id;
    preflight.toggles = query.toggles;
    app.handle_host_message(HostMessage::Preflight {
        request_id,
        result: Ok(preflight),
    });
}

fn selected_id(app: &App) -> String {
    app.launch.selected_check().unwrap().check_id.clone()
}

#[test]
fn twelve_checks_at_100x30_show_the_failure_first_and_its_hint_is_reachable() {
    let (mut app, _clock) = ready_launch_app("preflight-twelve");
    answer_preflight(&mut app, |_| {});
    assert_eq!(app.launch.preflight_checks().len(), 12);

    let launch = screen(&mut app, 100, 30);
    let title = row_of(&launch, "Preflight · launch blocked · ✖1 ▲1 ✔10");
    // Sorted failures → warnings → passes; the failure is the first row.
    assert_eq!(row_of(&launch, "✖ AirSim RPC port free"), title + 1);
    assert_eq!(row_of(&launch, "▲ GPU memory"), title + 2);
    assert_eq!(row_of(&launch, "✔ vLLM reachable"), title + 3);
    // The row is cut at 100 columns; the hint's end is not visible yet.
    assert!(!launch.join("\n").contains("other Harbor engine"));

    // Tab reaches the Preflight region; Enter opens the selected (failing)
    // check with its full hint and the Host's diagnostic command.
    focus(&mut app, LaunchField::Preflight);
    assert!(
        screen(&mut app, 100, 30)
            .join("\n")
            .contains("↑↓ check · Enter check detail")
    );
    app.handle_key(key(KeyCode::Enter));
    let detail = screen(&mut app, 100, 30).join("\n");
    assert!(detail.contains("Preflight check 1/12"), "{detail}");
    assert!(detail.contains("AirSim RPC port free"));
    assert!(detail.contains("fail · blocks launch"));
    assert!(detail.contains("port:41451"));
    assert!(detail.contains("stop the other Harbor engine"));
    assert!(detail.contains("ss -ltnp 'sport = :41451'"));

    // The overlay is modal: review and quit keys do nothing until Esc.
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Char('q')));
    assert!(app.launch.preflight_detail);
    assert!(!app.should_quit());
    app.handle_key(key(KeyCode::Esc));
    assert!(!app.launch.preflight_detail);
    assert_eq!(app.launch.focus, LaunchField::Preflight);
}

#[test]
fn up_down_select_a_check_and_the_detail_follows_the_selection() {
    let (mut app, _clock) = ready_launch_app("preflight-select");
    answer_preflight(&mut app, |_| {});
    focus(&mut app, LaunchField::Preflight);
    app.handle_key(key(KeyCode::Up));
    assert_eq!(
        selected_id(&app),
        "port:41451",
        "Up stops at the first check"
    );
    app.handle_key(key(KeyCode::Down));
    assert_eq!(selected_id(&app), "gpu");
    app.handle_key(key(KeyCode::End));
    assert_eq!(selected_id(&app), "disk");
    app.handle_key(key(KeyCode::Down));
    assert_eq!(selected_id(&app), "disk", "Down stops at the last check");
    app.handle_key(key(KeyCode::Home));
    assert_eq!(selected_id(&app), "port:41451");

    app.handle_key(key(KeyCode::Down));
    app.handle_key(key(KeyCode::Enter));
    let detail = screen(&mut app, 160, 45).join("\n");
    assert!(detail.contains("Preflight check 2/12"));
    assert!(detail.contains("YOLO needs about 2 GiB"));
    assert!(detail.contains("nvidia-smi"));
    // ↑↓ inside the overlay step through checks; a pass has no diagnostic.
    app.handle_key(key(KeyCode::Down));
    let pass = screen(&mut app, 160, 45).join("\n");
    assert!(pass.contains("Preflight check 3/12"));
    assert!(pass.contains("vLLM reachable"));
    assert!(!pass.contains("Diagnose"));
}

#[test]
fn typing_r_in_the_mission_intent_editor_inserts_text() {
    let (mut app, _clock) = ready_launch_app("preflight-r-editor");
    assert_eq!(app.launch.focus, LaunchField::Intent);
    let before = app.launch.editor.text().to_string();
    type_text(&mut app, " r");
    app.check_deadlines();
    assert_eq!(app.launch.editor.text(), format!("{before} r"));
    assert!(app.take_commands().is_empty(), "no preflight re-run");
    assert!(!app.launch.preflight_pending());

    // The same key on the Preflight region re-runs preflight.
    focus(&mut app, LaunchField::Preflight);
    app.handle_key(key(KeyCode::Char('r')));
    app.check_deadlines();
    single_preflight(&mut app);
    assert_eq!(app.launch.editor.text(), format!("{before} r"));
}

#[test]
fn selection_survives_a_refresh_by_check_id() {
    let (mut app, _clock) = ready_launch_app("preflight-keep");
    answer_preflight(&mut app, |_| {});
    focus(&mut app, LaunchField::Preflight);
    app.handle_key(key(KeyCode::Down));
    app.handle_key(key(KeyCode::Down));
    app.handle_key(key(KeyCode::Down));
    assert_eq!(selected_id(&app), "planners");
    assert_eq!(app.launch.selected_check_index(), Some(3));

    // The port frees up and the Host reorders: the same check stays
    // selected at its new position.
    answer_preflight(&mut app, |checks| {
        checks.reverse();
        for check in checks.iter_mut() {
            if check["check_id"] == "port:41451" {
                check["status"] = json!("pass");
                check["detail"] = json!("free");
                check["hint"] = Value::Null;
                check.as_object_mut().unwrap().remove("remediation");
            }
        }
    });
    assert_eq!(selected_id(&app), "planners");
    assert_eq!(app.launch.selected_check_index(), Some(10));
    let launch = screen(&mut app, 100, 30).join("\n");
    assert!(
        launch.contains("Preflight · launchable · ▲1 ✔11"),
        "{launch}"
    );
    assert!(launch.contains("11/12"));

    // An open detail stays on its check across a refresh...
    app.handle_key(key(KeyCode::Enter));
    answer_preflight(&mut app, |_| {});
    assert!(app.launch.preflight_detail);
    assert_eq!(selected_id(&app), "planners");
    // ...and closes when that check is gone, rather than showing another.
    answer_preflight(&mut app, |checks| {
        checks.retain(|check| check["check_id"] != "planners");
    });
    assert!(!app.launch.preflight_detail);
    assert_eq!(app.launch.preflight_selected, None);
    assert_eq!(selected_id(&app), "port:41451");
}

#[test]
fn more_checks_than_rows_scroll_with_the_selection_and_mark_hidden_rows() {
    let (mut app, _clock) = ready_launch_app("preflight-overflow");
    answer_preflight(&mut app, |checks| {
        let template = checks[0].clone();
        for index in 0..18 {
            let mut extra = template.clone();
            extra["check_id"] = json!(format!("extra-{index}"));
            extra["label"] = json!(format!("Extra check {index}"));
            checks.push(extra);
        }
    });
    focus(&mut app, LaunchField::Preflight);
    let top = screen(&mut app, 100, 30).join("\n");
    assert!(top.contains("1/30 ▼"), "{top}");
    assert!(top.contains("✖ AirSim RPC port free"));
    app.handle_key(key(KeyCode::End));
    let bottom = screen(&mut app, 100, 30).join("\n");
    assert!(bottom.contains("30/30"), "{bottom}");
    assert!(bottom.contains(" ▲ "));
    assert!(bottom.contains("Extra check 17"));
    assert!(!bottom.contains("✖ AirSim RPC port free"));
}
