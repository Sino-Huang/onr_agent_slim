//! `w` ("inspect current wait"): from any tab, open what the waiting banner
//! names - a starting service or running prep step on the Stack tab with its
//! log selected and following, or a live agent call on the Agents tab. The
//! target is captured at keypress.

mod common;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::{RunTab, WaitTarget};
use operator_console::host::OperatorSection;
use serde_json::{Value, json};

const STARTING: (&str, &str) = ("v1.5", "mission-run-operator-stack.starting.response.json");
const POST_READY: (&str, &str) = (
    "v1.5",
    "mission-run-operator-stack.post-ready.response.json",
);

fn stack((version, name): (&str, &str)) -> Value {
    stack_example(version, name)
}

fn stack_log_request(app: &mut operator_console::app::App) -> Option<(String, u64)> {
    service_log_request(&app.take_commands())
}

#[test]
fn w_from_overview_during_the_perception_wait_opens_stack_with_perception_following() {
    let (mut app, _clock) = waiting_run_app("inspect-service", "stack", stack(STARTING), |_| {});
    // Leave the Stack tab on another service with its log paused.
    app.handle_key(key(KeyCode::Char('6')));
    app.handle_key(key(KeyCode::PageUp));
    app.handle_key(key(KeyCode::Char('1')));
    app.take_commands();
    assert_eq!(app.view.stack.selected.as_deref(), Some("physical-runtime"));
    assert!(!app.view.stack.follow);
    assert_eq!(
        app.current_wait().map(|wait| wait.target()),
        Some(WaitTarget::StackRow("perception".to_string()))
    );

    app.handle_key(key(KeyCode::Char('w')));

    assert_eq!(app.view.tab, RunTab::Stack);
    let view = &app.view.stack;
    assert_eq!(view.selected.as_deref(), Some("perception"));
    assert!(view.follow, "the log follows its newest line");
    assert_eq!(view.scroll_back, 0);
    assert_eq!(
        view.tail.as_ref().unwrap().artifact_id,
        "service-log-perception"
    );
    assert_eq!(
        stack_log_request(&mut app),
        Some(("service-log-perception".to_string(), 0))
    );
}

#[test]
fn w_keeps_the_target_captured_at_keypress_when_the_wait_moves_on() {
    let (mut app, _clock) = waiting_run_app("inspect-captured", "stack", stack(STARTING), |_| {});
    app.handle_key(key(KeyCode::Char('w')));
    // perception becomes ready and physical-runtime starts.
    let commands = app.take_commands();
    let mut next = stack(STARTING);
    next["services"][1]["state"] = json!("ready");
    next["services"][1]["ready_at"] = json!("2026-08-27T14:01:39Z");
    next["services"][2]["state"] = json!("starting");
    next["services"][2]["started_at"] = json!("2026-08-27T14:01:39Z");
    app.handle_host_message(section_reply(
        OperatorSection::Stack,
        section_request_id(&commands, OperatorSection::Stack),
        None,
        move |page| page["stack"] = next,
    ));
    assert_eq!(
        app.current_wait().map(|wait| wait.target()),
        Some(WaitTarget::StackRow("physical-runtime".to_string()))
    );
    assert_eq!(app.view.stack.selected.as_deref(), Some("perception"));
}

#[test]
fn w_on_a_running_prep_step_opens_its_log_and_steps_are_selectable_rows() {
    let (mut app, _clock) = waiting_run_app("inspect-step", "stack", stack(POST_READY), |_| {});
    app.handle_key(key(KeyCode::Char('3')));
    app.take_commands();

    app.handle_key(key(KeyCode::Char('w')));

    assert_eq!(app.view.tab, RunTab::Stack);
    assert_eq!(
        app.view.stack.selected.as_deref(),
        Some("mission1-surveillance-views")
    );
    assert_eq!(
        stack_log_request(&mut app),
        Some(("service-log-mission1-surveillance-views".to_string(), 0))
    );

    // Rows in plan position: prepare steps, services, post-ready steps.
    let rows: Vec<&str> = operator_console::app::stack_rows(app.view.stack.stack.as_ref().unwrap())
        .map(|row| row.name())
        .collect();
    assert_eq!(
        rows,
        [
            "airsim-fixture",
            "airsim-engine",
            "physical-runtime",
            "airsim-visualizer",
            "closed-loop",
            "mission1-public-input",
            "mission1-surveillance-views",
        ]
    );
    for _ in 0..rows.len() {
        app.handle_key(key(KeyCode::Up));
    }
    assert_eq!(app.view.stack.selected.as_deref(), Some("airsim-fixture"));
    assert_eq!(
        app.view.stack.tail.as_ref().unwrap().artifact_id,
        "service-log-airsim-fixture"
    );
}

#[test]
fn w_on_a_v1_4_prep_step_opens_the_stack_tab_without_inventing_a_row() {
    let (mut app, _clock) = waiting_run_app(
        "inspect-v14-step",
        "stack",
        stack_example("v1.4", "mission-run-operator-stack.preparing.response.json"),
        |_| {},
    );
    let selected = app.view.stack.selected.clone();
    app.handle_key(key(KeyCode::Char('w')));
    assert_eq!(app.view.tab, RunTab::Stack);
    assert_eq!(app.view.stack.selected, selected);
}

#[test]
fn w_on_a_live_llm_call_selects_that_invocation_on_agents() {
    let (mut app, _clock) =
        waiting_run_app("inspect-llm", "planning", stack(STARTING), |overview| {
            set_live_llm_call(overview, "a-31")
        });

    app.handle_key(key(KeyCode::Char('w')));

    assert_eq!(app.view.tab, RunTab::Agents);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("a-31"));
    assert!(!app.view.agent_following);
    // The Agents page arrives with an older and a newer call: the captured
    // call stays selected, and only the call after it counts as newer.
    let id = section_request_id(&app.take_commands(), OperatorSection::Agents);
    app.handle_host_message(agents_reply(id, &["a-1", "a-31", "a-40"]));
    assert_eq!(app.view.selected_invocation.as_deref(), Some("a-31"));
    assert_eq!(app.view.selected_invocation().unwrap().0, 1);
    assert_eq!(app.view.newer_invocations, 1);
}

#[test]
fn w_without_a_wait_stays_and_says_why() {
    let (mut app, _clock) = waiting_run_app("inspect-nothing", "planning", stack(STARTING), |_| {});
    app.handle_key(key(KeyCode::Char('2')));
    app.take_commands();
    app.handle_key(key(KeyCode::Char('w')));
    assert_eq!(app.view.tab, RunTab::Progress);
    assert!(
        app.hint
            .as_deref()
            .unwrap()
            .starts_with("Nothing to inspect")
    );
    // The next key clears the hint.
    app.handle_key(key(KeyCode::Char('1')));
    assert_eq!(app.hint, None);
}
