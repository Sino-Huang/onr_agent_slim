//! "What this runs" next to the stack selector (API v1.5): the Launch screen
//! composes the Host's preset and toggle-choice descriptions into a short
//! description that follows the selected preset and toggles; focusing
//! Updates explains every update mode; Enter on Preset opens a named picker.
//! An older Host sends no descriptions and the console invents none.

mod common;

use common::*;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use operator_console::app::{App, LaunchField};
use operator_console::host::{HostCommand, HostMessage};
use ratatui::Terminal;
use ratatui::backend::TestBackend;

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

fn about(app: &App, label: &str) -> String {
    app.launch
        .what_this_runs()
        .into_iter()
        .find(|(row, _)| *row == label)
        .map(|(_, text)| text)
        .unwrap_or_else(|| panic!("no {label} row"))
}

/// Text rows inside the Mission Intent editor's border.
fn intent_rows(screen: &[String]) -> usize {
    let top = row_of(screen, "Mission Intent");
    let bottom = (top + 1..screen.len())
        .find(|&row| screen[row].starts_with('└'))
        .unwrap();
    bottom - top - 1
}

#[test]
fn airsim_on_for_mission1_harbor_says_airsim_visualizes_the_simulated_world() {
    let (mut app, _clock) = ready_launch_app("about-harbor-airsim");
    assert_eq!(app.launch.preset().unwrap().preset_id, "mission1-harbor");
    let off = screen(&mut app, 100, 30).join("\n");
    assert!(off.contains("What this runs"), "{off}");
    assert!(off.contains("Mission    Confirm every event in the vessel event report"));
    assert!(off.contains("Truth      Simulated truth"));
    assert!(off.contains("AirSim     Nothing: Harbor is not started"));
    assert!(off.contains("Updates    Mission time pauses while agents reason"));
    assert!(about(&app, "Mission").contains("Real LLM calls"));

    focus(&mut app, LaunchField::Airsim);
    app.handle_key(key(KeyCode::Right));
    assert!(app.launch.airsim);
    let airsim = about(&app, "AirSim");
    assert!(
        airsim.starts_with("AirSim Follower visualizes the simulated world (no agent perception)"),
        "{airsim}"
    );
    // The authority is unchanged: AirSim only visualizes simulated truth.
    assert!(about(&app, "Truth").starts_with("Simulated truth"));
    let on = screen(&mut app, 100, 30).join("\n");
    assert!(
        on.contains(
            "AirSim     AirSim Follower visualizes the simulated world (no agent perception)"
        ),
        "{on}"
    );
    assert!(!on.contains("Nothing: Harbor is not started"));

    app.handle_key(key(KeyCode::Left));
    assert!(about(&app, "AirSim").starts_with("Nothing"));
}

#[test]
fn perception_on_the_airsim_preset_switches_to_perception_fed_truth_and_annotations() {
    let (mut app, _clock) = ready_launch_app("about-airsim-perception");
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Right));
    assert_eq!(app.launch.perception, "yolo");
    assert!(about(&app, "Truth").starts_with("Perception-fed truth: YOLO"));
    assert!(about(&app, "AirSim").contains("annotated with the YOLO detections"));
    assert!(about(&app, "Mission").contains("299.5 s"));

    focus(&mut app, LaunchField::Perception);
    app.handle_key(key(KeyCode::Left));
    assert_eq!(app.launch.perception, "ideal");
    assert!(about(&app, "Truth").starts_with("Perception-fed truth: ideal perception"));
    assert!(about(&app, "AirSim").contains("ships ideal perception reported"));

    app.handle_key(key(KeyCode::Left));
    assert_eq!(app.launch.perception, "off");
    assert!(about(&app, "Truth").starts_with("Simulated truth"));
    assert!(about(&app, "AirSim").starts_with("AirSim Follower visualizes"));
}

#[test]
fn focusing_updates_explains_coordinator_and_environment_driven_updates() {
    let (mut app, _clock) = ready_launch_app("about-updates");
    let rows = |app: &App| -> Vec<(&'static str, String)> {
        app.launch
            .what_this_runs()
            .into_iter()
            .filter(|(label, text)| *label == "Updates" || text.contains("_driven:"))
            .collect()
    };
    // Unfocused: only the selected mode's meaning.
    assert_eq!(rows(&app).len(), 1);
    assert!(rows(&app)[0].1.starts_with("Mission time pauses"));

    focus(&mut app, LaunchField::Updates);
    let focused = rows(&app);
    assert_eq!(focused.len(), 2, "{focused:?}");
    assert!(
        focused[0]
            .1
            .starts_with("▸ coordinator_driven: Mission time pauses")
    );
    assert!(
        focused[1]
            .1
            .starts_with("  environment_driven: Mission time keeps advancing")
    );
    let text = screen(&mut app, 160, 45).join("\n");
    assert!(
        text.contains("Updates    ▸ coordinator_driven: Mission time pauses while agents reason")
    );
    assert!(text.contains("environment_driven: Mission time keeps advancing while agents reason"));

    // The marker follows the value.
    app.handle_key(key(KeyCode::Right));
    assert_eq!(app.launch.update_ownership, "environment_driven");
    let focused = rows(&app);
    assert!(focused[0].1.starts_with("  coordinator_driven"));
    assert!(focused[1].1.starts_with("▸ environment_driven"));

    // Leaving the field explains only the selected mode again.
    focus(&mut app, LaunchField::SimLimit);
    assert_eq!(rows(&app).len(), 1);
    assert!(rows(&app)[0].1.starts_with("Mission time keeps advancing"));
}

#[test]
fn an_older_host_without_descriptions_shows_no_invented_text() {
    let (mut app, _clock) = app_with_clock("about-older-host");
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    app.take_commands();
    app.handle_host_message(HostMessage::Presets(Ok(presets_v1_3())));
    assert!(app.launch.what_this_runs().is_empty());
    let text = screen(&mut app, 100, 30).join("\n");
    assert!(!text.contains("What this runs"));
    assert!(text.contains("mission1-harbor · mission1"));

    // The picker still works and names presets by title and id only.
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Enter));
    let picker = screen(&mut app, 100, 30).join("\n");
    assert!(picker.contains("Stack preset"));
    assert!(picker.contains("Mission 1 · AirSim live"));
    assert!(picker.contains("mission1-airsim"));
    assert!(!picker.contains("Runs"));
}

#[test]
fn enter_on_preset_opens_a_named_picker_that_moves_selects_and_cancels() {
    let (mut app, clock) = ready_launch_app("preset-picker");
    focus(&mut app, LaunchField::Preset);
    app.take_commands();

    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.launch.preset_picker, Some(0));
    assert_eq!(app.launch.focus, LaunchField::Preset);
    let open = screen(&mut app, 100, 30).join("\n");
    assert!(open.contains(" Stack preset "), "{open}");
    for title in [
        "Mission 1 · Harbor (simulated)",
        "Mission 2 · Collision-risk monitoring",
        "Joint · Missions 3 + 4",
    ] {
        assert!(open.contains(title), "{title} missing:\n{open}");
    }
    assert!(open.contains("▸ Mission 1 · Harbor (simulated)"));
    assert!(open.contains("mission1-harbor · current"));
    assert!(open.contains("Runs       Confirm every event in the vessel event report"));

    // Down moves the highlight and the description follows it; nothing is
    // selected yet.
    app.handle_key(key(KeyCode::Down));
    app.handle_key(key(KeyCode::Down));
    assert_eq!(app.launch.preset_picker, Some(2));
    assert_eq!(app.launch.preset().unwrap().preset_id, "mission1-harbor");
    let moved = screen(&mut app, 100, 30).join("\n");
    assert!(moved.contains("▸ Mission 2 · Collision-risk monitoring"));
    assert!(moved.contains("Runs       Monitor ship collision risks"));
    app.handle_key(key(KeyCode::Up));
    assert_eq!(app.launch.preset_picker, Some(1));

    // The overlay is modal: quit and review keys do nothing.
    app.handle_key(key(KeyCode::Char('q')));
    app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT));
    assert!(!app.should_quit());
    assert_eq!(app.launch.preset_picker, Some(1));

    // Esc cancels: the preset and toggles are unchanged.
    app.handle_key(key(KeyCode::Esc));
    assert_eq!(app.launch.preset_picker, None);
    assert_eq!(app.launch.preset().unwrap().preset_id, "mission1-harbor");
    assert!(
        !screen(&mut app, 100, 30)
            .join("\n")
            .contains(" Stack preset ")
    );

    // Enter selects: the preset's defaults apply and preflight re-runs.
    app.handle_key(key(KeyCode::Enter));
    app.handle_key(key(KeyCode::Down));
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.launch.preset_picker, None);
    let preset = app.launch.preset().unwrap();
    assert_eq!(preset.preset_id, "mission1-airsim");
    assert!(app.launch.airsim);
    assert_eq!(app.launch.perception, "yolo");
    assert_eq!(app.launch.simulation_limit_seconds, 290);
    clock.advance(std::time::Duration::from_millis(300));
    app.check_deadlines();
    let (_, query) = single_preflight(&mut app);
    assert_eq!(query.preset_id, "mission1-airsim");

    // ←/→ still cycles presets.
    app.handle_key(key(KeyCode::Left));
    assert_eq!(app.launch.preset().unwrap().preset_id, "mission1-harbor");
}

#[test]
fn re_picking_the_current_preset_keeps_the_operators_toggles() {
    let (mut app, _clock) = ready_launch_app("preset-picker-same");
    focus(&mut app, LaunchField::Airsim);
    app.handle_key(key(KeyCode::Right));
    assert!(app.launch.airsim);
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.launch.preset_picker, Some(0));
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.launch.preset_picker, None);
    assert!(app.launch.airsim, "re-picking must not reset toggles");

    // Home/End jump within the list.
    app.handle_key(key(KeyCode::Enter));
    app.handle_key(key(KeyCode::End));
    assert_eq!(app.launch.preset_picker, Some(7));
    app.handle_key(key(KeyCode::Home));
    assert_eq!(app.launch.preset_picker, Some(0));
    app.handle_key(key(KeyCode::Esc));
}

#[test]
fn enter_on_other_toggles_still_moves_to_the_next_field() {
    let (mut app, _clock) = ready_launch_app("preset-picker-other-fields");
    focus(&mut app, LaunchField::Airsim);
    app.handle_key(key(KeyCode::Enter));
    assert_eq!(app.launch.focus, LaunchField::Perception);
    assert_eq!(app.launch.preset_picker, None);
    assert!(
        app.take_commands()
            .iter()
            .all(|command| !matches!(command, HostCommand::Submit { .. }))
    );
}

#[test]
fn the_mission_intent_editor_keeps_usable_height_at_100x30() {
    let (mut app, _clock) = ready_launch_app("about-intent-height");
    let ready = screen(&mut app, 100, 30);
    assert!(ready.join("\n").contains("What this runs"));
    assert!(intent_rows(&ready) >= 5, "{}", ready.join("\n"));

    // Updates focused explains both modes; the editor keeps its minimum.
    focus(&mut app, LaunchField::Updates);
    let updates = screen(&mut app, 100, 30);
    assert!(intent_rows(&updates) >= 3, "{}", updates.join("\n"));
    // The AirSim Follower's longer description still leaves four rows.
    focus(&mut app, LaunchField::Airsim);
    app.handle_key(key(KeyCode::Right));
    let follower = screen(&mut app, 100, 30);
    assert!(intent_rows(&follower) >= 4, "{}", follower.join("\n"));
    // The Preset hint names the picker.
    focus(&mut app, LaunchField::Preset);
    assert!(
        screen(&mut app, 100, 30)
            .join("\n")
            .contains("Tab field · Enter preset picker · ←/→ cycle")
    );
}
