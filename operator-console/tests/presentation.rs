//! Issue #76 U11: the F4 presentation layout. F4 toggles it over the
//! selected tab and back; `v` cycles the evidence card; `p` freezes the
//! display while polling continues, and resuming shows the latest evidence;
//! failure, Human Decision and Host-offline alerts break through a frozen
//! view; a historical run is presented read-only.

mod common;

use std::time::Duration;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::{Alert, App, EvidenceCard, RunTab};
use operator_console::host::{
    CurrentRun, Fetched, FrameSource, HostCommand, HostMessage, OperatorSection, RunRecord,
    WorldFrame,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use ratatui_image::picker::Picker;

fn screen(app: &mut App, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| operator_console::ui::draw(frame, app))
        .unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| {
            (0..width)
                .map(|x| buffer[(x, y)].symbol().to_string())
                .collect::<String>()
                .trim_end()
                .to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Answer the section requests in `commands`, edited.
fn answer(
    app: &mut App,
    commands: &[HostCommand],
    edit: impl Fn(OperatorSection, &mut serde_json::Value),
) {
    for section in sections(commands) {
        let id = section_request_id(commands, section);
        app.handle_host_message(section_reply(section, id, None, |page| edit(section, page)));
    }
}

/// An owned running run with every section answered once.
fn running_app(name: &str) -> (App, std::sync::Arc<ManualClock>) {
    let (mut app, clock) = run_app(name);
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(running_run()),
    })));
    app.request_poll();
    let commands = app.take_commands();
    answer(&mut app, &commands, |_, _| {});
    (app, clock)
}

/// The fixture's agents page with one newer invocation `name`, which an
/// Agents view following the newest invocation selects.
fn newer_invocation(section: OperatorSection, page: &mut serde_json::Value) {
    if section == OperatorSection::Agents {
        let agent = &mut page["agents"][0];
        agent["stable_id"] = serde_json::json!("hyper-agent:invocation-9");
        agent["invocation_id"] = serde_json::json!("invocation-9");
        agent["name"] = serde_json::json!("replan_after_report");
        agent["started_at"] = serde_json::json!("2026-08-27T12:05:00Z");
    }
}

fn press(app: &mut App, code: KeyCode) {
    app.handle_key(key(code));
}

#[test]
fn f4_shows_the_layout_over_the_tab_and_toggles_back_to_it() {
    let (mut app, _clock) = running_app("presentation-toggle");
    press(&mut app, KeyCode::Char('3'));
    app.take_commands();
    press(&mut app, KeyCode::F(4));
    assert!(app.presenting());
    assert_eq!(
        app.view.tab,
        RunTab::Agents,
        "the tab stays selected behind"
    );
    let shown = screen(&mut app, 100, 30);
    assert!(shown.contains(" PRESENTATION  Mission 1 · Harbor (simulated)"));
    assert!(shown.contains("Wall 12:00:43 UTC · run 00:00:40 │ Mission t=12.5 s"));
    assert!(!shown.contains("3 Agents "), "no tab bar: {shown}");
    assert!(shown.contains("F4 back to tabs · v next card · p freeze · s source"));

    press(&mut app, KeyCode::F(4));
    assert!(!app.presenting());
    assert_eq!(app.view.tab, RunTab::Agents);
    let shown = screen(&mut app, 100, 30);
    assert!(shown.contains(" 3 Agents "));
    assert!(!shown.contains("PRESENTATION"));
}

#[test]
fn a_tab_key_leaves_the_layout_for_that_tab() {
    let (mut app, _clock) = running_app("presentation-tab-key");
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('4'));
    assert!(!app.presenting());
    assert_eq!(app.view.tab, RunTab::BeliefContext);
    // The tab that was selected behind the layout also leaves it.
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('4'));
    assert!(!app.presenting());
    assert_eq!(app.view.tab, RunTab::BeliefContext);
}

#[test]
fn presentation_polls_its_sections_and_never_rotates_tabs() {
    let (mut app, clock) = running_app("presentation-polls");
    press(&mut app, KeyCode::Char('6'));
    let commands = app.take_commands();
    answer(&mut app, &commands, |_, _| {});
    press(&mut app, KeyCode::F(4));
    let commands = app.take_commands();
    let mut requested = sections(&commands);
    requested.sort_by_key(|section| section.index());
    assert_eq!(
        requested,
        [
            OperatorSection::Overview,
            OperatorSection::Progress,
            OperatorSection::Agents,
            OperatorSection::Beliefs,
            OperatorSection::World,
        ]
    );
    answer(&mut app, &commands, |_, _| {});
    for _ in 0..5 {
        clock.advance(Duration::from_secs(1));
        app.request_poll();
        let commands = app.take_commands();
        answer(&mut app, &commands, |_, _| {});
        assert!(app.presenting());
        assert_eq!(app.view.tab, RunTab::Stack);
        assert_eq!(app.view.presentation.card, EvidenceCard::Milestone);
    }
}

#[test]
fn v_cycles_the_evidence_card_through_the_existing_selections() {
    let (mut app, _clock) = running_app("presentation-cards");
    press(&mut app, KeyCode::F(4));
    let commands = app.take_commands();
    answer(&mut app, &commands, |_, _| {});
    let shown = screen(&mut app, 160, 45);
    assert!(shown.contains("Milestone · v Belief entity"));
    assert!(shown.contains("2 Progress selection · following newest"));
    assert!(shown.contains("Phase 5/6 · Executing (plan revision 3)"));
    assert!(shown.contains("FSM patrol.leg_2 → patrol.leg_3"));
    press(&mut app, KeyCode::Char('v'));
    let shown = screen(&mut app, 160, 45);
    assert!(shown.contains("Belief entity · v Agent invocation"));
    assert!(shown.contains("4 Belief/Context selection"));
    press(&mut app, KeyCode::Char('v'));
    let shown = screen(&mut app, 160, 45);
    assert!(shown.contains("Agent invocation · v Milestone"));
    assert!(shown.contains("HYP planner_executor · complete"));
    press(&mut app, KeyCode::Char('v'));
    assert_eq!(app.view.presentation.card, EvidenceCard::Milestone);
}

#[test]
fn freeze_holds_the_card_while_new_evidence_arrives_and_resume_shows_the_latest() {
    let (mut app, clock) = running_app("presentation-freeze");
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('v'));
    press(&mut app, KeyCode::Char('v'));
    let commands = app.take_commands();
    answer(&mut app, &commands, |_, _| {});
    press(&mut app, KeyCode::Char('p'));
    assert!(app.view.presentation.is_frozen());

    // Polling continues while frozen: a newer invocation and Mission time
    // arrive, 20 s later.
    clock.advance(Duration::from_secs(20));
    app.request_poll();
    let commands = app.take_commands();
    assert!(sections(&commands).contains(&OperatorSection::Agents));
    answer(&mut app, &commands, |section, page| {
        newer_invocation(section, page);
        if section == OperatorSection::Overview {
            page["overview"]["environment"]["mission_time_seconds"] = serde_json::json!(151.0);
        }
    });
    assert_eq!(
        app.view.selected_invocation.as_deref(),
        Some("hyper-agent:invocation-9"),
        "the live view moved on"
    );
    let frozen = screen(&mut app, 160, 45);
    assert!(frozen.contains("FROZEN AT 12:00:43 UTC · t=12.5 s · run 00:00:40"));
    assert!(frozen.contains("HYP planner_executor · complete"));
    assert!(!frozen.contains("replan_after_report"));
    assert!(frozen.contains("F4 back to tabs · v next card · p resume · ? help"));
    // The card kind can still change; it shows the frozen copy.
    press(&mut app, KeyCode::Char('v'));
    press(&mut app, KeyCode::Char('v'));
    press(&mut app, KeyCode::Char('v'));
    assert!(screen(&mut app, 160, 45).contains("HYP planner_executor · complete"));

    press(&mut app, KeyCode::Char('p'));
    assert!(!app.view.presentation.is_frozen());
    let live = screen(&mut app, 160, 45);
    assert!(live.contains("HYP replan_after_report · complete"));
    assert!(live.contains("Wall 12:01:03 UTC · run 00:01:00 │ Mission t=151.0 s"));
    assert!(!live.contains("FROZEN"));
}

/// Freeze holds the frame with the World `p` pause: no frame fetch while
/// frozen, a reply already in flight is dropped, and resuming restores the
/// pause state found and fetches again.
#[test]
fn freeze_holds_the_frame_through_the_world_pause_and_resume_restores_it() {
    let (mut app, clock) = running_app("presentation-frame");
    app.configure_images(Some(Picker::halfblocks()));
    press(&mut app, KeyCode::F(4));
    let fetches = |commands: &[HostCommand]| {
        commands
            .iter()
            .filter(|command| matches!(command, HostCommand::FetchWorldFrame { .. }))
            .count()
    };
    assert_eq!(fetches(&app.take_commands()), 1, "the layout shows a frame");
    press(&mut app, KeyCode::Char('p'));
    assert!(app.view.media.paused);
    app.handle_host_message(HostMessage::WorldFrame {
        mission_run_id: RUN_ID.into(),
        source: FrameSource::World,
        result: Ok(Fetched::Fresh {
            value: WorldFrame {
                source: FrameSource::World,
                media_type: "image/png".into(),
                etag: Some("frame:9".into()),
                sequence: Some(9),
                mission_time: Some("14.5".into()),
                bytes: vec![1, 2, 3],
            },
            etag: Some("frame:9".into()),
        }),
    });
    assert!(app.view.frame.is_none(), "the in-flight frame is dropped");
    clock.advance(Duration::from_secs(2));
    app.request_visible_frame();
    app.request_poll();
    let commands = app.take_commands();
    assert_eq!(fetches(&commands), 0, "no frame fetch while frozen");
    assert!(!sections(&commands).is_empty(), "sections keep polling");
    assert_eq!(
        screen(&mut app, 100, 30).lines().nth(3).unwrap(),
        "┌ world · seq - · frozen ────────────────────────────────┐┌ Milestone · v Belief entity ───────────┐"
    );
    press(&mut app, KeyCode::Char('p'));
    assert!(!app.view.media.paused);
    assert_eq!(
        fetches(&app.take_commands()),
        1,
        "resume fetches the latest"
    );

    // A World `p` pause found by the freeze is restored, not lifted.
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('5'));
    press(&mut app, KeyCode::Char('p'));
    assert!(app.view.media.paused);
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('p'));
    press(&mut app, KeyCode::Char('p'));
    assert!(app.view.media.paused);
    // `s` waits for the resume.
    press(&mut app, KeyCode::Char('p'));
    press(&mut app, KeyCode::Char('s'));
    assert_eq!(app.view.frame_source, FrameSource::World);
    assert_eq!(
        app.hint.as_deref(),
        Some("Frozen: p resumes before s changes the source")
    );
}

#[test]
fn a_failure_alert_interrupts_a_frozen_view() {
    let (mut app, _clock) = running_app("presentation-failure");
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('p'));
    let before = screen(&mut app, 100, 30);
    assert!(app.presentation_alerts().is_empty());
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(stack_failed_run()),
    })));
    assert!(matches!(
        app.presentation_alerts().as_slice(),
        [Alert::Failure { label, historical: false, .. }] if label == "stack_failed"
    ));
    let shown = screen(&mut app, 100, 30);
    assert!(shown.contains("✖ RUN FAILED · stack_failed · Stack service perception failed"));
    assert!(
        shown.contains("╔ ✖ RUN FAILED · stack_failed"),
        "the failure card: {shown}"
    );
    assert!(app.view.presentation.is_frozen(), "the display stays held");
    assert!(shown.contains("FROZEN AT 12:00:43 UTC"));
    assert_eq!(
        before.lines().nth(2),
        shown.lines().nth(2),
        "the frozen stepper"
    );
    // Enter dismisses the card; the alert row stays.
    press(&mut app, KeyCode::Enter);
    let shown = screen(&mut app, 100, 30);
    assert!(!shown.contains("╔ ✖ RUN FAILED"));
    assert!(shown.contains("✖ RUN FAILED · stack_failed"));
}

#[test]
fn a_human_decision_alert_interrupts_a_frozen_view() {
    let (mut app, _clock) = running_app("presentation-hitl");
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('p'));
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            status: "awaiting_human_decision".to_string(),
            ..running_run()
        }),
    })));
    assert_eq!(
        app.presentation_alerts(),
        [Alert::HumanDecision { historical: false }]
    );
    let shown = screen(&mut app, 160, 45);
    assert!(shown.contains(
        "◆ HUMAN DECISION REQUIRED · the Mission Run is paused, awaiting a Human Decision (status only)"
    ));
    assert!(shown.contains("FROZEN AT 12:00:43 UTC"));
}

#[test]
fn a_host_offline_alert_interrupts_a_frozen_view() {
    let (mut app, clock) = running_app("presentation-offline");
    press(&mut app, KeyCode::F(4));
    press(&mut app, KeyCode::Char('p'));
    clock.advance(Duration::from_secs(6));
    assert_eq!(app.presentation_alerts(), [Alert::HostStale]);
    clock.advance(Duration::from_secs(30));
    assert_eq!(app.presentation_alerts(), [Alert::HostOffline]);
    let shown = screen(&mut app, 100, 30);
    assert!(
        shown.contains("✖ HOST OFFLINE · no Host response; showing the last received evidence")
    );
    assert!(shown.contains("│ host ✖ offline"));
    assert!(shown.contains("FROZEN AT 12:00:43 UTC"));
}

/// A historical run is presented read-only; the alert row follows the
/// current run, and Esc returns to the current run's own view.
#[test]
fn a_historical_run_is_presented_read_only_with_current_run_alerts() {
    let (mut app, _clock) = running_app("presentation-historical");
    app.health = Some(health_v1_5());
    open_history(&mut app);
    select_history_row(&mut app, HISTORICAL_RUN_ID);
    press(&mut app, KeyCode::Enter);
    let commands = app.take_commands();
    answer_sections_for(
        &mut app,
        &commands,
        HISTORICAL_RUN_ID,
        "succeeded",
        |_, _| {},
    );
    press(&mut app, KeyCode::F(4));
    assert!(app.presenting());
    let shown = screen(&mut app, 160, 45);
    assert!(shown.contains(" HISTORICAL   PRESENTATION "), "{shown}");
    assert!(shown.contains("HISTORICAL read-only · Esc back to current run"));
    press(&mut app, KeyCode::Char('c'));
    assert!(
        app.hint
            .as_deref()
            .unwrap()
            .starts_with("Historical run: read-only")
    );
    assert!(
        app.take_commands()
            .iter()
            .all(|command| !matches!(command, HostCommand::Cancel { .. }))
    );

    // The current run, parked behind, asks for a Human Decision.
    app.handle_host_message(HostMessage::Current(Ok(CurrentRun {
        mission_run: Some(RunRecord {
            status: "awaiting_human_decision".to_string(),
            ..running_run()
        }),
    })));
    assert_eq!(
        app.presentation_alerts(),
        [Alert::HumanDecision { historical: true }]
    );
    assert!(screen(&mut app, 160, 45).contains("the current Mission Run is paused"));

    press(&mut app, KeyCode::Esc);
    assert!(!app.viewing_history());
    assert!(!app.presenting(), "the current run was not presented");
}
