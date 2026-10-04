//! Failure landing card for infrastructure failures (issue #76 U7): which
//! runs get it, the log tail `l` opens, the OSC 52 copy `y` queues, `e`
//! waiting for cleanup, and Enter/`2` leaving it.

mod common;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::failure::osc52_sequence;
use operator_console::app::{App, Cleanup, RunTab};
use operator_console::host::{
    ArtifactContentPage, ContentPurpose, HostCommand, HostMessage, OperatorSection,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};

fn render(app: &mut App) -> String {
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
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// `(artifact_id, offset)` of every inspector fetch in `commands`.
fn inspector_fetches(commands: &[HostCommand]) -> Vec<(String, u64)> {
    commands
        .iter()
        .filter_map(|command| match command {
            HostCommand::FetchArtifactContent {
                purpose: ContentPurpose::Inspector,
                artifact_id,
                offset,
                ..
            } => Some((artifact_id.clone(), *offset)),
            _ => None,
        })
        .collect()
}

fn inspector_page(artifact_id: &str, offset: u64, content: &str, byte_size: u64) -> HostMessage {
    let end = offset + content.len() as u64;
    let eof = end >= byte_size;
    HostMessage::ArtifactContent {
        purpose: ContentPurpose::Inspector,
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

/// The v1.2 stack example with every service in `state`.
fn stack_in_state(state: &str) -> Value {
    let mut stack = stack_example("v1.2", "mission-run-operator-stack.response.json");
    for service in stack["services"].as_array_mut().unwrap() {
        service["state"] = json!(state);
    }
    stack
}

fn host_interrupted_app(name: &str, service_state: &'static str) -> App {
    terminal_run_app(name, host_interrupted_run(), move |section, page| {
        if section == OperatorSection::Stack {
            page["stack"] = stack_in_state(service_state);
        }
    })
    .0
}

#[test]
fn card_shows_for_each_infrastructure_failure_and_not_for_rejection() {
    for (mut app, kind) in [
        (stack_failed_app("failure-stack").0, "stack_failed"),
        (worker_failed_app("failure-worker").0, "worker_failed"),
        (
            host_interrupted_app("failure-interrupted", "stopped"),
            "host_interrupted",
        ),
    ] {
        assert!(app.failure_open(), "{kind}");
        assert!(!app.rejection_open(), "{kind}");
        let frame = render(&mut app);
        assert!(frame.contains(&format!("✖ RUN FAILED · {kind}")), "{frame}");
        assert!(!frame.contains("MISSION REJECTED"), "{frame}");
    }

    let (mut rejected, _) = terminal_run_app("failure-rejected", rejected_run(), |_, _| {});
    assert!(!rejected.failure_open());
    assert!(rejected.failure_card().is_none());
    let frame = render(&mut rejected);
    assert!(frame.contains("MISSION REJECTED"));
    assert!(!frame.contains("RUN FAILED"));

    // A rejection published before a Host restart does not describe the
    // host_interrupted ending: the failure card wins, without its reason.
    let mut stale = host_interrupted_run();
    stale.terminal_detail = rejected_run().terminal_detail;
    let (mut interrupted, _) = terminal_run_app("failure-stale-rejection", stale, |_, _| {});
    assert!(interrupted.failure_open());
    assert!(!interrupted.rejection_open());
    let card = interrupted.failure_card().unwrap();
    assert!(card.reason.contains("recorded no failure message"));
    assert!(render(&mut interrupted).contains("RUN FAILED · host_interrupted"));
}

#[test]
fn a_running_run_with_a_failed_stack_step_gets_no_card_yet() {
    let stack = stack_example("v1.5", "mission-run-operator-stack.starting.response.json");
    let (app, _) = waiting_run_app("failure-running", "stack", stack, |overview| {
        overview["phase"]["steps"][0]["status"] = json!("failed");
    });
    assert!(!app.failure_open());
    assert!(app.failure_card().is_none());
}

#[test]
fn stack_failed_card_reports_the_host_data_and_labels_the_last_line_as_context() {
    let (mut app, _) = stack_failed_app("failure-fields");
    let card = app.failure_card().unwrap();
    assert_eq!(card.stage, "Stack");
    assert_eq!(
        card.reason,
        "perception producer was not healthy within 900 seconds"
    );
    assert_eq!(
        card.failed_service,
        Some(("perception".to_string(), "terminal detail"))
    );
    assert_eq!(card.last_done_step, "none");
    assert_eq!(card.cleanup, Cleanup::Complete);
    assert_eq!(
        (card.log.artifact_id.as_str(), card.log.named_by_host),
        ("service-log-perception", true)
    );
    assert_eq!(card.last_line.as_deref(), Some("loading YOLO weights"));
    assert_eq!(card.run_root.as_deref(), Some(RUN_ROOT));
    let frame = render(&mut app);
    assert!(frame.contains("loading YOLO weights"));
    assert!(frame.contains("context only, not the cause"));

    let (app, _) = worker_failed_app("failure-worker-fields");
    let card = app.failure_card().unwrap();
    assert_eq!(card.stage, "Executing (closed_loop)");
    assert_eq!(
        card.reason,
        "RuntimeError: external environment has no planning data"
    );
    assert_eq!(card.last_done_step, "Statechart");
    assert_eq!(card.log.artifact_id, "worker-log");
    assert_eq!(
        card.failed_service,
        Some(("closed-loop".to_string(), "stack status"))
    );
}

#[test]
fn l_opens_the_failed_service_log_at_its_tail() {
    let (mut app, _) = stack_failed_app("failure-log-tail");
    app.handle_key(key(KeyCode::Char('l')));
    assert_eq!(
        inspector_fetches(&app.take_commands()),
        [("service-log-perception".to_string(), 0)]
    );
    // The first page proves the log is longer than one page: jump to the end.
    let head: String = (1..=300)
        .map(|n| format!("startup line {n:04}\n"))
        .collect();
    app.handle_host_message(inspector_page(
        "service-log-perception",
        0,
        &head[..4096],
        10_000,
    ));
    assert_eq!(
        inspector_fetches(&app.take_commands()),
        [("service-log-perception".to_string(), 10_000 - 4096)]
    );
    let tail: String = (1..=120)
        .map(|n| format!("health probe {n:03}: 503\n"))
        .chain(["perception producer exiting: weights not found\n".to_string()])
        .collect();
    app.handle_host_message(inspector_page(
        "service-log-perception",
        10_000 - 4096,
        &tail,
        10_000 - 4096 + tail.len() as u64,
    ));
    let frame = render(&mut app);
    // Scrolled to the last line of the last byte page; the card waits below.
    assert!(
        frame.contains("perception producer exiting: weights not found"),
        "{frame}"
    );
    assert!(frame.contains("/121 · bytes 5904-"), "{frame}");
    assert!(!frame.contains("health probe 001"), "{frame}");
    assert!(!frame.contains("RUN FAILED"));
    // Left steps back one page width from a page opened at the tail.
    app.handle_key(key(KeyCode::Left));
    assert_eq!(
        inspector_fetches(&app.take_commands()),
        [("service-log-perception".to_string(), 10_000 - 2 * 4096)]
    );
    app.handle_key(key(KeyCode::Esc));
    assert!(app.view.inspector.is_none());
    assert!(render(&mut app).contains("RUN FAILED · stack_failed"));
}

#[test]
fn l_falls_back_to_host_data_for_older_hosts() {
    // Worker failure: the Host-named worker log.
    let (mut app, _) = worker_failed_app("failure-log-worker");
    app.handle_key(key(KeyCode::Char('l')));
    assert_eq!(
        inspector_fetches(&app.take_commands()),
        [("worker-log".to_string(), 0)]
    );

    // A pre-1.5 stack failure names no log: the failed service's own log
    // from the stack status.
    let mut run = stack_failed_run();
    run.terminal_detail.as_mut().unwrap().log_artifact_id = None;
    let (mut app, _) = terminal_run_app("failure-log-legacy", run, |section, page| {
        if section == OperatorSection::Stack {
            page["stack"] =
                stack_example("v1.5", "mission-run-operator-stack.starting.response.json");
        }
    });
    app.handle_key(key(KeyCode::Char('l')));
    assert_eq!(
        inspector_fetches(&app.take_commands()),
        [("service-log-perception".to_string(), 0)]
    );
    assert!(app.hint.as_deref().unwrap().contains("API < 1.5"));

    // host_interrupted has no detail: the Run Worker log.
    let mut app = host_interrupted_app("failure-log-interrupted", "stopped");
    app.handle_key(key(KeyCode::Char('l')));
    assert_eq!(
        inspector_fetches(&app.take_commands()),
        [("worker-log".to_string(), 0)]
    );
}

#[test]
fn y_queues_an_osc_52_copy_of_the_run_id_and_run_root() {
    let (mut app, _) = stack_failed_app("failure-copy");
    assert_eq!(app.take_clipboard(), None);
    app.handle_key(key(KeyCode::Char('y')));
    let text = app.take_clipboard().unwrap();
    assert_eq!(text, format!("{RUN_ID} {RUN_ROOT}"));
    assert_eq!(app.take_clipboard(), None, "written once");
    assert_eq!(
        osc52_sequence(&text, false),
        "\x1b]52;c;cnVuLWZpeHR1cmUtMDAxIC9zcnYvb25yL3Zhci9ydW50aW1lLWhvc3QvcnVucy9ydW4tZml4dHVyZS0wMDE=\x07"
    );
    assert!(app.hint.as_deref().unwrap().contains("OSC 52"));
    assert!(app.failure_open(), "copying keeps the card open");

    // A Host without `overview.run_root` copies the run id alone and says so.
    let (mut app, _) = terminal_run_app(
        "failure-copy-legacy",
        stack_failed_run(),
        |section, page| {
            if section == OperatorSection::Overview {
                page["overview"].as_object_mut().unwrap().remove("run_root");
            }
        },
    );
    app.handle_key(key(KeyCode::Char('y')));
    assert_eq!(app.take_clipboard().as_deref(), Some(RUN_ID));
    assert!(app.hint.as_deref().unwrap().contains("no run root"));
}

#[test]
fn e_starts_a_new_intent_only_after_cleanup() {
    let mut app = host_interrupted_app("failure-e-running", "ready");
    assert_eq!(
        app.failure_card().unwrap().cleanup,
        Cleanup::Running(vec![
            "physical-runtime".to_string(),
            "closed-loop".to_string()
        ])
    );
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(app.logical_state_name(), "Run");
    assert!(
        app.hint
            .as_deref()
            .unwrap()
            .contains("physical-runtime, closed-loop still recorded as running")
    );
    assert!(render(&mut app).contains("e new Mission Intent after cleanup"));
    // Dismissing the card does not lift the gate.
    app.handle_key(key(KeyCode::Enter));
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(app.logical_state_name(), "Run");

    let mut app = host_interrupted_app("failure-e-stopped", "stopped");
    assert_eq!(app.failure_card().unwrap().cleanup, Cleanup::Complete);
    assert!(app.review_request_id().is_some());
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(app.logical_state_name(), "Launch");
    // Retrying the same intent is a new activation, not a replay of the
    // failed run's Activation Request.
    assert_eq!(app.review_request_id(), None);
}

#[test]
fn cleanup_is_unconfirmed_until_the_stack_status_arrives() {
    // Activated, but no section answered yet: the stack status is unknown.
    let (mut app, _) = ready_launch_app("failure-e-checking");
    accept(&mut app);
    app.take_commands();
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(stack_failed_run()),
        },
    )));
    let card = app.failure_card().unwrap();
    assert_eq!(card.cleanup, Cleanup::Checking);
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(app.logical_state_name(), "Run");
    assert!(
        app.hint
            .as_deref()
            .unwrap()
            .contains("waiting for the stack status")
    );
}

#[test]
fn enter_dismisses_and_2_opens_progress() {
    let (mut app, _) = worker_failed_app("failure-enter");
    app.handle_key(key(KeyCode::Enter));
    assert!(!app.failure_open());
    assert!(app.failure_classified());
    assert!(!render(&mut app).contains("RUN FAILED"));
    // `l` and `y` stay available after the card is dismissed.
    app.handle_key(key(KeyCode::Char('y')));
    assert!(app.take_clipboard().is_some());

    let (mut app, _) = worker_failed_app("failure-progress");
    assert_eq!(app.view.tab, RunTab::Overview);
    app.handle_key(key(KeyCode::Char('2')));
    assert_eq!(app.view.tab, RunTab::Progress);
    assert!(!app.failure_open());
}
