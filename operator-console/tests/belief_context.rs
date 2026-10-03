//! Independent belief/context captures and consumer-visible evidence checks.
//! UPDATE_FRAMES=1 uses the console's existing readable snapshot convention.

use std::fs;
use std::path::PathBuf;

use operator_console::host::{BeliefRevision, OperatorBeliefs, OperatorContext};
use operator_console::ui::belief_context::{
    draw_belief_context, draw_belief_mini, draw_context_mini,
};
use operator_console::ui::theme::Theme;
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use ratatui::layout::{Constraint, Layout};
use ratatui::style::Color;

fn beliefs_fixture() -> OperatorBeliefs {
    let value: serde_json::Value = serde_json::from_str(include_str!(
        "../../docs/design/operator-console/contract/v1.2/mission-run-operator-beliefs.response.json"
    )).unwrap();
    let mut beliefs: OperatorBeliefs = serde_json::from_value(value["beliefs"].clone()).unwrap();
    // Exact replay acceptance values, rather than the rounded contract example.
    beliefs.history.insert(
        0,
        BeliefRevision {
            revision: 6,
            created_at: "2026-08-27T14:00:30Z".into(),
            top_changes: vec![],
        },
    );
    let ship = &mut beliefs.entities[0];
    ship.mean = 0.6231;
    ship.delta_since_previous = Some(0.12);
    ship.delta_since_prior = Some(0.4902);
    ship.means_by_revision = vec![Some(0.1329), Some(0.5031), Some(0.6231)];
    beliefs.entities[1].means_by_revision = vec![None, Some(0.06), Some(0.06)];
    beliefs
}

fn none_fixture() -> OperatorBeliefs {
    let value: serde_json::Value = serde_json::from_str(include_str!(
        "../../docs/design/operator-console/contract/v1.2/mission-run-operator-beliefs.none.response.json"
    )).unwrap();
    serde_json::from_value(value["beliefs"].clone()).unwrap()
}

fn context_fixture() -> OperatorContext {
    let value: serde_json::Value = serde_json::from_str(include_str!(
        "../../docs/design/operator-console/contract/v1.2/mission-run-operator-context.response.json"
    )).unwrap();
    serde_json::from_value(value["context"].clone()).unwrap()
}

fn render(
    width: u16,
    height: u16,
    beliefs: Option<&OperatorBeliefs>,
    context: Option<&OperatorContext>,
    color: bool,
) -> (String, ratatui::buffer::Buffer) {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| {
            let area = frame.area();
            draw_belief_context(frame, area, beliefs, None, context, Theme::new(color));
        })
        .unwrap();
    let buffer = terminal.backend().buffer().clone();
    (text(&buffer), buffer)
}

fn text(buffer: &ratatui::buffer::Buffer) -> String {
    let area = buffer.area;
    let mut lines = Vec::new();
    for y in area.y..area.bottom() {
        let mut line = String::new();
        for x in area.x..area.right() {
            line.push_str(buffer[(x, y)].symbol());
        }
        lines.push(line.trim_end().to_string());
    }
    lines.push(String::new());
    lines.join("\n")
}

fn snapshot(name: &str, width: u16, height: u16, actual: &str) {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../docs/design/operator-console/frames")
        .join(format!("{name}-{width}x{height}.txt"));
    if std::env::var_os("UPDATE_FRAMES").is_some() {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, actual).unwrap();
    } else {
        let expected =
            fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
        assert_eq!(
            actual,
            expected,
            "{}; regenerate with UPDATE_FRAMES=1",
            path.display()
        );
    }
}

#[test]
fn belief_context_snapshots_compact_and_standard() {
    let beliefs = beliefs_fixture();
    let context = context_fixture();
    for (width, height) in [(100, 30), (160, 45)] {
        let (actual, _) = render(width, height, Some(&beliefs), Some(&context), false);
        snapshot("belief-context-detail", width, height, &actual);
        for evidence in [
            "ship 2",
            "0.6231",
            "+0.4902",
            "Mean history",
            "397",
            "assignment-1-in-progress",
            "assignment-2-in-progress",
            "patrol-replan-requested",
            "search_area",
            "selected",
            "replan",
            "missing",
            "stale",
        ] {
            assert!(
                actual.contains(evidence),
                "{width}x{height} omits {evidence}"
            );
        }
        assert!(actual.contains("200.5"));
        assert!(actual.contains("confirm the completed evidence interval"));
        assert!(actual.contains("62.0%"));
    }
}

#[test]
fn compact_tab_keeps_all_priority_evidence_after_run_chrome() {
    let beliefs = beliefs_fixture();
    let context = context_fixture();
    // The 100x30 run screen reserves six rows for its header and footer.
    let (actual, _) = render(100, 24, Some(&beliefs), Some(&context), false);
    for evidence in [
        "0.6231",
        "+0.4902",
        "397",
        "missing",
        "stale",
        "assignment-2-in-progress",
        "200.5",
        "confirm the completed evidence interval",
        "patrol-replan-requested",
        "search_area",
        "62.0%",
        "selected",
        "replan",
    ] {
        assert!(actual.contains(evidence), "compact tab omits {evidence}");
    }
}

#[test]
fn mission_modes_without_bayesian_belief_show_the_explicit_reason() {
    let mut beliefs = none_fixture();
    let context = context_fixture();
    for (width, height) in [(100, 30), (160, 45)] {
        let (actual, _) = render(width, height, Some(&beliefs), Some(&context), false);
        snapshot("belief-context-none", width, height, &actual);
    }
    for mode in ["mission2", "mission3", "mission4"] {
        beliefs.reason = Some(format!("no Bayesian belief for this mission mode ({mode})"));
        let (actual, _) = render(100, 30, Some(&beliefs), None, false);
        assert!(actual.contains("No Bayesian belief"));
        assert!(actual.contains(mode));
        assert!(!actual.contains("Mean history"));
        assert!(!actual.contains("Δ prior"));
    }
}

#[test]
fn missing_pages_and_null_context_are_not_invented_state() {
    let (pending, _) = render(100, 30, None, None, false);
    assert!(pending.contains("Waiting for the Host"));
    let context = OperatorContext {
        mission_snapshot: None,
        fsm_status: None,
        active_maneuver: None,
        latest_transition_intent: None,
        latest_hyper_outcome: None,
    };
    let (actual, _) = render(100, 30, None, Some(&context), false);
    for message in [
        "Snapshot unavailable",
        "FSM status unavailable",
        "No persisted active maneuver",
        "No persisted Transition Intent",
        "No persisted Hyper outcome",
    ] {
        assert!(actual.contains(message), "missing {message}");
    }
}

#[test]
fn published_history_changes_the_sparkline_and_absent_revisions_stay_blank() {
    let mut beliefs = beliefs_fixture();
    beliefs.entities.truncate(1);
    let (_, full) = render(100, 30, Some(&beliefs), None, false);
    let history_row = (0..full.area.height)
        .find(|&y| {
            (0..full.area.width)
                .map(|x| full[(x, y)].symbol())
                .collect::<String>()
                .contains("Mean history")
        })
        .unwrap();
    let trend_row = history_row + 1;
    let first = full[(1, trend_row)].symbol().to_string();
    let last = full[(3, trend_row)].symbol().to_string();
    assert_ne!(
        first, last,
        "0.1329 and 0.6231 must not have the same bar height"
    );
    beliefs.entities[0].means_by_revision[1] = None;
    let (_, gap) = render(100, 30, Some(&beliefs), None, false);
    assert_eq!(
        gap[(2, trend_row)].symbol(),
        " ",
        "missing revision is not a zero or interpolated bar"
    );
    assert_eq!(gap[(1, trend_row)].symbol(), first);
    assert_eq!(gap[(3, trend_row)].symbol(), last);
}

#[test]
fn distance_details_without_a_fraction_never_become_a_percentage() {
    let mut context = context_fixture();
    let maneuver = context.active_maneuver.as_mut().unwrap();
    maneuver.progress = None;
    maneuver.progress_detail = serde_json::json!({"remaining_distance_meters": 281.5});
    let (actual, _) = render(100, 30, None, Some(&context), false);
    assert!(actual.contains("remaining_distance_meters"));
    assert!(actual.contains("281.5"));
    assert!(!actual.contains('%'));
}

#[test]
fn missing_and_stale_sources_have_warning_glyphs_with_or_without_color() {
    let context = context_fixture();
    let (colored, colored_buffer) = render(160, 45, None, Some(&context), true);
    let (plain, plain_buffer) = render(160, 45, None, Some(&context), false);
    assert_eq!(plain, colored);
    let warning = (0..colored_buffer.area.height)
        .flat_map(|y| (0..colored_buffer.area.width).map(move |x| (x, y)))
        .find(|&(x, y)| colored_buffer[(x, y)].symbol() == "▲")
        .unwrap();
    assert_eq!(colored_buffer[warning].fg, Color::Yellow);
    assert_eq!(plain_buffer[warning].fg, Color::Reset);
}

#[test]
fn overview_minis_retain_belief_changes_and_source_warnings() {
    let beliefs = beliefs_fixture();
    let context = context_fixture();
    let mut terminal = Terminal::new(TestBackend::new(100, 8)).unwrap();
    terminal
        .draw(|frame| {
            let [left, right] =
                Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
                    .areas(frame.area());
            draw_belief_mini(frame, left, Some(&beliefs), Theme::new(false));
            draw_context_mini(frame, right, Some(&context), Theme::new(false));
        })
        .unwrap();
    let actual = text(terminal.backend().buffer());
    for evidence in [
        "0.6231",
        "+0.4902",
        "Snapshot v397",
        "missing [plan]",
        "stale [plan]",
        "candidates 2",
    ] {
        assert!(actual.contains(evidence), "mini omits {evidence}");
    }
}
