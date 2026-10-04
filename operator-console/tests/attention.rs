//! Attention signals (issue #76 U3): edge detection on run status and the
//! Host phase. Each kind fires exactly once per Mission Run; conditions that
//! are already true when a restarted console first sees its recovered run
//! are history and never replay.

mod common;

use common::*;
use operator_console::app::{App, AttentionKind, OwnerSessionState, SessionStateFile};
use operator_console::host::{
    CurrentRun, HostCommand, HostMessage, OperatorSection, RunRecord, TerminalDetail,
};
use serde_json::{Value, json};

fn kinds(app: &mut App) -> Vec<AttentionKind> {
    app.take_attention_events()
        .into_iter()
        .map(|event| event.kind)
        .collect()
}

fn messages(app: &mut App) -> Vec<String> {
    app.take_attention_events()
        .into_iter()
        .map(|event| event.message)
        .collect()
}

fn current(app: &mut App, run: RunRecord) {
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(run),
    })));
}

fn with_status(status: &str) -> RunRecord {
    RunRecord {
        status: status.to_string(),
        ..running_run()
    }
}

fn terminal(status: &str, classification: &str, detail_kind: Option<&str>) -> RunRecord {
    RunRecord {
        status: status.to_string(),
        finished_at: Some("2026-08-24T12:04:03Z".to_string()),
        terminal_classification: Some(classification.to_string()),
        terminal_detail: detail_kind.map(|kind| TerminalDetail {
            kind: kind.to_string(),
            stage: Some("stack".to_string()),
            reason: None,
            service: Some("perception".to_string()),
            error_type: None,
            message: Some("readiness timed out".to_string()),
            log_artifact_id: None,
        }),
        ..running_run()
    }
}

/// Answer the overview request among `commands` with phase edits.
fn answer_overview(app: &mut App, commands: &[HostCommand], edit: impl FnOnce(&mut Value)) {
    let request_id = section_request_id(commands, OperatorSection::Overview);
    app.handle_host_message(section_reply(
        OperatorSection::Overview,
        request_id,
        None,
        |page| edit(&mut page["overview"]),
    ));
}

fn poll_overview(app: &mut App, edit: impl FnOnce(&mut Value)) {
    app.request_poll();
    let commands = app.take_commands();
    answer_overview(app, &commands, edit);
}

fn stack_failed_phase(overview: &mut Value) {
    set_phase(overview, "stack");
    overview["phase"]["steps"][0]["status"] = json!("failed");
    overview["phase"]["steps"][0]["detail"] = json!("perception: readiness timed out");
}

/// A run this console just activated (`queued`) with the first overview at
/// the `stack` phase; events drained.
fn activated(name: &str) -> App {
    let (mut app, _clock) = ready_launch_app(name);
    accept(&mut app);
    let commands = app.take_commands();
    answer_overview(&mut app, &commands, |overview| set_phase(overview, "stack"));
    assert_eq!(kinds(&mut app), [], "queued at the stack phase is not news");
    app
}

fn recovered(file: SessionStateFile) -> App {
    let mut app = App::new_with_session_file(HOST.to_string(), file);
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.take_commands();
    app
}

fn owner_file(name: &str) -> SessionStateFile {
    let file = state_file(name);
    file.save(&OwnerSessionState {
        host_authority: HOST.to_string(),
        host_api_major: 1,
        mission_run_id: RUN_ID.to_string(),
        console_session_id: "attention-owner".to_string(),
        credential: "attention-credential".to_string(),
    })
    .unwrap();
    file
}

#[test]
fn each_transition_of_an_activated_run_fires_exactly_once() {
    let mut app = activated("attention-transitions");

    poll_overview(&mut app, |overview| set_phase(overview, "intent"));
    let events = app.take_attention_events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].kind, AttentionKind::StackReady);
    assert_eq!(events[0].mission_run_id, RUN_ID);
    assert_eq!(events[0].message, "Stack ready · planning started");
    poll_overview(&mut app, |overview| set_phase(overview, "planning"));
    assert_eq!(kinds(&mut app), [], "stack ready fires once");

    current(&mut app, with_status("awaiting_human_decision"));
    assert_eq!(messages(&mut app), ["Awaiting a Human Decision"]);
    current(&mut app, with_status("awaiting_human_decision"));
    current(&mut app, with_status("running"));
    current(&mut app, with_status("awaiting_human_decision"));
    assert_eq!(
        kinds(&mut app),
        [],
        "keyed by run id + event: a second request in the same run stays silent"
    );

    current(&mut app, terminal("succeeded", "succeeded", None));
    assert_eq!(messages(&mut app), ["✔ succeeded"]);
    current(&mut app, terminal("succeeded", "succeeded", None));
    assert_eq!(kinds(&mut app), []);
}

#[test]
fn a_stack_failure_before_the_run_ends_fires_once_then_the_terminal_status_fires() {
    let mut app = activated("attention-stack-failed-first");

    poll_overview(&mut app, stack_failed_phase);
    assert_eq!(
        messages(&mut app),
        ["Stack failed · perception: readiness timed out"]
    );
    poll_overview(&mut app, stack_failed_phase);
    assert_eq!(kinds(&mut app), []);

    current(
        &mut app,
        terminal("failed", "stack_failed", Some("stack_failed")),
    );
    let events = app.take_attention_events();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].kind, AttentionKind::Terminal);
    assert_eq!(events[0].message, "✖ stack_failed");
}

#[test]
fn a_stack_failure_first_seen_as_the_terminal_status_is_one_terminal_event() {
    let mut app = activated("attention-stack-failed-terminal");

    current(
        &mut app,
        terminal("failed", "stack_failed", Some("stack_failed")),
    );
    assert_eq!(kinds(&mut app), [AttentionKind::Terminal]);
    // The final overview wave proves the failed stack step: already told.
    poll_overview(&mut app, stack_failed_phase);
    assert_eq!(kinds(&mut app), []);
}

#[test]
fn a_terminal_run_never_reports_stale_stack_ready_or_decision_events() {
    let mut app = activated("attention-terminal-stale");

    current(&mut app, terminal("cancelled", "cancelled_by_owner", None));
    assert_eq!(messages(&mut app), ["✖ cancelled_by_owner"]);
    poll_overview(&mut app, |overview| set_phase(overview, "terminal"));
    assert_eq!(kinds(&mut app), [], "stack done after the end is not news");
}

#[test]
fn recovering_an_already_terminal_run_replays_nothing() {
    let mut app = recovered(owner_file("attention-recover-terminal"));

    current(
        &mut app,
        terminal("failed", "stack_failed", Some("stack_failed")),
    );
    assert_eq!(kinds(&mut app), []);
    poll_overview(&mut app, stack_failed_phase);
    assert_eq!(kinds(&mut app), []);
    assert_eq!(app.window_title(), "ONR ✖ stack_failed");
}

#[test]
fn a_console_restart_replays_nothing_already_true_but_reports_new_transitions() {
    let file = state_file("attention-restart");
    {
        // Before the restart: this console activated the run and saw the
        // stack become ready, then a Human Decision Request.
        let (mut app, _clock) = run_app_with_file("attention-restart", file.clone());
        assert_eq!(kinds(&mut app), [AttentionKind::StackReady]);
        current(&mut app, with_status("awaiting_human_decision"));
        assert_eq!(kinds(&mut app), [AttentionKind::AwaitingHumanDecision]);
    }

    let mut app = recovered(file);
    current(&mut app, with_status("awaiting_human_decision"));
    assert_eq!(kinds(&mut app), [], "the open request is not replayed");
    poll_overview(&mut app, |overview| set_phase(overview, "executing"));
    assert_eq!(
        kinds(&mut app),
        [],
        "the stack was ready before the restart"
    );
    current(&mut app, with_status("running"));
    assert_eq!(kinds(&mut app), []);

    current(
        &mut app,
        terminal("failed", "worker_failed", Some("worker_failed")),
    );
    assert_eq!(messages(&mut app), ["✖ worker_failed"]);
}

#[test]
fn a_recovered_run_still_reports_the_stack_becoming_ready_after_the_restart() {
    let mut app = recovered(owner_file("attention-recover-stack"));

    current(&mut app, with_status("running"));
    poll_overview(&mut app, |overview| set_phase(overview, "stack"));
    assert_eq!(kinds(&mut app), []);
    poll_overview(&mut app, |overview| set_phase(overview, "intent"));
    assert_eq!(kinds(&mut app), [AttentionKind::StackReady]);
}

#[test]
fn the_window_title_tracks_the_run_status() {
    let (mut app, _clock) = ready_launch_app("attention-title");
    assert_eq!(app.window_title(), "ONR");

    accept(&mut app);
    let commands = app.take_commands();
    answer_overview(&mut app, &commands, |overview| {
        set_phase(overview, "planning")
    });
    current(&mut app, running_run());
    assert_eq!(app.window_title(), "ONR ● running 00:00:40 · Planning");

    current(&mut app, with_status("awaiting_human_decision"));
    assert!(
        app.window_title()
            .starts_with("ONR ● awaiting_human_decision 00:00:40")
    );

    current(&mut app, terminal("succeeded", "succeeded", None));
    assert_eq!(app.window_title(), "ONR ✔ succeeded");
}
