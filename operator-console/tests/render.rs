//! Ratatui `TestBackend` render snapshots for the Launch screen and every
//! Run tab at the compact minimum (100x30) and a standard-plus size (160x45).
//!
//! Frames are committed as readable plain-text captures under
//! `docs/design/operator-console/frames/`. Regenerate after an intentional
//! layout change with:
//!
//! ```sh
//! UPDATE_FRAMES=1 cargo test --test render
//! ```

mod common;

use std::fs;
use std::path::PathBuf;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::{App, LaunchField};
use operator_console::host::{
    ActivationOutcome, ApiVersion, ContentPurpose, Health, HostError, HostMessage, OperatorSection,
    ReceiptExportOutcome,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;

use image::{DynamicImage, ImageFormat, Rgba, RgbaImage};
use operator_console::host::{FrameSource, WorldFrame};
use ratatui_image::picker::Picker;
const SIZES: [(u16, u16); 2] = [(100, 30), (160, 45)];

fn frames_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../docs/design/operator-console/frames")
}

pub fn render(app: &mut App, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| operator_console::ui::draw(frame, app))
        .unwrap();
    if app.view.media.is_enabled() && app.view.frame.is_some() {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
        while !app.view.media.is_ready() {
            if let Some(result) = app.view.media.poll() {
                result.unwrap();
            }
            terminal
                .draw(|frame| operator_console::ui::draw(frame, app))
                .unwrap();
            assert!(
                std::time::Instant::now() < deadline,
                "image snapshot did not encode"
            );
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }
    let buffer = terminal.backend().buffer();
    let mut lines: Vec<String> = (0..height)
        .map(|y| {
            let mut line = String::new();
            for x in 0..width {
                line.push_str(buffer[(x, y)].symbol());
            }
            line.trim_end().to_string()
        })
        .collect();
    lines.push(String::new());
    lines.join("\n")
}

fn assert_frame(name: &str, actual: &str) {
    let path = frames_dir().join(name);
    if std::env::var_os("UPDATE_FRAMES").is_some() {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, actual).unwrap();
        return;
    }
    let expected = fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("missing fixture frame {}: {error}", path.display()));
    let expected: Vec<&str> = expected.lines().map(str::trim_end).collect();
    let actual: Vec<&str> = actual.lines().map(str::trim_end).collect();
    assert_eq!(
        actual, expected,
        "frame mismatch for {name}; run UPDATE_FRAMES=1 cargo test --test render to regenerate"
    );
}

/// Snapshot `app` at both sizes as `<name>-<w>x<h>.txt`.
fn snapshot(name: &str, app: &mut App) {
    for (width, height) in SIZES {
        app.handle_resize(width, height);
        if name.starts_with("run-world") || (name == "run-overview" && width >= 140) {
            install_world_frame(app);
        }
        assert_frame(
            &format!("{name}-{width}x{height}.txt"),
            &render(app, width, height),
        );
    }
}

/// An owned running run with overview and stack sections answered.
fn populated_run_app(name: &str) -> App {
    let (mut app, _clock) = run_app(name);
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(running_run()),
        },
    )));
    app.request_poll();
    let commands = app.take_commands();
    for section in sections(&commands) {
        let id = section_request_id(&commands, section);
        app.handle_host_message(section_reply(section, id, None, |_| {}));
    }
    app
}

fn on_tab(app: &mut App, digit: char) -> Vec<operator_console::host::HostCommand> {
    app.handle_key(key(KeyCode::Char(digit)));
    app.take_commands()
}

/// A synthetic frame for the selected source, sequenced as the Host advertised.
fn install_world_frame(app: &mut App) {
    let source = app.view.frame_source;
    let value = world_frame(app);
    app.handle_host_message(HostMessage::WorldFrame {
        mission_run_id: RUN_ID.into(),
        source,
        result: Ok(operator_console::host::Fetched::Fresh {
            value,
            etag: Some("snapshot-world".into()),
        }),
    });
}

/// [`install_world_frame`] straight into the media pipeline, as the frame
/// a frozen presentation held from before its freeze (a reply arriving while
/// frozen is dropped).
fn install_held_world_frame(app: &mut App) {
    let value = world_frame(app);
    app.view.frame = Some(WorldFrame {
        bytes: Vec::new(),
        ..value.clone()
    });
    app.view.media.submit(value);
}

/// A synthetic frame (with fresh halfblocks media) for the selected source,
/// sequenced as the displayed world (a frozen presentation's, if frozen).
fn world_frame(app: &mut App) -> WorldFrame {
    let source = app.view.frame_source;
    let sequence = app
        .presentation_evidence()
        .world
        .and_then(|world| {
            world
                .frames
                .iter()
                .find(|frame| frame.source == source.as_str())
        })
        .and_then(|frame| frame.sequence)
        .unwrap_or(812);
    app.configure_images(Some(Picker::halfblocks()));
    let mut image = RgbaImage::new(320, 240);
    for (x, y, pixel) in image.enumerate_pixels_mut() {
        *pixel = if x > 140 && x < 180 && y > 30 && y < 210 {
            Rgba([245, 210, 40, 255])
        } else if y > 100 && y < 140 {
            Rgba([70, 140, 100, 255])
        } else {
            Rgba([25, 50, 100, 255])
        };
    }
    let mut bytes = std::io::Cursor::new(Vec::new());
    DynamicImage::ImageRgba8(image)
        .write_to(&mut bytes, ImageFormat::Png)
        .unwrap();
    WorldFrame {
        source,
        media_type: "image/png".into(),
        etag: Some("snapshot-world".into()),
        sequence: Some(sequence),
        mission_time: Some("143.5".into()),
        bytes: bytes.into_inner(),
    }
}

#[test]
fn launch_screen_ready() {
    let (mut app, _clock) = ready_launch_app("render-launch");
    snapshot("launch-ready", &mut app);
}

#[test]
fn launch_screen_blocked_by_a_failing_check_on_the_airsim_preset() {
    let (mut app, clock) = ready_launch_app("render-launch-blocked");
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Right));
    focus(&mut app, LaunchField::Perception);
    clock.advance(std::time::Duration::from_millis(300));
    app.check_deadlines();
    let (request_id, query) = single_preflight(&mut app);
    app.handle_host_message(HostMessage::Preflight {
        request_id,
        result: Ok(preflight(&query, false)),
    });
    snapshot("launch-blocked", &mut app);
}

/// The AirSim preset answered by the v1.5 Host example: 12 checks with one
/// failure (8th in the Host's order) and one warning.
fn airsim_preflight_app(name: &str) -> App {
    let (mut app, clock) = ready_launch_app(name);
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Right));
    focus(&mut app, LaunchField::Perception);
    clock.advance(std::time::Duration::from_millis(300));
    app.check_deadlines();
    let (request_id, query) = single_preflight(&mut app);
    let mut preflight: operator_console::host::StackPreflight =
        serde_json::from_value(contract("v1.5", "stack-preflight.response.json")).unwrap();
    preflight.preset_id = query.preset_id;
    preflight.toggles = query.toggles;
    app.handle_host_message(HostMessage::Preflight {
        request_id,
        result: Ok(preflight),
    });
    app
}

/// Preflight focused: failures, then warnings, then passes, with counts in
/// the title, the GPU warning selected, and the position on the border.
#[test]
fn launch_screen_with_the_preflight_region_focused() {
    let mut app = airsim_preflight_app("render-launch-preflight");
    focus(&mut app, LaunchField::Preflight);
    app.handle_key(key(KeyCode::Down));
    snapshot("launch-preflight-focused", &mut app);
}

/// Enter on the failing check: its full detail, hint, and the Host's
/// copyable diagnostic command.
#[test]
fn launch_preflight_check_detail_overlay() {
    let mut app = airsim_preflight_app("render-launch-preflight-check");
    focus(&mut app, LaunchField::Preflight);
    app.handle_key(key(KeyCode::Enter));
    snapshot("launch-preflight-check", &mut app);
}

#[test]
fn launch_screen_while_presets_load() {
    let (mut app, _clock) = app_with_clock("render-launch-loading");
    app.handle_host_message(HostMessage::Connected(Ok(health())));
    snapshot("launch-loading", &mut app);
}

#[test]
fn launch_screen_wraps_a_long_intent_and_places_the_cursor_on_it() {
    let (mut app, _clock) = ready_launch_app("render-launch-wrap");
    app.launch.editor.set_text(
        "Patrol the window 60-130 s north -1150..-550 east -950..-450 and verify every reported event, then hold position over ship 2 until the mission budget is spent.",
    );
    for (width, height) in SIZES {
        app.handle_resize(width, height);
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| operator_console::ui::draw(frame, &mut app))
            .unwrap();
        let cursor = terminal.get_cursor_position().unwrap();
        let buffer = terminal.backend().buffer();
        // The cursor sits just after the final "." of the wrapped intent.
        assert_eq!(buffer[(cursor.x - 1, cursor.y)].symbol(), ".");
        assert_eq!(buffer[(cursor.x, cursor.y)].symbol(), " ");
        assert_frame(
            &format!("launch-wrapped-intent-{width}x{height}.txt"),
            &render(&mut app, width, height),
        );
    }
}

#[test]
fn demo_prompt_picker() {
    let (mut app, _clock) = ready_launch_app("render-demo");
    app.handle_key(key(KeyCode::F(2)));
    app.handle_key(key(KeyCode::Down));
    snapshot("launch-demo-prompts", &mut app);
}

/// "What this runs" on mission1-harbor with AirSim switched on: the AirSim
/// Follower visualizes the simulated world (ADR 0016).
#[test]
fn launch_screen_describes_the_airsim_follower() {
    let (mut app, _clock) = ready_launch_app("render-launch-follower");
    focus(&mut app, LaunchField::Airsim);
    app.handle_key(key(KeyCode::Right));
    snapshot("launch-airsim-follower", &mut app);
}

/// Enter on Preset: the named picker with the highlighted preset's
/// description, what it offers and the defaults it applies.
#[test]
fn preset_picker() {
    let (mut app, _clock) = ready_launch_app("render-preset-picker");
    focus(&mut app, LaunchField::Preset);
    app.handle_key(key(KeyCode::Enter));
    app.handle_key(key(KeyCode::Down));
    snapshot("launch-preset-picker", &mut app);
}

#[test]
fn review_activation() {
    let (mut app, _clock) = ready_launch_app("render-review");
    app.handle_key(alt_enter());
    app.pin_review_request_id("8f3a1c2e-4b5d-4e6f-9a0b-1c2d3e4f5a6b");
    snapshot("review-activation", &mut app);
}

#[test]
fn host_too_old() {
    let (mut app, _clock) = app_with_clock("render-too-old");
    app.handle_host_message(HostMessage::Connected(Ok(Health {
        status: "ok".to_string(),
        api_version: ApiVersion { major: 1, minor: 1 },
    })));
    snapshot("host-too-old", &mut app);
}

#[test]
fn connecting_and_activation_rejected() {
    let (mut app, _clock) = app_with_clock("render-connecting");
    snapshot("connecting", &mut app);
    let (mut app, _clock) = ready_launch_app("render-activation-rejected");
    app.handle_key(alt_enter());
    app.handle_key(key(KeyCode::Enter));
    app.handle_host_message(HostMessage::Activated(Ok(ActivationOutcome::Rejected {
        code: "mission_run_active".to_string(),
        message: "another Mission Run is active".to_string(),
    })));
    snapshot("activation-rejected", &mut app);
}

#[test]
fn run_overview_tab() {
    let mut app = populated_run_app("render-overview");
    snapshot("run-overview", &mut app);
}

#[test]
fn run_overview_before_evidence_and_offline() {
    let (mut app, clock) = run_app("render-overview-empty");
    app.view = operator_console::app::RunView::default();
    snapshot("run-overview-waiting", &mut app);
    app.handle_host_message(HostMessage::Current(Err(HostError::UnexpectedStatus(
        500,
        "boom".to_string(),
    ))));
    clock.advance(std::time::Duration::from_secs(31));
    snapshot("run-overview-offline", &mut app);
}

#[test]
fn run_progress_tab_renders_the_hierarchy() {
    let mut app = populated_run_app("render-progress");
    on_tab(&mut app, '2');
    snapshot("run-progress", &mut app);
}

/// The narrative header with a v1.5 Host: age, coverage and newer records,
/// on the capped Overview preview and on the Progress tab.
#[test]
fn run_narrative_shows_its_age_and_coverage() {
    let (mut app, clock) = hydrated_run_app("render-narrative", v1_5_progress);
    // Generated at 12:00:31; 30 s later the Host still answers `/current`
    // while the narrative has not changed, so only its age moved.
    clock.advance(std::time::Duration::from_secs(30));
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(running_run()),
        },
    )));
    snapshot("run-overview-narrative", &mut app);
    on_tab(&mut app, '2');
    snapshot("run-progress-narrative", &mut app);
}

#[test]
fn run_agents_tab() {
    let mut app = populated_run_app("render-agents");
    let id = section_request_id(&on_tab(&mut app, '3'), OperatorSection::Agents);
    app.handle_host_message(agents_reply(id, &["a-1", "a-2"]));
    snapshot("run-agents", &mut app);
}

#[test]
fn run_agents_tab_scrolls_to_a_selection_below_the_fold() {
    let mut app = populated_run_app("render-agents-scrolled");
    let id = section_request_id(&on_tab(&mut app, '3'), OperatorSection::Agents);
    app.handle_host_message(section_reply(OperatorSection::Agents, id, None, |page| {
        let template = page["agents"][0].clone();
        page["agents"] = (1..=60)
            .map(|n| {
                let mut agent = template.clone();
                agent["stable_id"] = serde_json::json!(format!("inv-{n:02}"));
                agent["phase"] = serde_json::json!("plan");
                agent["name"] = serde_json::json!(format!("attempt_{n:02}"));
                agent["started_at"] =
                    serde_json::json!(format!("2026-08-24T12:{:02}:{:02}Z", n / 60, n % 60));
                agent
            })
            .collect();
    }));
    app.handle_key(key(KeyCode::Home));
    for _ in 0..39 {
        app.handle_key(key(KeyCode::Down));
    }
    snapshot("run-agents-scrolled", &mut app);
}

#[test]
fn run_belief_context_tab_renders_both_sections() {
    let mut app = populated_run_app("render-belief");
    on_tab(&mut app, '4');
    snapshot("run-belief-context", &mut app);
}

#[test]
fn belief_tab_keeps_the_selected_entity_detail_visible_with_twenty_entities() {
    let mut app = populated_run_app("render-belief-twenty");
    on_tab(&mut app, '4');
    let beliefs = app.view.beliefs.as_mut().unwrap();
    let template = beliefs.entities[0].clone();
    beliefs.entities = (1..=20)
        .map(|n| {
            let mut entity = template.clone();
            entity.entity_id = n.to_string();
            entity.label = format!("ship {n}");
            entity.mean = if n == 20 { 0.9 } else { 0.1 };
            entity.means_by_revision = if n == 20 {
                vec![Some(0.5), Some(1.0)]
            } else {
                vec![Some(0.1), Some(0.1)]
            };
            entity
        })
        .collect();
    for _ in 0..19 {
        app.handle_key(key(KeyCode::Char('j')));
    }
    app.handle_key(key(KeyCode::Down)); // clamps at the last entity
    for (width, height) in SIZES {
        app.handle_resize(width, height);
        let screen = render(&mut app, width, height);
        let lines: Vec<&str> = screen.lines().collect();
        assert!(
            screen.contains("20/20 · Δ prev"),
            "{width}x{height}:\n{screen}"
        );
        assert!(screen.contains("ship 20 0.9000"), "gauge for the selection");
        assert!(
            lines
                .iter()
                .any(|line| line.contains('▶') && line.contains("ship 20 ")),
            "selected row scrolled into the table viewport at {width}x{height}"
        );
        let history = lines
            .iter()
            .position(|line| line.contains("Mean history"))
            .unwrap();
        assert!(
            lines[history + 1].starts_with("│▄█"),
            "sparkline of 0.5 → 1.0 at {width}x{height}: {}",
            lines[history + 1]
        );
        assert!(screen.contains("Variance"), "detail is not clipped");
    }
    // A newer revision reorders entities; the selection follows the entity ID.
    app.view.beliefs.as_mut().unwrap().entities.reverse();
    let screen = render(&mut app, 100, 30);
    assert!(screen.contains("1/20 · Δ prev"), "{screen}");
    assert!(screen.contains("ship 20 0.9000"));
}

#[test]
fn run_world_tab_renders_viewer_state() {
    let mut app = populated_run_app("render-world");
    let commands = on_tab(&mut app, '5');
    for section in sections(&commands) {
        let id = section_request_id(&commands, section);
        app.handle_host_message(section_reply(section, id, None, |_| {}));
    }
    snapshot("run-world", &mut app);
}

/// A v1.3 World section: the committed `example` world, optionally edited.
fn open_world_tab(name: &str, example: &str, edit: impl FnOnce(&mut serde_json::Value)) -> App {
    let mut app = populated_run_app(name);
    let mut world = contract("v1.3", example)["world"].clone();
    edit(&mut world);
    let commands = on_tab(&mut app, '5');
    for section in sections(&commands) {
        let id = section_request_id(&commands, section);
        let world = world.clone();
        app.handle_host_message(section_reply(section, id, None, move |value| {
            if section == OperatorSection::World {
                value["world"] = world;
            }
        }));
    }
    app
}

#[test]
fn run_world_tab_shows_the_airsim_follower_on_the_annotated_front_camera() {
    let mut app = open_world_tab(
        "render-world-follower",
        "mission-run-operator-world.follower.response.json",
        |_| {},
    );
    app.handle_key(key(KeyCode::Char('s')));
    app.take_commands();
    assert_eq!(app.view.frame_source, FrameSource::CameraFrontAnnotated);
    snapshot("run-world-airsim-follower", &mut app);
}

#[test]
fn run_world_tab_shows_a_scene_clock_frame_without_a_perception_sample() {
    let mut app = open_world_tab(
        "render-world-scene-clock",
        "mission-run-operator-world.scene-clock.response.json",
        |world| {
            let annotation = &mut world["airsim"]["annotation"];
            annotation["match"] = serde_json::json!("none");
            annotation["objects"] = serde_json::json!(0);
            annotation["perception_mission_time_seconds"] = serde_json::json!(143.0);
        },
    );
    snapshot("run-world-airsim-scene-clock", &mut app);
}

/// Poll once and answer every requested section from its example, edited.
fn answer_poll(app: &mut App, edit: impl Fn(OperatorSection, &mut serde_json::Value)) {
    app.request_poll();
    let commands = app.take_commands();
    answer_commands(app, &commands, edit);
}

/// Answer the section requests in `commands` from their examples, edited.
fn answer_commands(
    app: &mut App,
    commands: &[operator_console::host::HostCommand],
    edit: impl Fn(OperatorSection, &mut serde_json::Value),
) {
    for section in sections(commands) {
        let id = section_request_id(commands, section);
        app.handle_host_message(section_reply(section, id, None, |page| edit(section, page)));
    }
}

/// The AirSim Follower on the annotated front camera, presented with F4:
/// the Overview reports the world's Mission time (143.5 s).
fn presentation_app(name: &str) -> App {
    let mut app = open_world_tab(
        name,
        "mission-run-operator-world.follower.response.json",
        |_| {},
    );
    app.handle_key(key(KeyCode::Char('s')));
    app.handle_key(key(KeyCode::F(4)));
    let commands = app.take_commands();
    answer_commands(&mut app, &commands, |section, page| {
        if section == OperatorSection::Overview {
            page["overview"]["environment"]["mission_time_seconds"] = serde_json::json!(143.5);
        } else if section == OperatorSection::World {
            page["world"] =
                contract("v1.3", "mission-run-operator-world.follower.response.json")["world"]
                    .clone();
        }
    });
    app
}

/// Snapshot a presentation at both sizes. Its frame is installed as held
/// media, which a frozen presentation keeps (it drops arriving replies).
fn snapshot_presentation(name: &str, app: &mut App) {
    for (width, height) in SIZES {
        app.handle_resize(width, height);
        install_held_world_frame(app);
        assert_frame(
            &format!("{name}-{width}x{height}.txt"),
            &render(app, width, height),
        );
    }
}

/// F4: the annotated front camera large with the follower and box
/// disclosures, the milestone card, the mission title and both clocks.
#[test]
fn presentation_layout_with_the_airsim_follower() {
    let mut app = presentation_app("render-presentation");
    snapshot_presentation("run-presentation", &mut app);
}

/// `v v p`: the invocation card frozen; a newer invocation and a later
/// Mission time arrived after the freeze and are not shown.
#[test]
fn presentation_frozen_holds_the_card_and_clocks() {
    let mut app = presentation_app("render-presentation-frozen");
    app.handle_key(key(KeyCode::Char('v')));
    app.handle_key(key(KeyCode::Char('v')));
    app.handle_key(key(KeyCode::Char('p')));
    answer_poll(&mut app, |section, page| match section {
        OperatorSection::Overview => {
            page["overview"]["environment"]["mission_time_seconds"] = serde_json::json!(151.0);
        }
        OperatorSection::Agents => {
            let agent = &mut page["agents"][0];
            agent["stable_id"] = serde_json::json!("hyper-agent:invocation-9");
            agent["invocation_id"] = serde_json::json!("invocation-9");
            agent["name"] = serde_json::json!("replan_after_report");
            agent["started_at"] = serde_json::json!("2026-08-27T12:05:00Z");
        }
        _ => {}
    });
    snapshot_presentation("run-presentation-frozen", &mut app);
}

/// A stack failure while the presentation is frozen: the alert row and the
/// failure card break through; the held card and clocks stay.
#[test]
fn presentation_failure_alert_breaks_through_a_frozen_view() {
    let mut app = presentation_app("render-presentation-alert");
    app.handle_key(key(KeyCode::Char('p')));
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(stack_failed_run()),
        },
    )));
    app.take_commands();
    snapshot_presentation("run-presentation-alert", &mut app);
}

#[test]
fn run_stack_tab_with_log_tail() {
    let mut app = populated_run_app("render-stack");
    on_tab(&mut app, '6');
    app.handle_host_message(service_log_reply(
        0,
        "starting physical runtime for mission-fixture-001\nloading scenario harbor\nviewer listening on 127.0.0.1:5066\nenvironment update 1 published\n",
        125,
    ));
    snapshot("run-stack", &mut app);
}

/// [`waiting_run_app`] answering the stack section with the edited example.
fn waiting_example_app(
    name: &str,
    current: &str,
    (version, example): (&str, &str),
    edit_stack: impl FnOnce(&mut serde_json::Value),
    edit_overview: impl FnOnce(&mut serde_json::Value),
) -> (App, std::sync::Arc<ManualClock>) {
    let mut stack = stack_example(version, example);
    edit_stack(&mut stack);
    waiting_run_app(name, current, stack, edit_overview)
}

// The fixture clock reads 2026-08-24T12:00:43Z.

#[test]
fn waiting_banner_names_the_service_and_its_readiness_budget_on_every_tab() {
    let (mut app, _clock) = waiting_example_app(
        "render-waiting-service",
        "stack",
        ("v1.4", "mission-run-operator-stack.starting.response.json"),
        |stack| {
            stack["services"][0]["started_at"] = serde_json::json!("2026-08-24T11:58:38Z");
            stack["services"][0]["ready_at"] = serde_json::json!("2026-08-24T11:59:56Z");
            stack["services"][1]["started_at"] = serde_json::json!("2026-08-24T11:59:56Z");
        },
        |_| {},
    );
    snapshot("run-waiting-service", &mut app);
    on_tab(&mut app, '6');
    // The first poll selected physical-runtime; select the starting service.
    app.handle_key(key(KeyCode::Up));
    app.take_commands();
    let log = "perception producer starting on 127.0.0.1:8766\nloading YOLO weights\n";
    app.handle_host_message(service_log_reply_for(
        "service-log-perception",
        0,
        log,
        log.len() as u64,
    ));
    snapshot("run-waiting-service-stack", &mut app);
}

/// The v1.5 perception wait: started 0:47 ago, ready in 0:27 last run.
fn perception_wait_app(name: &str) -> (App, std::sync::Arc<ManualClock>) {
    waiting_example_app(
        name,
        "stack",
        ("v1.5", "mission-run-operator-stack.starting.response.json"),
        |stack| {
            stack["services"][0]["started_at"] = serde_json::json!("2026-08-24T11:58:38Z");
            stack["services"][0]["ready_at"] = serde_json::json!("2026-08-24T11:59:56Z");
            stack["services"][1]["started_at"] = serde_json::json!("2026-08-24T11:59:56Z");
        },
        |_| {},
    )
}

#[test]
fn waiting_banner_shows_last_run_history_and_w_opens_the_service_log() {
    let (mut app, _clock) = perception_wait_app("render-waiting-history");
    snapshot("run-waiting-service-history", &mut app);
    app.handle_key(key(KeyCode::Char('w')));
    app.take_commands();
    let log = "perception producer starting on 127.0.0.1:8766\nloading YOLO weights\n";
    app.handle_host_message(service_log_reply_for(
        "service-log-perception",
        0,
        log,
        log.len() as u64,
    ));
    snapshot("run-waiting-service-inspected", &mut app);
}

#[test]
fn stack_tab_lists_prep_steps_with_measured_durations() {
    let (mut app, _clock) = waiting_example_app(
        "render-stack-steps",
        "stack",
        (
            "v1.5",
            "mission-run-operator-stack.post-ready.response.json",
        ),
        |stack| {
            stack["step"]["started_at"] = serde_json::json!("2026-08-24T11:59:10Z");
            stack["steps"][2]["started_at"] = serde_json::json!("2026-08-24T11:59:10Z");
        },
        |_| {},
    );
    // `w` opens the running prep step's log on the Stack tab.
    app.handle_key(key(KeyCode::Char('w')));
    app.take_commands();
    let log = "rendering surveillance view 12/40\nrendering surveillance view 13/40\n";
    app.handle_host_message(service_log_reply_for(
        "service-log-mission1-surveillance-views",
        0,
        log,
        log.len() as u64,
    ));
    snapshot("run-stack-steps", &mut app);
}

#[test]
fn waiting_banner_reports_a_running_prep_step_and_turns_late_near_its_budget() {
    let (mut app, _clock) = waiting_example_app(
        "render-waiting-step",
        "stack",
        ("v1.4", "mission-run-operator-stack.preparing.response.json"),
        |stack| {
            stack["step"]["started_at"] = serde_json::json!("2026-08-24T11:58:38Z");
        },
        |_| {},
    );
    app.handle_resize(100, 30);
    let banner = render(&mut app, 100, 30)
        .lines()
        .nth(3)
        .unwrap()
        .to_string();
    assert_eq!(
        banner,
        " ⠋ Running airsim-fixture (stack preparation) · 2:05 / 10:00 · w: 6 Stack log"
    );

    // 8:05 of 10:00 is past 80% of the budget; of 60:00 it is not.
    for (timeout, expected) in [
        (
            600.0,
            " ⠋ Running airsim-fixture (stack preparation) · ▲ 8:05 / 10:00 · w: 6 Stack log",
        ),
        (
            3600.0,
            " ⠋ Running airsim-fixture (stack preparation) · 8:05 / 60:00 · w: 6 Stack log",
        ),
    ] {
        let (mut app, _clock) = waiting_example_app(
            "render-waiting-late",
            "stack",
            ("v1.4", "mission-run-operator-stack.preparing.response.json"),
            |stack| {
                stack["step"]["started_at"] = serde_json::json!("2026-08-24T11:52:38Z");
                stack["step"]["timeout_seconds"] = serde_json::json!(timeout);
            },
            |_| {},
        );
        app.handle_resize(100, 30);
        let frame = render(&mut app, 100, 30);
        assert_eq!(frame.lines().nth(3).unwrap(), expected);
    }
}

#[test]
fn waiting_banner_spinner_advances_with_the_console_clock() {
    let (mut app, clock) = waiting_example_app(
        "render-waiting-spinner",
        "stack",
        ("v1.4", "mission-run-operator-stack.starting.response.json"),
        |_| {},
        |_| {},
    );
    app.handle_resize(100, 30);
    let first = render(&mut app, 100, 30)
        .lines()
        .nth(3)
        .unwrap()
        .to_string();
    clock.advance(std::time::Duration::from_millis(100));
    let second = render(&mut app, 100, 30)
        .lines()
        .nth(3)
        .unwrap()
        .to_string();
    assert!(first.starts_with(" ⠋ Starting perception (2/4)"), "{first}");
    assert!(
        second.starts_with(" ⠙ Starting perception (2/4)"),
        "{second}"
    );
}

#[test]
fn waiting_banner_shows_a_live_llm_call_and_leaves_once_the_stack_is_ready() {
    let live_call =
        |overview: &mut serde_json::Value| set_live_llm_call(overview, "hyper-agent:invocation-1");
    let (mut app, _clock) = waiting_example_app(
        "render-waiting-llm",
        "planning",
        ("v1.4", "mission-run-operator-stack.starting.response.json"),
        // The stack phase is done: a stale `starting` entry must not win.
        |_| {},
        live_call,
    );
    snapshot("run-waiting-llm", &mut app);

    let (mut app, _clock) = waiting_example_app(
        "render-waiting-none",
        "planning",
        ("v1.4", "mission-run-operator-stack.starting.response.json"),
        |_| {},
        |_| {},
    );
    app.handle_resize(100, 30);
    let frame = render(&mut app, 100, 30);
    assert!(
        frame.lines().nth(3).unwrap().starts_with("┌"),
        "no banner row without a wait:\n{frame}"
    );
}
/// The v1.5 teardown example with the engine's SIGTERM 0:06 before the
/// fixture clock, after the owner confirmed `c`: the request is in flight.
fn teardown_app(name: &str) -> App {
    let (mut app, _clock) = waiting_example_app(
        name,
        "executing",
        ("v1.5", "mission-run-operator-stack.stopping.response.json"),
        |stack| {
            stack["services"][0]["stop_requested_at"] = serde_json::json!("2026-08-24T12:00:37Z");
            stack["teardown"]["started_at"] = serde_json::json!("2026-08-24T12:00:34Z");
        },
        |_| {},
    );
    app.handle_key(key(KeyCode::Char('c')));
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app
}

#[test]
fn teardown_banner_replaces_the_cancellation_dialog() {
    let mut app = teardown_app("render-teardown");
    snapshot("run-teardown", &mut app);
}

#[test]
fn stack_tab_shows_stopping_and_stopped_services_with_their_mode() {
    let mut app = teardown_app("render-teardown-stack");
    // `w` selects the stopping engine and follows its log.
    app.handle_key(key(KeyCode::Char('w')));
    app.take_commands();
    let log = "Engine ready and frozen: ready.json\nStopping Harbor\n";
    app.handle_host_message(service_log_reply_for(
        "service-log-airsim-engine",
        0,
        log,
        log.len() as u64,
    ));
    snapshot("run-teardown-stack", &mut app);
}

#[test]
fn terminal_overview_shows_the_teardown_receipt() {
    let (mut app, _clock) = terminal_run_app(
        "render-teardown-receipt",
        cancelled_run(),
        |section, page| match section {
            OperatorSection::Stack => {
                let mut stack =
                    stack_example("v1.5", "mission-run-operator-stack.cancelled.response.json");
                stack["teardown"]["started_at"] = serde_json::json!("2026-08-24T12:00:30Z");
                stack["teardown"]["finished_at"] = serde_json::json!("2026-08-24T12:00:40Z");
                page["stack"] = stack;
            }
            // The Host's phase for a run cancelled while executing.
            OperatorSection::Overview => {
                let overview = &mut page["overview"];
                failed_phase(overview, "executing", "plan revision 3");
                overview["phase"]["steps"][5]["detail"] = serde_json::json!("cancelled_by_owner");
            }
            _ => {}
        },
    );
    snapshot("run-teardown-receipt", &mut app);
}

/// The cancelled run's v1.5 teardown with a 0:10 stop, as in
/// `run-teardown-receipt`.
fn cancelled_teardown() -> serde_json::Value {
    let mut stack = stack_example("v1.5", "mission-run-operator-stack.cancelled.response.json");
    stack["teardown"]["started_at"] = serde_json::json!("2026-08-24T12:00:30Z");
    stack["teardown"]["finished_at"] = serde_json::json!("2026-08-24T12:00:40Z");
    stack
}

/// A succeeded lifecycle whose audit artifact recorded FAIL: the receipt
/// shows the audit's verdict, never one inferred from the lifecycle.
#[test]
fn terminal_receipt_for_a_succeeded_run() {
    let example = contract(
        "v1.5",
        "mission-run-operator-overview.succeeded.response.json",
    );
    let mut stack = stack_example("v1.5", "mission-run-operator-stack.cancelled.response.json");
    for service in stack["services"].as_array_mut().unwrap() {
        service["stop_mode"] = serde_json::json!("graceful");
    }
    stack["teardown"]["started_at"] = serde_json::json!("2026-08-24T12:05:10Z");
    stack["teardown"]["finished_at"] = serde_json::json!("2026-08-24T12:05:14Z");
    stack["teardown"]["harbor_config"]["reported_by"] = serde_json::json!("engine");
    let (mut app, _clock) = receipt_run_app(
        "render-receipt-succeeded",
        succeeded_run(),
        receipt_example(|_| {}),
        Some(stack),
        |overview| overview["phase"] = example["overview"]["phase"].clone(),
    );
    snapshot("run-receipt-succeeded", &mut app);
}

/// A worker failure (card dismissed): no audit artifact, so `not recorded`.
#[test]
fn terminal_receipt_for_a_failed_run() {
    let (mut app, _clock) = receipt_run_app(
        "render-receipt-failed",
        worker_failed_run(),
        receipt_example(|receipt| {
            receipt["status"] = serde_json::json!("failed");
            receipt["classification"] = serde_json::json!("worker_failed");
            receipt["wall_seconds"] = serde_json::json!(6.0);
            receipt["final"]["fsm_state"] = serde_json::Value::Null;
            receipt["final"]["plan_revision"] = serde_json::Value::Null;
            receipt["final"]["mission_time_seconds"] = serde_json::Value::Null;
            receipt["final"]["source"] = serde_json::Value::Null;
            receipt["audit"] = serde_json::json!({
                "status": "not_recorded",
                "path": null,
                "recorded_at": null,
                "mission_mode": null,
                "failures": []
            });
        }),
        None,
        |overview| {
            failed_phase(
                overview,
                "executing",
                "external environment has no planning data",
            )
        },
    );
    app.handle_key(key(KeyCode::Enter));
    snapshot("run-receipt-failed", &mut app);
}

/// Owner cancellation: teardown and the receipt in one panel, the Context
/// labelled `last observed`, and the export the Host wrote.
#[test]
fn terminal_receipt_for_a_cancelled_run_after_export() {
    let (mut app, _clock) = receipt_run_app(
        "render-receipt-cancelled",
        cancelled_run(),
        receipt_example(|receipt| {
            receipt["status"] = serde_json::json!("cancelled");
            receipt["classification"] = serde_json::json!("cancelled_by_owner");
            receipt["wall_seconds"] = serde_json::json!(38.0);
            receipt["final"]["fsm_state"] = serde_json::json!("navigate");
            receipt["final"]["plan_revision"] = serde_json::json!(3);
            receipt["final"]["mission_time_seconds"] = serde_json::json!(12.5);
            receipt["audit"] = serde_json::json!({
                "status": "not_recorded",
                "path": null,
                "recorded_at": null,
                "mission_mode": null,
                "failures": []
            });
        }),
        Some(cancelled_teardown()),
        |overview| {
            failed_phase(overview, "executing", "plan revision 3");
            overview["phase"]["steps"][5]["detail"] = serde_json::json!("cancelled_by_owner");
        },
    );
    app.handle_key(key(KeyCode::Char('x')));
    app.take_commands();
    let exported = contract("v1.5", "mission-run-receipt-export.response.json");
    app.handle_host_message(HostMessage::ReceiptExported(Ok(
        ReceiptExportOutcome::Exported(serde_json::from_value(exported).unwrap()),
    )));
    snapshot("run-receipt-cancelled", &mut app);
    app.handle_key(key(KeyCode::Char('4')));
    app.take_commands();
    snapshot("run-receipt-context", &mut app);
}

#[test]
fn run_artifacts_tab_and_inspector() {
    let mut app = populated_run_app("render-artifacts");
    let id = section_request_id(&on_tab(&mut app, '7'), OperatorSection::Artifacts);
    app.handle_host_message(artifacts_reply(id));
    snapshot("run-artifacts", &mut app);
    let artifact = app.view.selected_artifact.clone().unwrap();
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(content_reply(&artifact, 0, "text-page"));
    snapshot("run-artifact-inspector", &mut app);
}

#[test]
fn run_artifact_inspector_scrolled_to_the_last_line_of_a_tall_page() {
    let mut app = populated_run_app("render-inspector-scrolled");
    let id = section_request_id(&on_tab(&mut app, '7'), OperatorSection::Artifacts);
    app.handle_host_message(artifacts_reply(id));
    let artifact = app.view.selected_artifact.clone().unwrap();
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    let mut value = contract("v1", "mission-run-artifact-content.text-page.response.json");
    value["mission_run_id"] = serde_json::json!(RUN_ID);
    value["artifact_id"] = serde_json::json!(artifact);
    value["byte_size"] = serde_json::json!(51234);
    let content: String = (1..=80)
        .map(|n| {
            if n % 20 == 0 {
                format!(
                    "[2026-08-24T12:01:{:02}Z] planner: candidate plan cost {n} rejected because the survey window overlaps the exclusion zone around ship {}\n",
                    n % 60,
                    n / 20
                )
            } else {
                format!(
                    "[2026-08-24T12:01:{:02}Z] planner: expanded {} states\n",
                    n % 60,
                    n * 16
                )
            }
        })
        .collect();
    value["content"] = serde_json::json!(content);
    app.handle_host_message(HostMessage::ArtifactContent {
        purpose: ContentPurpose::Inspector,
        mission_run_id: RUN_ID.to_string(),
        artifact_id: artifact,
        requested_offset: 0,
        result: Ok(serde_json::from_value(value).unwrap()),
    });
    app.handle_resize(100, 30);
    render(&mut app, 100, 30);
    app.handle_key(key(KeyCode::End));
    snapshot("run-artifact-inspector-scrolled", &mut app);
}

#[test]
fn run_help_overlay() {
    let mut app = populated_run_app("render-help");
    app.handle_key(key(KeyCode::Char('?')));
    snapshot("run-help", &mut app);
}

#[test]
fn run_cancellation_confirmation() {
    let mut app = populated_run_app("render-cancel");
    app.handle_key(key(KeyCode::Char('c')));
    snapshot("run-cancellation-confirmation", &mut app);
}

#[test]
fn run_terminal_rejected_shows_the_terminal_detail() {
    let mut app = populated_run_app("render-rejected");
    app.launch.editor.set_text("buy me a coffee");
    app.handle_host_message(HostMessage::Current(Ok(
        operator_console::host::CurrentRun {
            mission_run: Some(rejected_run()),
        },
    )));
    app.request_poll();
    let commands = app.take_commands();
    for section in sections(&commands) {
        app.handle_host_message(section_reply(
            section,
            section_request_id(&commands, section),
            None,
            |page| {
                page["run_status"] = serde_json::json!("failed");
                page["has_more"] = serde_json::json!(false);
                match section {
                    OperatorSection::Artifacts => page["artifacts"] = serde_json::json!([]),
                    OperatorSection::Environment => {
                        page["environment"]["timeline"] = serde_json::json!([])
                    }
                    OperatorSection::Context => {
                        page["context"]["active_maneuver"] = serde_json::Value::Null
                    }
                    OperatorSection::Overview => {
                        page["overview"]["narrative"]["terminal"] = serde_json::json!(true);
                        page["overview"]["narrative"]["status"] = serde_json::json!("unavailable");
                        page["overview"]["phase"]["current"] = serde_json::json!("intent");
                        for step in page["overview"]["phase"]["steps"].as_array_mut().unwrap() {
                            let status = match step["id"].as_str().unwrap() {
                                "stack" => "done",
                                "intent" => "failed",
                                _ => "pending",
                            };
                            step["status"] = serde_json::json!(status);
                            step["detail"] = serde_json::Value::Null;
                        }
                    }
                    _ => {}
                }
            },
        ));
    }
    snapshot("run-terminal-rejected", &mut app);
}

#[test]
fn run_failure_card_for_a_stack_failure() {
    let (mut app, _clock) = stack_failed_app("render-failure-stack");
    snapshot("run-failure-stack-failed", &mut app);
}

#[test]
fn run_failure_card_for_a_worker_failure() {
    let (mut app, _clock) = worker_failed_app("render-failure-worker");
    snapshot("run-failure-worker-failed", &mut app);
}

/// F3 over the owned running run: the v1.5 history example, the succeeded
/// historical run selected.
#[test]
fn run_history_overlay() {
    let mut app = populated_run_app("render-history");
    app.health = Some(health_v1_5());
    open_history(&mut app);
    select_history_row(&mut app, HISTORICAL_RUN_ID);
    app.take_commands();
    snapshot("run-history", &mut app);
}

/// The succeeded historical run opened read-only: `HISTORICAL` badge, its
/// receipt without the export control, and keys that only navigate.
#[test]
fn historical_run_overview_with_the_badge() {
    let mut app = populated_run_app("render-historical");
    app.health = Some(health_v1_5());
    open_history(&mut app);
    select_history_row(&mut app, HISTORICAL_RUN_ID);
    app.handle_key(key(KeyCode::Enter));
    let commands = app.take_commands();
    let example = contract(
        "v1.5",
        "mission-run-operator-overview.succeeded.response.json",
    );
    answer_sections_for(
        &mut app,
        &commands,
        HISTORICAL_RUN_ID,
        "succeeded",
        |section, page| match section {
            OperatorSection::Overview => {
                let overview = &mut page["overview"];
                let receipt = receipt_example(|receipt| {
                    receipt["wall_seconds"] = serde_json::json!(311.831137);
                });
                overview["fsm"]["state"] = receipt["final"]["fsm_state"].clone();
                overview["environment"]["mission_time_seconds"] =
                    receipt["final"]["mission_time_seconds"].clone();
                overview["receipt"] = receipt;
                overview["phase"] = example["overview"]["phase"].clone();
                overview["run_root"] = serde_json::json!(format!(
                    "/srv/onr/var/runtime-host/runs/{HISTORICAL_RUN_ID}"
                ));
            }
            OperatorSection::Stack => page["stack"] = cancelled_teardown(),
            _ => {}
        },
    );
    app.take_commands();
    snapshot("run-historical-overview", &mut app);
}

/// A large historical run on a cold Host: its first read outlasted the
/// request limit while the Host rebuilds it from disk. Loading, retried; not
/// a failed poll.
#[test]
fn historical_run_loading_from_disk() {
    let (mut app, clock) = run_app("render-historical-loading");
    let current = || {
        HostMessage::Current(Ok(operator_console::host::CurrentRun {
            mission_run: Some(running_run()),
        }))
    };
    app.handle_host_message(current());
    app.request_poll();
    let commands = app.take_commands();
    for section in sections(&commands) {
        let id = section_request_id(&commands, section);
        app.handle_host_message(section_reply(section, id, None, |_| {}));
    }
    app.health = Some(health_v1_5());
    open_history(&mut app);
    select_history_row(&mut app, HISTORICAL_RUN_ID);
    app.handle_key(key(KeyCode::Enter));
    let commands = app.take_commands();
    clock.advance(std::time::Duration::from_secs(7));
    // The parked current run keeps answering: the Host itself is live.
    app.handle_host_message(current());
    let request_id = commands
        .iter()
        .find_map(|command| match command {
            operator_console::host::HostCommand::FetchOperatorView {
                mission_run_id,
                section: OperatorSection::Overview,
                request_id,
                ..
            } if mission_run_id == HISTORICAL_RUN_ID => Some(*request_id),
            _ => None,
        })
        .unwrap();
    app.handle_host_message(HostMessage::OperatorView {
        mission_run_id: HISTORICAL_RUN_ID.to_string(),
        section: OperatorSection::Overview,
        request_id,
        result: Err(HostError::Timeout("timeout: global".to_string())),
    });
    app.take_commands();
    snapshot("run-historical-loading", &mut app);
}

#[test]
fn below_minimum_renders_only_resize_required() {
    let (mut app, _clock) = ready_launch_app("render-resize");
    app.handle_resize(80, 24);
    assert_frame("resize-required-80x24.txt", &render(&mut app, 80, 24));
}
