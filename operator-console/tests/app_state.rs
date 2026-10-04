//! State machine and keyboard behavior: handshake, Launch screen, activation,
//! Run tabs and polling, terminal final refresh, cancellation and managed
//! exit, ownership, liveness, session persistence, and the Stack tab tail.

mod common;

use std::fs;
use std::time::Duration;

use common::*;
use crossterm::event::{KeyCode, KeyModifiers};
use operator_console::app::{
    App, AppState, CancellationState, CleanExitAction, LaunchField, Liveness, LivenessThresholds,
    MIN_HEIGHT, MIN_WIDTH, OwnerSessionState, RunTab, SessionStateFile,
};
use operator_console::host::{
    ApiVersion, CancellationAccepted, CancellationOutcome, ContentPurpose, CurrentRun, Fetched,
    Health, HostCommand, HostError, HostMessage, MissionIntent, OperatorSection,
};
use serde_json::json;

// ---------------------------------------------------------------------------
// Handshake
// ---------------------------------------------------------------------------

#[test]
fn new_app_connects_first() {
    let mut app = App::new_with_session_file(HOST.to_string(), state_file("new"));
    assert_eq!(app.state, AppState::Connecting);
    assert_eq!(app.take_commands(), [HostCommand::Connect]);
}

#[test]
fn connect_failure_is_retryable_and_r_reconnects() {
    let (mut app, _) = app_with_clock("connect-failure");
    app.handle_host_message(HostMessage::Connected(Err(HostError::Transport(
        "refused".to_string(),
    ))));
    assert!(matches!(
        app.state,
        AppState::Error {
            retry_connect: true,
            ..
        }
    ));
    app.handle_key(key(KeyCode::Char('r')));
    assert_eq!(app.state, AppState::Connecting);
    assert_eq!(app.take_commands(), [HostCommand::Connect]);
}

#[test]
fn hosts_below_v1_2_are_too_old_and_other_majors_incompatible() {
    for (major, minor, expected) in [
        (1, 0, "Runtime Host too old"),
        (1, 1, "Runtime Host too old"),
        (2, 2, "Incompatible Runtime Host API v2.2"),
    ] {
        let (mut app, _) = app_with_clock("too-old");
        app.handle_host_message(HostMessage::Connected(Ok(Health {
            status: "ok".to_string(),
            api_version: ApiVersion { major, minor },
        })));
        match &app.state {
            AppState::Error { message, .. } => assert!(message.contains(expected), "{message}"),
            other => panic!("expected an error for v{major}.{minor}, got {other:?}"),
        }
        assert!(app.take_commands().is_empty(), "no presets for an old Host");
        assert!(app.health.is_none());
    }
}

// ---------------------------------------------------------------------------
// Launch screen
// ---------------------------------------------------------------------------

#[test]
fn connecting_loads_presets_prefills_intent_and_runs_preflight_at_once() {
    let (mut app, _clock) = app_with_clock("launch");
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    assert_eq!(app.state, AppState::Launch);
    assert_eq!(app.take_commands(), [HostCommand::FetchPresets]);
    app.handle_host_message(HostMessage::Presets(Ok(presets())));
    assert_eq!(app.launch.preset().unwrap().preset_id, "mission1-harbor");
    assert_eq!(
        app.launch.editor.text(),
        "Please patrol the environment and confirm every reported event."
    );
    assert_eq!(app.launch.focus, LaunchField::Intent);
    app.check_deadlines();
    let (_, query) = single_preflight(&mut app);
    assert_eq!(query.preset_id, "mission1-harbor");
    assert!(!query.toggles.airsim);
    assert_eq!(query.toggles.perception, "off");
    assert!(app.launch.preflight_pending());
}

#[test]
fn toggles_honor_supports_and_rerun_preflight_after_a_300ms_debounce() {
    let (mut app, clock) = ready_launch_app("debounce");
    assert_eq!(app.launch.airsim_options(), [false, true]);
    assert_eq!(app.launch.perception_options(), ["off"]);
    focus(&mut app, LaunchField::Airsim);
    app.handle_key(key(KeyCode::Right));
    assert!(
        app.launch.airsim,
        "mission1-harbor can follow the world model in AirSim"
    );
    assert_eq!(app.launch.perception, "off");
    assert_eq!(app.launch.perception_options(), ["off"]);
    focus(&mut app, LaunchField::Perception);
    app.handle_key(key(KeyCode::Right));
    assert_eq!(
        app.launch.perception, "off",
        "perception stays off on Harbor"
    );

    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Right));
    let preset = app.launch.preset().unwrap();
    assert_eq!(preset.preset_id, "mission1-airsim");
    assert!(app.launch.airsim);
    assert_eq!(app.launch.perception, "yolo");
    assert_eq!(app.launch.simulation_limit_seconds, 290);
    assert_eq!(app.launch.airsim_options(), [true]);
    assert_eq!(app.launch.perception_options(), ["off", "ideal", "yolo"]);

    clock.advance(Duration::from_millis(200));
    focus(&mut app, LaunchField::Perception);
    app.handle_key(key(KeyCode::Right));
    assert_eq!(
        app.launch.perception, "off",
        "AirSim on with perception off is selectable"
    );
    clock.advance(Duration::from_millis(299));
    app.check_deadlines();
    assert!(
        app.take_commands().is_empty(),
        "a change restarts the debounce"
    );
    clock.advance(Duration::from_millis(1));
    app.check_deadlines();
    let (_, query) = single_preflight(&mut app);
    assert_eq!(query.preset_id, "mission1-airsim");
    assert!(query.toggles.airsim);
    assert_eq!(query.toggles.perception, "off");
    app.check_deadlines();
    assert!(app.take_commands().is_empty(), "one preflight per change");
}

#[test]
fn only_the_latest_preflight_answer_for_the_current_selection_counts() {
    let (mut app, clock) = ready_launch_app("stale-preflight");
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Right));
    clock.advance(Duration::from_millis(300));
    app.check_deadlines();
    let (old_id, old_query) = single_preflight(&mut app);
    app.handle_key(key(KeyCode::Left));
    clock.advance(Duration::from_millis(300));
    app.check_deadlines();
    let (new_id, new_query) = single_preflight(&mut app);
    assert_eq!(new_query.preset_id, "mission1-harbor");
    app.handle_host_message(HostMessage::Preflight {
        request_id: old_id,
        result: Ok(preflight(&old_query, false)),
    });
    // The superseded answer is ignored; launch waits for the current one.
    assert!(app.launch.current_preflight().unwrap().allows_launch());
    assert_eq!(
        app.launch.launch_blocker().as_deref(),
        Some("Preflight running")
    );
    app.handle_host_message(HostMessage::Preflight {
        request_id: new_id,
        result: Ok(preflight(&new_query, true)),
    });
    assert!(app.launch.current_preflight().unwrap().allows_launch());
    assert_eq!(app.launch.launch_blocker(), None);
}

#[test]
fn launch_is_disabled_while_any_check_fails() {
    let (mut app, _clock) = app_with_clock("blocked");
    connect_with_presets(&mut app);
    app.check_deadlines();
    let (id, query) = single_preflight(&mut app);
    app.handle_host_message(HostMessage::Preflight {
        request_id: id,
        result: Ok(preflight(&query, false)),
    });
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::Launch);
    let hint = app.hint.clone().unwrap();
    assert!(hint.contains("AirSim RPC port free"), "{hint}");
    assert!(app.take_commands().is_empty());

    app.handle_key(key(KeyCode::Char('r')));
    assert_eq!(app.launch.editor.text().chars().last(), Some('r'));
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Char('r')));
    app.check_deadlines();
    let (id, query) = single_preflight(&mut app);
    app.handle_host_message(HostMessage::Preflight {
        request_id: id,
        result: Ok(preflight(&query, true)),
    });
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::ReviewActivation);
}

#[test]
fn preset_changes_replace_an_untouched_intent_but_keep_an_edited_one() {
    let (mut app, _clock) = ready_launch_app("prefill");
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Right));
    assert_eq!(
        app.launch.editor.text(),
        "Patrol the window 60-130 s and verify every reported event."
    );
    focus(&mut app, LaunchField::Intent);
    type_text(&mut app, " now");
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Left));
    assert_eq!(
        app.launch.editor.text(),
        "Patrol the window 60-130 s and verify every reported event. now"
    );
}

#[test]
fn f2_offers_the_preset_mission_and_the_rejection_battery() {
    let (mut app, _clock) = ready_launch_app("demo-prompts");
    app.handle_key(key(KeyCode::F(2)));
    assert_eq!(app.launch.demo_picker, Some(0));
    let prompts: Vec<String> = app
        .launch
        .demo_prompts()
        .into_iter()
        .map(|(_, text)| text)
        .collect();
    assert_eq!(prompts.len(), 4);
    assert_eq!(prompts[1], "buy me a coffee");
    assert_eq!(prompts[2], "What is the capital of France?");
    app.handle_key(key(KeyCode::Down));
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.launch.demo_picker, None);
    assert_eq!(app.launch.editor.text(), "buy me a coffee");
    assert_eq!(app.launch.focus, LaunchField::Intent);
    app.handle_key(key(KeyCode::F(2)));
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.launch.demo_picker, None);
    assert_eq!(app.launch.editor.text(), "buy me a coffee");
}

#[test]
fn intent_editor_supports_newlines_cursor_movement_and_deletion() {
    let (mut app, _clock) = ready_launch_app("editor");
    app.launch.editor.set_text("");
    type_text(&mut app, "ab");
    app.handle_key(key(KeyCode::Enter));
    type_text(&mut app, "cd");
    assert_eq!(app.launch.editor.text(), "ab\ncd");
    app.handle_key(key(KeyCode::Up));
    assert_eq!(app.launch.editor.cursor_line_col(), (0, 2));
    app.handle_key(key(KeyCode::Home));
    app.handle_key(key(KeyCode::Delete));
    assert_eq!(app.launch.editor.text(), "b\ncd");
    app.handle_key(key(KeyCode::Down));
    app.handle_key(key(KeyCode::End));
    app.handle_key(key(KeyCode::Backspace));
    assert_eq!(app.launch.editor.text(), "b\nc");
    app.handle_key(key(KeyCode::Char('q')));
    assert!(!app.should_quit(), "q types inside the intent");
    assert_eq!(app.launch.editor.text(), "b\ncq");
}

#[test]
fn empty_intent_cannot_be_reviewed() {
    let (mut app, _clock) = ready_launch_app("empty");
    app.launch.editor.set_text("  ");
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::Launch);
    assert!(
        app.hint
            .as_deref()
            .unwrap()
            .contains("Mission Intent is empty")
    );
}

#[test]
fn review_submits_exactly_once_with_the_selected_stack() {
    let (mut app, _clock) = ready_launch_app("submit");
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::ReviewActivation);
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.state, AppState::Launch);
    app.handle_key(alt_enter());
    let review_id = app.review_request_id().unwrap().to_string();
    app.handle_key(key(KeyCode::Enter));
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.state, AppState::Submitting);
    let commands = app.take_commands();
    assert_eq!(commands.len(), 1);
    let HostCommand::Submit {
        request,
        credential,
    } = &commands[0]
    else {
        panic!("expected Submit, got {commands:?}");
    };
    assert_eq!(credential, &app.session.credential);
    assert_eq!(request.activation_request_id, review_id);
    assert_eq!(request.source_authority, "operator_console");
    let stack = request.stack.as_ref().unwrap();
    assert_eq!(stack.preset_id, "mission1-harbor");
    assert_eq!(stack.simulation_limit_seconds, 600);
    assert_eq!(stack.update_ownership, "coordinator_driven");
}

#[test]
fn retries_reuse_the_request_id_until_intent_or_stack_change() {
    let (mut app, _clock) = ready_launch_app("retry-id");
    app.handle_key(alt_enter());
    let first = app.review_request_id().unwrap().to_string();
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(HostMessage::Activated(Err(HostError::Transport(
        "timeout".to_string(),
    ))));
    assert!(matches!(app.state, AppState::Error { .. }));
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.state, AppState::Launch);
    // Returning to Launch re-runs preflight before another review.
    app.check_deadlines();
    let (id, query) = single_preflight(&mut app);
    app.handle_host_message(HostMessage::Preflight {
        request_id: id,
        result: Ok(preflight(&query, true)),
    });
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::ReviewActivation);
    assert_eq!(app.review_request_id(), Some(first.as_str()));

    app.handle_key(key(KeyCode::Esc));
    focus(&mut app, LaunchField::SimLimit);
    app.handle_key(key(KeyCode::Right));
    assert_eq!(app.launch.simulation_limit_seconds, 630);
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::ReviewActivation);
    assert_ne!(app.review_request_id(), Some(first.as_str()));
}

// ---------------------------------------------------------------------------
// Run screen
// ---------------------------------------------------------------------------

#[test]
fn accepted_activation_persists_ownership_enters_run_and_polls_current_overview_stack() {
    let file = state_file("accepted");
    let (mut app, _clock) = ready_launch_app_with_file(file.clone());
    accept(&mut app);
    assert_eq!(app.state, AppState::Run);
    let saved = file.load().unwrap().unwrap();
    assert_eq!(saved.mission_run_id, RUN_ID);
    assert_eq!(saved.credential, app.session.credential);
    let run = app.run.as_ref().unwrap();
    assert_eq!(run.stack.as_ref().unwrap().preset_id, "mission1-harbor");
    let commands = app.take_commands();
    assert!(commands.contains(&HostCommand::PollCurrent {
        credential: app.session.credential.clone(),
    }));
    assert_eq!(
        sections(&commands),
        [
            OperatorSection::Overview,
            OperatorSection::Stack,
            OperatorSection::Progress,
            OperatorSection::Beliefs,
            OperatorSection::Context,
            OperatorSection::World,
            OperatorSection::Agents,
            OperatorSection::Environment,
            OperatorSection::Artifacts
        ]
    );
    file.remove().unwrap();
}

#[test]
fn tabs_switch_with_digits_and_tab_and_poll_every_visible_section() {
    let (mut app, _clock) = run_app("tabs");
    for (digit, tab) in [
        ('3', RunTab::Agents),
        ('2', RunTab::Progress),
        ('4', RunTab::BeliefContext),
        ('5', RunTab::World),
        ('7', RunTab::Artifacts),
        ('6', RunTab::Stack),
        ('1', RunTab::Overview),
    ] {
        app.handle_key(key(KeyCode::Char(digit)));
        assert_eq!(app.view.tab, tab);
        let commands = app.take_commands();
        assert_eq!(sections(&commands), tab.sections());
        for section in sections(&commands) {
            app.handle_host_message(section_reply(
                section,
                section_request_id(&commands, section),
                None,
                |_| {},
            ));
        }
        app.take_commands();
    }
    app.handle_key(key(KeyCode::Tab));
    assert_eq!(app.view.tab, RunTab::Progress);
    let commands = app.take_commands();
    app.handle_host_message(section_reply(
        OperatorSection::Progress,
        section_request_id(&commands, OperatorSection::Progress),
        None,
        |_| {},
    ));
    app.handle_key(key(KeyCode::BackTab));
    let commands = app.take_commands();
    for section in sections(&commands) {
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |_| {},
        ));
    }
    app.handle_key(key(KeyCode::BackTab));
    assert_eq!(app.view.tab, RunTab::Artifacts);
    let commands = app.take_commands();
    app.handle_host_message(artifacts_reply(section_request_id(
        &commands,
        OperatorSection::Artifacts,
    )));
    app.take_commands();
    app.request_poll();
    assert_eq!(
        sections(&app.take_commands()),
        [
            OperatorSection::Overview,
            OperatorSection::Stack,
            OperatorSection::Artifacts
        ]
    );
}

#[test]
fn section_replies_carry_cursor_and_etag_and_ignore_stale_or_304_replies() {
    let (mut app, _clock) = run_app("cursor-etag");
    app.request_poll();
    let request_id = section_request_id(&app.take_commands(), OperatorSection::Overview);
    app.handle_host_message(overview_reply(request_id, Some("\"o-1\"")));
    assert_eq!(
        app.view
            .overview
            .as_ref()
            .unwrap()
            .phase
            .as_ref()
            .unwrap()
            .current,
        "executing"
    );
    app.request_poll();
    let commands = app.take_commands();
    let (cursor, etag) = section_cursor_etag(&commands, OperatorSection::Overview);
    assert_eq!(cursor.as_deref(), Some("overview-cursor-3"));
    assert_eq!(etag.as_deref(), Some("\"o-1\""));
    let next = section_request_id(&commands, OperatorSection::Overview);
    // A reply to the superseded request is ignored.
    app.handle_host_message(HostMessage::OperatorView {
        mission_run_id: RUN_ID.to_string(),
        section: OperatorSection::Overview,
        request_id,
        result: Err(HostError::Transport("late".to_string())),
    });
    assert_eq!(app.notice, None);
    app.handle_host_message(HostMessage::OperatorView {
        mission_run_id: RUN_ID.to_string(),
        section: OperatorSection::Overview,
        request_id: next,
        result: Ok(Fetched::NotModified),
    });
    assert!(app.view.overview.is_some(), "304 keeps the last overview");
}

#[test]
fn terminal_refresh_drains_all_pages_waits_for_final_narrative_then_stops() {
    let (mut app, clock) = run_app("final-refresh");
    app.handle_host_message(current(rejected_run()));
    app.request_poll();
    let commands = app.take_commands();
    assert_eq!(sections(&commands), OperatorSection::ALL);
    assert!(!app.polling_stopped());
    for section in sections(&commands) {
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |page| {
                page["run_status"] = json!("failed");
                if section == OperatorSection::Progress {
                    page["has_more"] = json!(true);
                    page["next_cursor"] = json!("terminal-progress-page-2");
                }
            },
        ));
    }
    let drain = app.take_commands();
    assert_eq!(sections(&drain), [OperatorSection::Progress]);
    assert_eq!(
        section_cursor_etag(&drain, OperatorSection::Progress)
            .0
            .as_deref(),
        Some("terminal-progress-page-2")
    );
    app.handle_host_message(section_reply(
        OperatorSection::Progress,
        section_request_id(&drain, OperatorSection::Progress),
        None,
        |page| {
            page["run_status"] = json!("failed");
        },
    ));
    app.take_commands();
    clock.advance(Duration::from_millis(400));
    app.request_poll();
    assert!(
        app.take_commands().is_empty(),
        "terminal waiting does not poll at400ms"
    );
    clock.advance(Duration::from_millis(1600));
    app.request_poll();
    let commands = app.take_commands();
    assert_eq!(sections(&commands), [OperatorSection::Overview]);
    app.handle_host_message(section_reply(
        OperatorSection::Overview,
        section_request_id(&commands, OperatorSection::Overview),
        None,
        |page| {
            page["run_status"] = json!("failed");
            page["overview"]["narrative"]["terminal"] = json!(true);
            page["overview"]["narrative"]["status"] = json!("available");
            page["overview"]["narrative"]["text"] = json!("Final rejection narrative");
        },
    ));
    assert!(
        !app.polling_stopped(),
        "all final sections still need their final wave"
    );
    app.request_poll();
    let commands = app.take_commands();
    assert_eq!(sections(&commands), OperatorSection::ALL);
    for section in sections(&commands) {
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |page| {
                page["run_status"] = json!("failed");
                if section == OperatorSection::Overview {
                    page["overview"]["narrative"]["terminal"] = json!(true);
                    page["overview"]["narrative"]["status"] = json!("available");
                    page["overview"]["narrative"]["text"] = json!("Final rejection narrative");
                }
                if section == OperatorSection::Progress {
                    page["progress"]["narrative"]["text"] = json!("Final rejection narrative");
                }
            },
        ));
    }
    app.take_commands();
    assert_eq!(
        app.view
            .overview
            .as_ref()
            .unwrap()
            .narrative
            .text
            .as_deref(),
        Some("Final rejection narrative")
    );
    assert!(app.polling_stopped());
    app.request_poll();
    assert!(app.take_commands().is_empty());
    clock.advance(Duration::from_secs(60));
    assert_eq!(app.liveness(), Liveness::Idle);
}

#[test]
fn unavailable_terminal_narrative_and_failed_final_page_complete_without_busy_polling() {
    let (mut app, clock) = run_app("terminal-unavailable");
    app.handle_host_message(current(rejected_run()));
    for wave in 0..2 {
        app.request_poll();
        let commands = app.take_commands();
        for section in sections(&commands) {
            let request_id = section_request_id(&commands, section);
            if wave == 1 && section == OperatorSection::Progress {
                app.handle_host_message(HostMessage::OperatorView {
                    mission_run_id: RUN_ID.into(),
                    section,
                    request_id,
                    result: Err(HostError::Transport("disconnected".into())),
                });
            } else {
                app.handle_host_message(section_reply(section, request_id, None, |page| {
                    page["run_status"] = json!("failed");
                    if section == OperatorSection::Overview {
                        page["overview"]["narrative"]["terminal"] = json!(true);
                        page["overview"]["narrative"]["status"] = json!("unavailable");
                    }
                }));
            }
        }
        app.take_commands();
    }
    assert!(!app.polling_stopped(), "a final page has not completed");
    clock.advance(Duration::from_millis(400));
    app.request_poll();
    assert!(app.take_commands().is_empty());
    clock.advance(Duration::from_millis(1600));
    app.request_poll();
    let commands = app.take_commands();
    assert_eq!(sections(&commands), [OperatorSection::Progress]);
    app.handle_host_message(section_reply(
        OperatorSection::Progress,
        section_request_id(&commands, OperatorSection::Progress),
        None,
        |page| page["run_status"] = json!("failed"),
    ));
    assert_eq!(
        app.view.overview.as_ref().unwrap().narrative.status,
        "unavailable"
    );
    assert!(app.polling_stopped());
}

#[test]
fn rejection_dismissal_survives_refresh_and_relaunch_keeps_stack_selection() {
    let (mut app, _) = run_app("rejection-dismiss");
    let stack = app.launch.selection().unwrap();
    app.launch.editor.set_text("buy me a coffee");
    let mut rejecting = rejected_run();
    rejecting.status = "running".into();
    rejecting.finished_at = None;
    app.handle_host_message(current(rejecting));
    assert!(
        app.rejection_open(),
        "reason is visible during final stack cleanup"
    );
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(
        app.state,
        AppState::Run,
        "relaunch waits for the real terminal lifecycle"
    );
    assert!(app.rejection_open());
    app.handle_key(key(KeyCode::Enter));
    assert!(!app.rejection_open());
    app.handle_host_message(current(rejected_run()));
    assert!(
        !app.rejection_open(),
        "refresh must not reopen a dismissed card"
    );
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(app.state, AppState::Launch);
    assert_eq!(app.launch.selection(), Some(stack));
    assert_eq!(app.launch.editor.text(), "buy me a coffee");
    assert!(app.run.is_none());
}

#[test]
fn progress_search_captures_global_letters_and_tabs_but_not_managed_interrupt() {
    let (mut app, _) = run_app("progress-search");
    app.handle_key(key(KeyCode::Char('2')));
    app.handle_key(key(KeyCode::Char('/')));
    for code in [
        KeyCode::Char('q'),
        KeyCode::Char('c'),
        KeyCode::Char('3'),
        KeyCode::Tab,
    ] {
        app.handle_key(key(code));
    }
    assert_eq!(app.view.tab, RunTab::Progress);
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert!(!app.should_quit());
    assert!(app.view.progress.search_editing);
    app.handle_key(ctrl('c'));
    assert_eq!(app.cancellation, CancellationState::Confirming);
}

#[test]
fn stack_section_selects_a_service_and_tails_its_log_from_the_last_window() {
    let (mut app, _clock) = run_app("stack-tail");
    app.handle_key(key(KeyCode::Char('6')));
    let commands = app.take_commands();
    let id = section_request_id(&commands, OperatorSection::Stack);
    app.handle_host_message(section_reply(OperatorSection::Stack, id, None, |_| {}));
    let stack = &app.view.stack;
    assert_eq!(stack.selected.as_deref(), Some("physical-runtime"));
    assert_eq!(
        stack.tail.as_ref().unwrap().artifact_id,
        "service-log-physical-runtime"
    );
    app.request_poll();
    let (artifact_id, offset) = service_log_request(&app.take_commands()).unwrap();
    assert_eq!(
        (artifact_id.as_str(), offset),
        ("service-log-physical-runtime", 0)
    );
    app.handle_host_message(service_log_reply(0, &"x".repeat(4096), 20_480));
    app.request_poll();
    assert_eq!(
        service_log_request(&app.take_commands()).unwrap().1,
        20_480 - 4096
    );
    app.handle_host_message(service_log_reply(16_384, "cut\nviewer ready\n", 16_401));
    let lines: Vec<&str> = app
        .view
        .stack
        .tail
        .as_ref()
        .unwrap()
        .visible_lines()
        .collect();
    assert_eq!(lines, ["viewer ready"]);

    app.handle_key(key(KeyCode::Down));
    assert_eq!(app.view.stack.selected.as_deref(), Some("closed-loop"));
    let commands = app.take_commands();
    assert_eq!(
        service_log_request(&commands),
        Some(("worker-log".to_string(), 0))
    );
}

#[test]
fn agents_follow_newest_until_moved_then_count_newer_until_f() {
    let (mut app, _clock) = run_app("agents");
    app.handle_key(key(KeyCode::Char('3')));
    let id = section_request_id(&app.take_commands(), OperatorSection::Agents);
    app.handle_host_message(agents_reply(id, &["a-1", "a-2"]));
    assert_eq!(app.view.selected_invocation.as_deref(), Some("a-2"));
    app.handle_key(key(KeyCode::Up));
    assert!(!app.view.agent_following);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("a-1"));
    app.request_poll();
    let id = section_request_id(&app.take_commands(), OperatorSection::Agents);
    app.handle_host_message(agents_reply(id, &["a-3"]));
    assert_eq!(app.view.newer_invocations, 1);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("a-1"));
    app.handle_key(key(KeyCode::Char('f')));
    assert!(app.view.agent_following);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("a-3"));
}

#[test]
fn artifact_inspector_pages_forward_and_back_and_closes() {
    let (mut app, _clock) = run_app("inspector");
    app.handle_key(key(KeyCode::Char('7')));
    let id = section_request_id(&app.take_commands(), OperatorSection::Artifacts);
    app.handle_host_message(artifacts_reply(id));
    // Artifacts sort by id; "planner-…" precedes "report".
    let artifact = app.view.selected_artifact.clone().unwrap();
    assert!(artifact.starts_with("planner-"));
    app.handle_key(key(KeyCode::Enter));
    let commands = app.take_commands();
    assert!(matches!(
        &commands[..],
        [HostCommand::FetchArtifactContent {
            purpose: ContentPurpose::Inspector,
            offset: 0,
            ..
        }]
    ));
    app.handle_host_message(content_reply(&artifact, 0, "text-page"));
    app.handle_key(key(KeyCode::Right));
    assert_eq!(app.view.inspector.as_ref().unwrap().offset, 4096);
    app.take_commands();
    app.handle_host_message(content_reply(&artifact, 4096, "text-final"));
    app.handle_key(key(KeyCode::Right));
    assert!(app.take_commands().is_empty(), "no page after eof");
    app.handle_key(key(KeyCode::Left));
    assert_eq!(app.view.inspector.as_ref().unwrap().offset, 0);
    app.handle_key(key(KeyCode::Esc));
    assert!(app.view.inspector.is_none());
}

#[test]
fn e_after_a_terminal_run_returns_to_launch_with_the_same_stack() {
    let file = state_file("edit-new-intent");
    let (mut app, _clock) = ready_launch_app_with_file(file.clone());
    focus(&mut app, LaunchField::SimLimit);
    app.handle_key(key(KeyCode::Left));
    accept(&mut app);
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(app.state, AppState::Run, "e does nothing while running");
    app.handle_host_message(current(rejected_run()));
    app.take_commands();
    app.handle_key(key(KeyCode::Char('e')));
    assert_eq!(app.state, AppState::Launch);
    assert!(app.run.is_none());
    assert!(!file.path().exists(), "owner session is released");
    assert_eq!(app.launch.simulation_limit_seconds, 570);
    assert_eq!(app.launch.preset().unwrap().preset_id, "mission1-harbor");
    app.check_deadlines();
    let (_, query) = single_preflight(&mut app);
    assert_eq!(query.preset_id, "mission1-harbor");
    assert_eq!(query.toggles.perception, "off");
}

#[test]
fn resize_below_minimum_overlays_and_restores_the_state() {
    let (mut app, _clock) = run_app("resize");
    app.handle_resize(MIN_WIDTH - 1, MIN_HEIGHT);
    assert_eq!(app.state.name(), "ResizeRequired");
    assert_eq!(app.logical_state_name(), "Run");
    app.request_poll();
    assert!(!app.take_commands().is_empty(), "polling continues");
    app.handle_resize(MIN_WIDTH, MIN_HEIGHT);
    assert_eq!(app.state, AppState::Run);
}

// ---------------------------------------------------------------------------
// Cancellation and managed exit
// ---------------------------------------------------------------------------

#[test]
fn c_requires_confirmation_and_enqueues_one_cancellation() {
    let (mut app, _clock) = run_app("cancel");
    app.take_commands();
    app.handle_key(key(KeyCode::Char('c')));
    assert_eq!(app.cancellation, CancellationState::Confirming);
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.cancellation, CancellationState::Idle);
    app.handle_key(key(KeyCode::Char('c')));
    app.handle_key(key(KeyCode::Enter));
    app.handle_key(key(KeyCode::Enter));
    let cancels: Vec<_> = app
        .take_commands()
        .into_iter()
        .filter(|command| matches!(command, HostCommand::Cancel { .. }))
        .collect();
    assert_eq!(cancels.len(), 1);
    let HostCommand::Cancel { request, .. } = &cancels[0] else {
        unreachable!()
    };
    app.handle_host_message(cancelled(&request.cancellation_request_id));
    assert!(matches!(
        app.cancellation,
        CancellationState::Requested { .. }
    ));
    let mut cancelled_run = running_run();
    cancelled_run.status = "cancelled".to_string();
    app.handle_host_message(current(cancelled_run));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert_eq!(
        app.take_clean_exit_action(),
        None,
        "c keeps the console open"
    );
}

#[test]
fn mismatched_cancellation_acceptance_is_a_contract_failure() {
    let (mut app, _clock) = run_app("cancel-mismatch");
    app.handle_key(key(KeyCode::Char('c')));
    app.handle_key(key(KeyCode::Enter));
    app.handle_host_message(cancelled("someone-else"));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert!(app.notice.as_deref().unwrap().contains("contract failure"));
}

#[test]
fn q_cancels_then_exits_after_cancelled_or_at_the_fifteen_second_limit() {
    let file = state_file("q-exit");
    let (mut app, _clock) = run_app_with_file("q-exit", file.clone());
    app.handle_key(key(KeyCode::Char('q')));
    assert_eq!(app.cancellation, CancellationState::Confirming);
    app.handle_key(key(KeyCode::Enter));
    let request_id = cancel_request_id(&app.take_commands());
    app.handle_host_message(cancelled(&request_id));
    let mut cancelled_run = running_run();
    cancelled_run.status = "cancelled".to_string();
    app.handle_host_message(current(cancelled_run));
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::Cancelled)
    );
    assert!(!file.path().exists());

    let (mut app, clock) = run_app("q-timeout");
    app.handle_key(key(KeyCode::Char('q')));
    app.handle_key(key(KeyCode::Enter));
    app.handle_host_message(HostMessage::Cancelled(Err(HostError::Transport(
        "down".to_string(),
    ))));
    clock.advance(Duration::from_millis(14_999));
    app.check_deadlines();
    assert_eq!(app.take_clean_exit_action(), None);
    clock.advance(Duration::from_millis(1));
    app.check_deadlines();
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::CancellationTimedOut)
    );
}

#[test]
fn managed_exit_waits_for_valid_cancellation_beyond_ordinary_poll_timeout() {
    let (mut app, clock) = run_app("slow-cancellation");
    app.handle_key(key(KeyCode::Char('q')));
    app.handle_key(key(KeyCode::Enter));
    let request_id = cancel_request_id(&app.take_commands());
    clock.advance(Duration::from_secs(9));
    app.check_deadlines();
    assert_eq!(app.take_clean_exit_action(), None);
    app.handle_host_message(cancelled(&request_id));
    assert!(matches!(
        app.cancellation,
        CancellationState::Requested { .. }
    ));
    let mut run = running_run();
    run.status = "cancelled".into();
    app.handle_host_message(current(run));
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::Cancelled)
    );
}

#[test]
fn q_on_a_terminal_run_exits_directly() {
    let file = state_file("q-terminal");
    let (mut app, _clock) = run_app_with_file("q-terminal", file.clone());
    app.handle_host_message(current(rejected_run()));
    app.handle_key(key(KeyCode::Char('q')));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::TerminalRun)
    );
    assert!(!file.path().exists());
}

#[test]
fn ctrl_c_goes_through_managed_exit_during_an_owned_active_run() {
    let (mut app, _clock) = run_app("ctrl-c-run");
    app.handle_key(ctrl('c'));
    assert!(!app.should_quit());
    assert_eq!(app.cancellation, CancellationState::Confirming);
    app.handle_key(key(KeyCode::Enter));
    assert!(
        app.take_commands()
            .iter()
            .any(|command| matches!(command, HostCommand::Cancel { .. }))
    );

    let (mut app, _clock) = run_app("ctrl-c-twice");
    app.handle_key(ctrl('c'));
    app.handle_key(ctrl('c'));
    assert!(
        !app.should_quit(),
        "a second Ctrl+C must not silently detach"
    );
    assert_eq!(app.cancellation, CancellationState::Confirming);
    app.handle_key(key(KeyCode::Enter));
    assert!(
        app.take_commands()
            .iter()
            .any(|command| matches!(command, HostCommand::Cancel { .. }))
    );

    let (mut app, _clock) = run_app("ctrl-c-terminal");
    app.handle_host_message(current(rejected_run()));
    app.handle_key(ctrl('c'));
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::TerminalRun)
    );

    let (mut app, _clock) = ready_launch_app("ctrl-c-launch");
    app.handle_key(ctrl('c'));
    assert!(app.should_quit());

    let file = state_file("ctrl-q");
    let (mut app, clock) = run_app_with_file("ctrl-q", file.clone());
    let saved_owner = file.load().unwrap().unwrap();
    clock.advance(Duration::from_secs(31));
    app.handle_key(ctrl('q'));
    assert!(app.should_quit(), "Ctrl+Q detaches immediately");
    assert_eq!(
        app.take_clean_exit_action(),
        Some(CleanExitAction::Detached)
    );
    assert_eq!(file.load().unwrap(), Some(saved_owner));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert!(
        !app.take_commands()
            .iter()
            .any(|command| matches!(command, HostCommand::Cancel { .. }))
    );

    let (mut app, clock) = run_app("ctrl-c-offline-owner");
    clock.advance(Duration::from_secs(31));
    app.handle_key(ctrl('c'));
    assert!(
        !app.should_quit(),
        "offline ownership still requires explicit detach"
    );
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert!(app.notice.as_deref().unwrap().contains("Ctrl+Q"));
}

// ---------------------------------------------------------------------------
// Ownership and liveness
// ---------------------------------------------------------------------------

#[test]
fn liveness_uses_inclusive_thresholds_and_gates_mutations() {
    let (app, clock) = run_app("liveness");
    let mut app = app.with_liveness_thresholds(LivenessThresholds {
        stale: Duration::from_secs(5),
        offline: Duration::from_secs(30),
    });
    app.handle_host_message(current(running_run()));
    assert_eq!(app.liveness(), Liveness::Live);
    assert!(app.mutations_enabled());
    clock.advance(Duration::from_secs(5));
    assert_eq!(app.liveness(), Liveness::Stale);
    assert!(!app.mutations_enabled());
    app.handle_key(key(KeyCode::Char('c')));
    assert_eq!(app.cancellation, CancellationState::Idle);
    assert!(app.notice.is_some());
    clock.advance(Duration::from_secs(25));
    assert_eq!(app.liveness(), Liveness::Offline);
    app.handle_host_message(HostMessage::Current(Err(HostError::Transport(
        "down".to_string(),
    ))));
    assert_eq!(
        app.liveness(),
        Liveness::Offline,
        "transport errors prove nothing"
    );
    app.handle_host_message(HostMessage::Current(Err(HostError::UnexpectedStatus(
        500,
        "boom".to_string(),
    ))));
    assert_eq!(app.liveness(), Liveness::Live);
}

#[test]
fn observers_of_a_run_they_do_not_own_cannot_cancel() {
    let (mut app, _clock) = ready_launch_app("observer");
    app.state = AppState::Run;
    app.handle_host_message(current(running_run()));
    assert!(!app.ownership_available());
    app.handle_key(key(KeyCode::Char('c')));
    assert_eq!(app.cancellation, CancellationState::Idle);
    app.handle_key(ctrl('c'));
    assert!(app.should_quit(), "nothing to cancel: Ctrl+C detaches");
}

// ---------------------------------------------------------------------------
// Session persistence and recovery
// ---------------------------------------------------------------------------

fn owner(run_id: &str, credential: &str) -> OwnerSessionState {
    OwnerSessionState {
        host_authority: HOST.to_string(),
        host_api_major: 1,
        mission_run_id: run_id.to_string(),
        console_session_id: "session-recovered".to_string(),
        credential: credential.to_string(),
    }
}

#[test]
fn session_state_file_is_atomic_owner_only_and_round_trips_only_authority_fields() {
    let root = scratch_dir("round-trip");
    let file = SessionStateFile::at(root.join("onr/operator-console/session.json"));
    let state = owner("run-1", "credential-1");
    file.save(&state).unwrap();
    assert_eq!(file.load().unwrap(), Some(state));
    let value: serde_json::Value = serde_json::from_slice(&fs::read(file.path()).unwrap()).unwrap();
    assert_eq!(value.as_object().unwrap().len(), 5);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode =
            |path: &std::path::Path| fs::metadata(path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(file.path()), 0o600);
        assert_eq!(mode(&root.join("onr")), 0o700);
        assert_eq!(mode(&root.join("onr/operator-console")), 0o700);
    }
    file.remove().unwrap();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn session_save_hardens_the_final_directory_without_chmodding_ancestors() {
    let root = scratch_dir("existing-state");
    let parent = root.join("operator-console");
    fs::create_dir_all(&parent).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o755)).unwrap();
    }
    SessionStateFile::at(parent.join("session.json"))
        .save(&owner("run-1", "credential-1"))
        .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode =
            |path: &std::path::Path| fs::metadata(path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(&parent), 0o700);
        assert_eq!(mode(&root), 0o755);
    }
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn recovered_owner_reads_intent_through_host_authorization_and_enters_run() {
    let file = state_file("recover");
    file.save(&owner(RUN_ID, "credential-recovered")).unwrap();
    let mut app = App::new_with_session_file(HOST.to_string(), file.clone());
    assert_eq!(app.session.session_id, "session-recovered");
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    let commands = app.take_commands();
    assert!(commands.contains(&HostCommand::FetchIntent {
        mission_run_id: RUN_ID.to_string(),
        credential: "credential-recovered".to_string(),
    }));
    assert!(!commands.contains(&HostCommand::FetchPresets));
    app.handle_host_message(HostMessage::Intent(Ok(MissionIntent {
        mission_run_id: RUN_ID.to_string(),
        mission_intent: "hold the ridge".to_string(),
        source_authority: "operator_console".to_string(),
    })));
    app.handle_host_message(current(running_run()));
    assert_eq!(app.logical_state_name(), "Run");
    assert_eq!(app.launch.editor.text(), "hold the ridge");
    assert!(app.recovered_owner());
    assert!(app.mutations_enabled());
    file.remove().unwrap();
}

#[test]
fn recovered_owner_rejects_a_mismatched_current_run_and_keeps_the_record() {
    let file = state_file("recover-mismatch");
    file.save(&owner("run-owned", "credential")).unwrap();
    let mut app = App::new_with_session_file(HOST.to_string(), file.clone());
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.handle_host_message(current(running_run()));
    assert_eq!(app.logical_state_name(), "Launch");
    assert!(app.run.is_none());
    assert!(file.path().exists());
    assert!(app.notice.as_deref().unwrap().contains(RUN_ID));
    assert!(!app.recovered_owner());
    assert!(!app.ownership_available());
    app.take_commands();
    app.handle_host_message(HostMessage::Presets(Ok(presets())));
    app.check_deadlines();
    let (request_id, query) = single_preflight(&mut app);
    app.handle_host_message(HostMessage::Preflight {
        request_id,
        result: Ok(preflight(&query, true)),
    });
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::ReviewActivation);
    file.remove().unwrap();
}

#[test]
fn stale_recovery_authorization_keeps_the_record_and_reports_an_error() {
    let file = state_file("recover-stale");
    file.save(&owner("run-owned", "stale")).unwrap();
    let mut app = App::new_with_session_file(HOST.to_string(), file.clone());
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.handle_host_message(HostMessage::Intent(Err(HostError::AuthorizationFailed {
        code: "authorization_failed".to_string(),
        message: "request is not authorized".to_string(),
    })));
    assert!(file.path().exists());
    assert!(matches!(app.state, AppState::Error { .. }));
    file.remove().unwrap();
}

fn cancelled(request_id: &str) -> HostMessage {
    HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(CancellationAccepted {
        mission_run_id: RUN_ID.to_string(),
        cancellation_request_id: request_id.to_string(),
        disposition: "cancellation_requested".to_string(),
        status: "running".to_string(),
        requested_at: "2026-08-24T12:00:05Z".to_string(),
    })))
}

fn cancel_request_id(commands: &[HostCommand]) -> String {
    commands
        .iter()
        .find_map(|command| match command {
            HostCommand::Cancel { request, .. } => Some(request.cancellation_request_id.clone()),
            _ => None,
        })
        .expect("a cancellation was requested")
}

fn current(run: operator_console::host::RunRecord) -> HostMessage {
    HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(run),
    }))
}

fn ctrl(c: char) -> crossterm::event::KeyEvent {
    crossterm::event::KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
}

#[test]
fn absent_recovered_run_preserves_record_and_can_activate_a_new_mission() {
    let file = state_file("recover-absent");
    let saved = owner("run-gone", "credential-recovered");
    file.save(&saved).unwrap();
    let mut app = App::new_with_session_file(HOST.to_string(), file.clone());
    app.take_commands();
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.take_commands();
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun { mission_run: None })));
    assert_eq!(app.logical_state_name(), "Launch");
    assert!(!app.recovered_owner());
    assert!(!app.ownership_available());
    assert_eq!(file.load().unwrap(), Some(saved));
    app.take_commands();
    app.handle_host_message(HostMessage::Presets(Ok(presets())));
    app.check_deadlines();
    let (request_id, query) = single_preflight(&mut app);
    app.handle_host_message(HostMessage::Preflight {
        request_id,
        result: Ok(preflight(&query, true)),
    });
    app.handle_key(alt_enter());
    assert_eq!(app.state, AppState::ReviewActivation);
    app.handle_key(key(KeyCode::Enter));
    let commands = app.take_commands();
    assert!(
        commands
            .iter()
            .any(|command| matches!(command, HostCommand::Submit { .. }))
    );
    app.handle_host_message(HostMessage::Activated(Ok(
        operator_console::host::ActivationOutcome::Accepted(
            operator_console::host::ActivationAccepted {
                activation_request_id: app.review_request_id().unwrap().to_string(),
                mission_id: MISSION_ID.to_string(),
                mission_run_id: RUN_ID.to_string(),
                status: "queued".to_string(),
                created_at: "2026-08-24T12:00:00Z".to_string(),
            },
        ),
    )));
    assert_eq!(app.logical_state_name(), "Run");
    assert_eq!(file.load().unwrap().unwrap().mission_run_id, RUN_ID);
    assert!(app.ownership_available());
    file.remove().unwrap();
}

#[test]
fn dropped_section_retries_without_host_failure_and_final_wave_completes() {
    use operator_console::host::workers::Dispatch;
    let (mut app, clock) = run_app("dropped-final-section");
    app.handle_host_message(current(rejected_run()));
    let mut dropped = false;
    for _ in 0..4 {
        app.request_poll();
        let commands = app.take_commands();
        for command in commands {
            if let HostCommand::FetchOperatorView {
                section,
                request_id,
                ..
            } = &command
            {
                if *section == OperatorSection::Agents && !dropped {
                    app.handle_dispatch(Dispatch::Dropped(command));
                    dropped = true;
                    assert_eq!(app.notice, None);
                    assert_eq!(app.liveness(), Liveness::Live);
                    continue;
                }
                app.handle_host_message(section_reply(*section, *request_id, None, |page| {
                    page["run_status"] = json!("failed");
                    if *section == OperatorSection::Overview {
                        page["overview"]["narrative"]["terminal"] = json!(true);
                        page["overview"]["narrative"]["status"] = json!("available");
                    }
                }));
            }
        }
        if app.polling_stopped() {
            break;
        }
        clock.advance(Duration::from_secs(2));
    }
    assert!(dropped);
    assert!(
        app.polling_stopped(),
        "local backpressure must not strand a final section"
    );
}

#[test]
fn coalesced_section_accepts_original_response_id_and_can_poll_again() {
    use operator_console::host::workers::Dispatch;
    let (mut app, _) = run_app("coalesced-section-owner");
    app.request_poll();
    let command = app
        .take_commands()
        .into_iter()
        .find(|command| {
            matches!(
                command,
                HostCommand::FetchOperatorView {
                    section: OperatorSection::Progress,
                    ..
                }
            )
        })
        .unwrap();
    let original = 987;
    app.handle_dispatch(Dispatch::Coalesced {
        command,
        original_request_id: Some(original),
    });
    app.handle_host_message(section_reply(
        OperatorSection::Progress,
        original,
        None,
        |page| {
            page["progress"]["nodes"][0]["title"] = json!("Coalesced response applied");
        },
    ));
    assert_eq!(
        app.view.progress.nodes["summary:7"].title,
        "Coalesced response applied"
    );
    app.request_poll();
    assert!(sections(&app.take_commands()).contains(&OperatorSection::Progress));
}

#[test]
fn initial_agents_and_artifacts_history_drains_before_terminal_completion() {
    use operator_console::host::OperatorCursor;
    for section in [OperatorSection::Agents, OperatorSection::Artifacts] {
        let (mut app, clock) = ready_launch_app("history-lists");
        accept(&mut app);
        let commands = app.take_commands();
        for other in sections(&commands) {
            if other != section {
                app.handle_host_message(section_reply(
                    other,
                    section_request_id(&commands, other),
                    None,
                    |_| {},
                ));
            }
        }
        let field = if section == OperatorSection::Agents {
            "agents"
        } else {
            "artifacts"
        };
        let id_field = if section == OperatorSection::Agents {
            "stable_id"
        } else {
            "artifact_id"
        };
        let items = |page: &mut serde_json::Value, range: std::ops::Range<u64>| {
            let template = page[field][0].clone();
            page[field] = json!(
                range
                    .map(|n| {
                        let mut item = template.clone();
                        item[id_field] = json!(format!("history-{n:03}"));
                        if section == OperatorSection::Artifacts {
                            item["classification"] = json!("service_log");
                        }
                        item
                    })
                    .collect::<Vec<_>>()
            );
        };
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |page| {
                page["next_cursor"] = json!("history-120");
                page["before_cursor"] = json!("history-21");
                page["has_more"] = json!(true);
                items(page, 21..121);
            },
        ));
        if section == OperatorSection::Agents {
            app.view.agent_following = false;
            app.view.selected_invocation = Some("history-100".to_string());
        } else {
            app.view.selected_artifact = Some("history-100".to_string());
        }
        let commands = app.take_commands();
        assert!(commands.iter().any(|command| matches!(command, HostCommand::FetchOperatorView { cursor: OperatorCursor::Before(cursor), .. } if cursor == "history-21")));
        // The run becomes terminal while its initial history is still draining.
        app.handle_host_message(current(rejected_run()));
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |page| {
                page["run_status"] = json!("failed");
                page["next_cursor"] = json!("terminal-150");
                items(page, 1..21);
            },
        ));
        assert!(!app.polling_stopped());
        let commands = app.take_commands();
        assert!(commands.iter().any(|command| matches!(command, HostCommand::FetchOperatorView { cursor: OperatorCursor::After(cursor), .. } if cursor == "history-120")));
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |page| {
                page["run_status"] = json!("failed");
                page["next_cursor"] = json!("terminal-150");
                items(page, 121..151);
            },
        ));
        for _ in 0..3 {
            app.request_poll();
            let commands = app.take_commands();
            for other in sections(&commands) {
                app.handle_host_message(section_reply(
                    other,
                    section_request_id(&commands, other),
                    None,
                    |page| {
                        page["run_status"] = json!("failed");
                        if other == section {
                            page[field] = json!([]);
                        }
                        if other == OperatorSection::Overview {
                            page["overview"]["narrative"]["terminal"] = json!(true);
                            page["overview"]["narrative"]["status"] = json!("available");
                        }
                    },
                ));
            }
            if app.polling_stopped() {
                break;
            }
            clock.advance(Duration::from_secs(2));
        }
        assert!(app.polling_stopped());
        if section == OperatorSection::Agents {
            assert_eq!(app.view.agents.len(), 150);
            assert_eq!(app.view.selected_invocation.as_deref(), Some("history-100"));
            assert!(!app.view.agent_following);
        } else {
            assert_eq!(app.view.artifacts.len(), 150);
            assert_eq!(app.view.selected_artifact.as_deref(), Some("history-100"));
        }
    }
}
