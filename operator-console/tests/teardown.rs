//! Cancellation as visible teardown (issue #76 U8): the waiting banner names
//! the service being stopped with its position in the reverse stop order and
//! the supervisor's grace period; a confirmed cancellation leaves the tabs
//! navigable; the terminal Overview shows the Host's teardown receipt.

mod common;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::{App, RunTab, WaitTarget};
use operator_console::host::{
    CancellationAccepted, CancellationOutcome, HostCommand, HostMessage, OperatorSection,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};

const STOPPING: &str = "mission-run-operator-stack.stopping.response.json";
const CANCELLED: &str = "mission-run-operator-stack.cancelled.response.json";

fn render(app: &mut App) -> Vec<String> {
    app.handle_resize(100, 30);
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|frame| operator_console::ui::draw(frame, app))
        .unwrap();
    let buffer = terminal.backend().buffer();
    (0..30)
        .map(|y| {
            (0..100)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
                .trim_end()
                .to_string()
        })
        .collect()
}

fn banner(app: &mut App) -> String {
    render(app)[3].clone()
}

/// The v1.5 teardown example, retimed so the engine got SIGTERM `elapsed`
/// seconds before the fixture clock (12:00:43Z).
fn stopping_stack(elapsed: i64) -> Value {
    let mut stack = stack_example("v1.5", STOPPING);
    let requested = format!("2026-08-24T12:00:{:02}Z", 43 - elapsed);
    stack["services"][0]["stop_requested_at"] = json!(requested);
    stack["teardown"]["started_at"] = json!("2026-08-24T12:00:30Z");
    stack
}

#[test]
fn teardown_banner_names_the_stopping_service_its_position_and_grace() {
    let (mut app, _clock) =
        waiting_run_app("teardown-banner", "executing", stopping_stack(6), |_| {});
    assert_eq!(
        banner(&mut app),
        " ⠋ Stopping airsim-engine (3/3) · 0:06 / grace 0:30 · w: 6 Stack log"
    );

    // Past 80% of the grace period the timer carries the warning glyph.
    let (mut app, _clock) =
        waiting_run_app("teardown-late", "executing", stopping_stack(25), |_| {});
    assert_eq!(
        banner(&mut app),
        " ⠋ Stopping airsim-engine (3/3) · ▲ 0:25 / grace 0:30 · w: 6 Stack log"
    );
}

#[test]
fn teardown_outranks_a_starting_service_and_a_live_agent_call() {
    let mut stack = stopping_stack(6);
    // A stale `starting` entry and a live LLM call are not what teardown waits on.
    stack["services"][1]["state"] = json!("starting");
    let (mut app, _clock) = waiting_run_app("teardown-precedence", "stack", stack, |overview| {
        set_live_llm_call(overview, "hyper-agent:invocation-1");
    });
    assert!(
        banner(&mut app).starts_with(" ⠋ Stopping airsim-engine (3/3)"),
        "{}",
        banner(&mut app)
    );
    assert_eq!(
        app.current_wait().map(|wait| wait.target()),
        Some(WaitTarget::StackRow("airsim-engine".to_string()))
    );

    // `w` selects the stopping service on the Stack tab, its log following.
    app.handle_key(key(KeyCode::Char('w')));
    assert_eq!(app.view.tab, RunTab::Stack);
    assert_eq!(app.view.stack.selected.as_deref(), Some("airsim-engine"));
    assert!(app.view.stack.follow);
}

#[test]
fn between_services_the_banner_counts_what_teardown_stopped() {
    let mut stack = stopping_stack(6);
    stack["services"][0]["state"] = json!("ready");
    stack["services"][0]
        .as_object_mut()
        .unwrap()
        .remove("stop_requested_at");
    let (mut app, _clock) = waiting_run_app("teardown-between", "executing", stack, |_| {});
    assert_eq!(
        banner(&mut app),
        " ⠋ Stopping the Environment Stack (2/3 stopped) · w: 6 Stack"
    );

    let mut stack = stopping_stack(6);
    stack["services"][0]["state"] = json!("stopped");
    stack["services"][0]["stop_mode"] = json!("graceful");
    stack["services"][0]["stopped_at"] = json!("2026-08-24T12:00:40Z");
    stack["teardown"]["finished_at"] = json!("2026-08-24T12:00:40Z");
    let (mut app, _clock) = waiting_run_app("teardown-finished", "executing", stack, |_| {});
    assert_eq!(
        banner(&mut app),
        " ⠋ Environment Stack stopped · waiting for the Host to record the end · w: 6 Stack"
    );
}

#[test]
fn a_confirmed_cancellation_shows_teardown_instead_of_a_blocking_dialog() {
    let stack = stack_example(
        "v1.5",
        "mission-run-operator-stack.post-ready.response.json",
    );
    let (mut app, _clock) = waiting_run_app("teardown-cancel", "executing", stack, |_| {});
    app.handle_key(key(KeyCode::Char('c')));
    assert!(
        render(&mut app)
            .iter()
            .any(|row| row.contains("Cancel Mission Run"))
    );
    app.handle_key(key(KeyCode::Enter));
    let request_id = app
        .take_commands()
        .into_iter()
        .find_map(|command| match command {
            HostCommand::Cancel { request, .. } => Some(request.cancellation_request_id),
            _ => None,
        })
        .expect("a cancellation was requested");

    // The Host answers only after teardown; meanwhile nothing blocks the view.
    assert!(app.cancellation_in_progress());
    let frame = render(&mut app);
    assert!(!frame.iter().any(|row| row.contains("Cancel Mission Run")));
    assert_eq!(
        frame[3],
        " ⠋ Cancellation requested · waiting for the Run Worker to begin teardown · w: 6 Stack"
    );
    assert!(
        frame
            .iter()
            .any(|row| row.ends_with("? help · w teardown · Ctrl+Q: detach")),
        "{}",
        frame.join("\n")
    );
    assert!(
        !frame
            .iter()
            .any(|row| row.contains("polling the current Mission Run"))
    );
    app.handle_key(key(KeyCode::Char('6')));
    assert_eq!(app.view.tab, RunTab::Stack, "tabs stay navigable");

    // The next stack poll reports the engine stopping.
    app.request_poll();
    let commands = app.take_commands();
    let id = section_request_id(&commands, OperatorSection::Stack);
    app.handle_host_message(section_reply(OperatorSection::Stack, id, None, |page| {
        page["stack"] = stopping_stack(6);
    }));
    assert!(
        banner(&mut app).starts_with(" ⠋ Stopping airsim-engine (3/3) · 0:06 / grace 0:30"),
        "{}",
        banner(&mut app)
    );
    app.handle_host_message(HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(
        CancellationAccepted {
            mission_run_id: RUN_ID.to_string(),
            cancellation_request_id: request_id,
            disposition: "cancellation_requested".to_string(),
            status: "running".to_string(),
            requested_at: "2026-08-24T12:00:30Z".to_string(),
        },
    ))));
    assert!(app.cancellation_in_progress());
    assert!(banner(&mut app).starts_with(" ⠋ Stopping airsim-engine (3/3)"));
}

/// A cancelled run whose stack section is the receipt example, edited.
fn receipt_app(name: &str, edit: impl Fn(&mut Value)) -> App {
    let (app, _clock) = terminal_run_app(name, cancelled_run(), |section, page| {
        if section == OperatorSection::Stack {
            let mut stack = stack_example("v1.5", CANCELLED);
            edit(&mut stack);
            page["stack"] = stack;
        }
    });
    app
}

fn receipt(app: &mut App) -> Vec<String> {
    render(app)
        .into_iter()
        .filter_map(|row| {
            let start = row.find(" Teardown:").or_else(|| row.find(" Services:"));
            let start = start.or_else(|| row.find(" Harbor:"))?;
            let cell = &row[start..];
            Some(
                cell[..cell.find('│').unwrap_or(cell.len())]
                    .trim_end()
                    .to_string(),
            )
        })
        .collect()
}

#[test]
fn receipt_distinguishes_worker_services_and_a_confirmed_restoration() {
    let mut app = receipt_app("receipt-forced", |_| {});
    assert_eq!(
        receipt(&mut app),
        [
            " Teardown:  worker stopped · took 0:10",
            " Services:  3 stopped · 2 graceful, 1 forced",
            " Harbor:    config restored (guardian reported)",
        ]
    );
}

#[test]
fn receipt_for_an_all_graceful_teardown_reported_by_the_engine() {
    let mut app = receipt_app("receipt-graceful", |stack| {
        stack["services"][0]["stop_mode"] = json!("graceful");
        stack["teardown"]["harbor_config"]["reported_by"] = json!("engine");
    });
    assert_eq!(
        receipt(&mut app)[1..],
        [
            " Services:  3 stopped · all graceful",
            " Harbor:    config restored (engine reported)",
        ]
    );
}

#[test]
fn receipt_never_claims_an_unreported_restoration() {
    let mut app = receipt_app("receipt-unknown", |stack| {
        stack["teardown"]["harbor_config"] = json!({"state": "unknown", "reported_by": null});
    });
    assert_eq!(
        receipt(&mut app)[2],
        " Harbor:    restoration unknown (no report)"
    );

    // A Host reap before the supervisor began: no start time, nothing graceful.
    let mut app = receipt_app("receipt-reaped", |stack| {
        stack["teardown"]["started_at"] = Value::Null;
        stack["teardown"]["harbor_config"] =
            json!({"state": "not_applicable", "reported_by": null});
        for service in stack["services"].as_array_mut().unwrap().iter_mut().take(3) {
            service["stop_mode"] = json!("forced");
        }
    });
    assert_eq!(
        receipt(&mut app),
        [
            " Teardown:  worker stopped",
            " Services:  3 stopped · all forced",
            " Harbor:    not applicable (no AirSim engine)",
        ]
    );
}

#[test]
fn a_running_run_shows_no_receipt() {
    let (mut app, _clock) =
        waiting_run_app("receipt-running", "executing", stopping_stack(6), |_| {});
    assert!(receipt(&mut app).is_empty());
}
