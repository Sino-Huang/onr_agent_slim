//! Terminal receipt (#76 U9): the verdict comes only from the audit artifact,
//! the Context is history once the run ended, and `x` exports through the
//! Host.

mod common;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::{App, ReceiptExportState};
use operator_console::host::{
    HostCommand, HostError, HostMessage, OperatorSection, ReceiptExportOutcome, ReceiptExported,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};

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

fn audit(status: &str, path: Option<&str>, failures: &[&str]) -> Value {
    json!({
        "status": status,
        "path": path,
        "recorded_at": path.map(|_| "2026-08-24T12:06:02Z"),
        "mission_mode": path.map(|_| "mission1"),
        "failures": failures,
    })
}

fn succeeded_with(name: &str, audit: Value) -> App {
    receipt_run_app(
        name,
        succeeded_run(),
        receipt_example(|receipt| receipt["audit"] = audit),
        None,
        |_| {},
    )
    .0
}

fn exported(replaced: bool) -> ReceiptExported {
    let mut value = contract("v1.5", "mission-run-receipt-export.response.json");
    value["replaced"] = json!(replaced);
    serde_json::from_value(value).unwrap()
}

#[test]
fn a_succeeded_lifecycle_without_an_audit_artifact_is_not_a_verdict() {
    let mut app = succeeded_with("receipt-not-recorded", audit("not_recorded", None, &[]));
    for (width, height) in [(100, 30), (160, 45)] {
        let text = screen(&mut app, width, height);
        assert!(text.contains("Lifecycle: succeeded"), "{text}");
        assert!(text.contains("Audit:     not recorded (no audit artifact)"));
        assert!(!text.contains("PASS"), "never infers a pass:\n{text}");
    }
}

#[test]
fn a_succeeded_lifecycle_with_a_failed_audit_shows_fail() {
    let mut app = succeeded_with(
        "receipt-fail",
        audit("fail", Some("live-acceptance.json"), &["fsm_not_terminal"]),
    );
    let text = screen(&mut app, 160, 45);
    assert!(text.contains("Lifecycle: succeeded"));
    assert!(text.contains("Audit:     FAIL · 1 failure: fsm_not_terminal"));
    assert!(!text.contains("PASS"));
}

#[test]
fn only_a_pass_audit_artifact_shows_pass_and_an_unreadable_one_says_so() {
    let mut app = succeeded_with(
        "receipt-pass",
        audit("pass", Some("live-acceptance.json"), &[]),
    );
    assert!(screen(&mut app, 160, 45).contains("Audit:     PASS · live-acceptance.json"));
    let mut app = succeeded_with(
        "receipt-unreadable",
        audit("unreadable", Some("live-acceptance.json"), &[]),
    );
    let text = screen(&mut app, 160, 45);
    assert!(text.contains("unreadable (live-acceptance.json is not a PASS/FAIL audit)"));
    assert!(!text.contains("Audit:     PASS"));
}

#[test]
fn context_is_labelled_last_observed_only_after_termination() {
    let (mut live, _clock) = hydrated_run_app("receipt-context-live", |_| {});
    let text = screen(&mut live, 160, 45);
    assert!(
        !text.contains("last observed"),
        "a live run is live:\n{text}"
    );
    assert!(text.contains("▶ maneuver-7 active"));
    live.handle_key(key(KeyCode::Char('4')));
    let text = screen(&mut live, 160, 45);
    assert!(text.contains("Active: assignment-1-in-progress"));
    assert!(!text.contains("Last observed"));

    let mut ended = succeeded_with("receipt-context-ended", audit("not_recorded", None, &[]));
    let text = screen(&mut ended, 160, 45);
    assert!(
        text.contains("search_area · active (last observed)"),
        "{text}"
    );
    assert!(text.contains("FSM assignment-1-in-progress (last observed)"));
    assert!(text.contains("last maneuver-7 active"), "header: {text}");
    assert!(!text.contains("▶ maneuver"));
    ended.handle_key(key(KeyCode::Char('4')));
    ended.take_commands();
    let text = screen(&mut ended, 160, 45);
    assert!(
        text.contains("Last observed: assignment-1-in-progress"),
        "{text}"
    );
    assert!(text.contains("Last observed Maneuver"));
    assert!(!text.contains("Active: "));
}

#[test]
fn x_asks_the_host_to_export_once_and_shows_the_written_path() {
    let mut app = succeeded_with("receipt-export", audit("not_recorded", None, &[]));
    assert!(screen(&mut app, 160, 45).contains("Export:    x writes mission-run-receipt.json"));
    app.handle_key(key(KeyCode::Char('x')));
    assert_eq!(
        app.take_commands(),
        [HostCommand::ExportReceipt {
            mission_run_id: RUN_ID.to_string(),
            credential: app.session.credential.clone(),
        }]
    );
    assert_eq!(app.view.receipt_export, ReceiptExportState::Exporting);
    // A second press while the Host writes sends nothing.
    app.handle_key(key(KeyCode::Char('x')));
    assert!(app.take_commands().is_empty());

    app.handle_host_message(HostMessage::ReceiptExported(Ok(
        ReceiptExportOutcome::Exported(exported(false)),
    )));
    // The export re-read the audit, so the Overview is fetched once more even
    // though terminal polling has stopped.
    let refresh = app.take_commands();
    assert_eq!(sections(&refresh), [OperatorSection::Overview]);
    let text = screen(&mut app, 160, 45);
    assert!(
        text.contains("Export:    ✔ wrote mission-run-receipt.json"),
        "{text}"
    );
    // The Host's full path, in the value column, unbroken by word wrap.
    let path: String = text
        .lines()
        .skip_while(|line| !line.contains("✔ wrote"))
        .skip(1)
        .take(2)
        .map(|line| {
            line[line.find("│").unwrap() + 3..]
                .split('│')
                .next()
                .unwrap()
                .trim()
        })
        .collect();
    assert_eq!(path, exported(false).path);
    // Exporting again overwrites the same file and says so.
    app.handle_key(key(KeyCode::Char('x')));
    assert_eq!(app.take_commands().len(), 1);
    app.handle_host_message(HostMessage::ReceiptExported(Ok(
        ReceiptExportOutcome::Exported(exported(true)),
    )));
    assert!(
        screen(&mut app, 160, 45).contains("✔ overwrote mission-run-receipt.json (earlier export)")
    );
}

#[test]
fn an_export_after_the_audit_shows_the_verdict_the_file_carries() {
    // The final refresh saw no audit; the operator then ran the audit script.
    let mut app = succeeded_with("receipt-export-audit", audit("not_recorded", None, &[]));
    app.handle_key(key(KeyCode::Char('x')));
    app.take_commands();
    app.handle_host_message(HostMessage::ReceiptExported(Ok(
        ReceiptExportOutcome::Exported(exported(false)),
    )));
    let commands = app.take_commands();
    let receipt = receipt_example(|receipt| {
        receipt["audit"] = audit("fail", Some("live-acceptance.json"), &["fsm_not_terminal"]);
    });
    app.handle_host_message(section_reply(
        OperatorSection::Overview,
        section_request_id(&commands, OperatorSection::Overview),
        None,
        |page| {
            page["run_status"] = json!("succeeded");
            page["has_more"] = json!(false);
            page["overview"]["receipt"] = receipt;
        },
    ));
    let text = screen(&mut app, 160, 45);
    assert!(
        text.contains("Audit:     FAIL · 1 failure: fsm_not_terminal"),
        "{text}"
    );
    assert!(text.contains("✔ wrote mission-run-receipt.json"));
}

#[test]
fn a_refused_or_failed_export_is_shown_and_can_be_retried() {
    let mut app = succeeded_with("receipt-export-refused", audit("not_recorded", None, &[]));
    app.handle_key(key(KeyCode::Char('x')));
    app.take_commands();
    app.handle_host_message(HostMessage::ReceiptExported(Ok(
        ReceiptExportOutcome::Rejected {
            code: "mission_run_not_terminal".to_string(),
            message: "not yet".to_string(),
        },
    )));
    assert!(
        app.take_commands().is_empty(),
        "a refused export refreshes nothing"
    );
    assert!(
        screen(&mut app, 160, 45).contains("Receipt export rejected (mission_run_not_terminal)")
    );
    app.handle_key(key(KeyCode::Char('x')));
    app.take_commands();
    app.handle_host_message(HostMessage::ReceiptExported(Err(HostError::Transport(
        "connection refused".to_string(),
    ))));
    assert!(matches!(
        &app.view.receipt_export,
        ReceiptExportState::Failed(message) if message.contains("connection refused")
    ));
}

#[test]
fn x_does_nothing_while_running_and_needs_a_receipt() {
    let (mut running, _clock) = hydrated_run_app("receipt-running", |_| {});
    running.handle_key(key(KeyCode::Char('x')));
    assert!(running.take_commands().is_empty());

    // An older Host's terminal overview has no receipt: nothing to export.
    let (mut legacy, _clock) = terminal_run_app("receipt-legacy", cancelled_run(), |_, _| {});
    legacy.handle_key(key(KeyCode::Char('x')));
    assert!(legacy.take_commands().is_empty());
    assert!(legacy.hint.as_deref().unwrap().contains("no receipt"));
}
