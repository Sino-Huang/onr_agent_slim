//! Run Narrative freshness and coverage: the header names the narrative's age
//! (ticking with wall time between polls), the operational record it covers,
//! and how many records are newer. The newer count comes only from the
//! Host's `latest_operational_sequence` (API v1.5); older Hosts get no count.

mod common;

use std::time::Duration;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::App;
use operator_console::host::ProgressNarrative;
use operator_console::ui::progress::narrative_header;
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use serde_json::json;

/// 12:00:43, 12 s after the v1.5 example's `generated_at`.
const NOW: i64 = UNIX_ORIGIN;

fn narrative(watermark: u64, latest: Option<u64>, terminal: Option<bool>) -> ProgressNarrative {
    ProgressNarrative {
        status: "available".to_string(),
        text: Some("Legs 1-2 complete.".to_string()),
        generated_at: Some("2026-08-24T12:00:31Z".to_string()),
        source_watermark: watermark,
        terminal,
        latest_operational_sequence: latest,
    }
}

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
                .trim_end()
                .to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn newer_count_is_the_latest_sequence_minus_the_watermark() {
    let header = |watermark, latest, terminal| {
        narrative_header(Some(&narrative(watermark, latest, terminal)), NOW)
    };
    assert_eq!(
        header(763, Some(781), Some(false)),
        "AI narrative · 12 s old · through record #763 · 18 newer"
    );
    // Caught up: an explicit zero, not a missing count.
    assert_eq!(
        header(781, Some(781), Some(false)),
        "AI narrative · 12 s old · through record #781 · 0 newer"
    );
    // An older Host omits the newest sequence: no count rather than a wrong one.
    assert_eq!(
        header(412, None, None),
        "AI narrative · 12 s old · through record #412"
    );
    // The final narrative names itself; it covers every record, so no count.
    assert_eq!(
        header(781, Some(781), Some(true)),
        "AI narrative · final · 12 s old · through record #781"
    );
    // Records that arrived after the final attempt are still counted.
    assert_eq!(
        header(779, Some(781), Some(true)),
        "AI narrative · final · 12 s old · through record #779 · 2 newer"
    );
    assert_eq!(narrative(763, Some(781), None).newer_records(), Some(18));
    assert_eq!(narrative(781, Some(781), None).newer_records(), Some(0));
    assert_eq!(narrative(763, None, None).newer_records(), None);
}

#[test]
fn header_names_pending_and_unavailable_coverage_explicitly() {
    assert_eq!(
        narrative_header(None, NOW),
        "AI narrative · coverage pending"
    );
    let none = ProgressNarrative {
        status: "none".to_string(),
        text: None,
        generated_at: None,
        source_watermark: 0,
        terminal: Some(false),
        latest_operational_sequence: Some(5),
    };
    assert_eq!(
        narrative_header(Some(&none), NOW),
        "AI narrative · coverage pending"
    );
    let unavailable = ProgressNarrative {
        status: "unavailable".to_string(),
        text: None,
        terminal: Some(true),
        ..narrative(763, Some(781), None)
    };
    assert_eq!(
        narrative_header(Some(&unavailable), NOW),
        "AI narrative · final · unavailable · attempted 12 s ago"
    );
    // Ages beyond two minutes read in minutes, then hours.
    assert_eq!(
        narrative_header(Some(&narrative(763, Some(781), None)), NOW + 288),
        "AI narrative · 5 min old · through record #763 · 18 newer"
    );
    assert_eq!(
        narrative_header(Some(&narrative(763, Some(781), None)), NOW + 7_188),
        "AI narrative · 2 h old · through record #763 · 18 newer"
    );
}

#[test]
fn age_ticks_with_wall_time_between_polls_on_overview_and_progress() {
    let (mut app, clock) = hydrated_run_app("narrative-age", v1_5_progress);
    let overview = screen(&mut app, 160, 45);
    assert!(overview.contains("AI narrative · 12 s old · through record #763 · 18 newer"));

    clock.advance(Duration::from_secs(30));
    assert!(app.take_commands().is_empty(), "no poll was answered");
    assert!(screen(&mut app, 160, 45).contains("AI narrative · 42 s old"));

    app.handle_key(key(KeyCode::Char('2')));
    let progress = screen(&mut app, 100, 30);
    assert!(progress.contains("◆ AI narrative · 42 s old · through record #763 · 18 newer"));
}

#[test]
fn newer_count_follows_the_host_as_records_arrive_and_the_narrative_catches_up() {
    let (mut app, _clock) = hydrated_run_app("narrative-newer", v1_5_progress);
    let reply = |app: &mut App, watermark: u64, latest: u64| {
        app.request_poll();
        let commands = app.take_commands();
        let id = section_request_id(&commands, operator_console::host::OperatorSection::Progress);
        app.handle_host_message(section_reply(
            operator_console::host::OperatorSection::Progress,
            id,
            None,
            |page| {
                v1_5_progress(page);
                page["progress"]["nodes"] = json!([]);
                page["progress"]["narrative"]["source_watermark"] = json!(watermark);
                page["progress"]["narrative"]["latest_operational_sequence"] = json!(latest);
            },
        ));
    };
    reply(&mut app, 763, 790);
    assert!(screen(&mut app, 160, 45).contains("through record #763 · 27 newer"));
    reply(&mut app, 790, 790);
    assert!(screen(&mut app, 160, 45).contains("through record #790 · 0 newer"));
}

#[test]
fn an_older_host_shows_coverage_without_a_newer_count() {
    // The v1.2 example has no `latest_operational_sequence`.
    let (mut app, _clock) = hydrated_run_app("narrative-older-host", |_| {});
    let overview = screen(&mut app, 160, 45);
    assert!(overview.contains("through record #412"));
    assert!(!overview.contains("newer"));
}

#[test]
fn overview_preview_caps_the_narrative_and_points_to_progress() {
    let (mut app, _clock) = hydrated_run_app("narrative-cap", v1_5_progress);
    for (width, height) in [(100, 30), (160, 45)] {
        let overview = screen(&mut app, width, height);
        assert!(overview.contains("2 Progress: full narrative"));
        assert!(
            !overview.contains("stack is healthy."),
            "the narrative tail is only on Progress at {width}x{height}"
        );
    }
    // A short narrative needs no hint.
    let (mut app, _clock) = hydrated_run_app("narrative-short", |_| {});
    assert!(!screen(&mut app, 100, 30).contains("2 Progress: full narrative"));
}

#[test]
fn an_unavailable_narrative_says_so_instead_of_waiting() {
    let (mut app, _clock) = hydrated_run_app("narrative-unavailable", |page| {
        v1_5_progress(page);
        let narrative = &mut page["progress"]["narrative"];
        narrative["status"] = json!("unavailable");
        narrative["text"] = json!(null);
    });
    let overview = screen(&mut app, 160, 45);
    assert!(overview.contains("AI narrative · unavailable · attempted 12 s ago"));
    assert!(!overview.contains("Waiting for the Run Narrative"));
}
