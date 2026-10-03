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
    ActivationOutcome, ApiVersion, Health, HostError, HostMessage, OperatorSection,
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
    let sequence = app
        .view
        .world
        .as_ref()
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
    app.handle_host_message(HostMessage::WorldFrame {
        mission_run_id: RUN_ID.into(),
        source,
        result: Ok(operator_console::host::Fetched::Fresh {
            value: WorldFrame {
                source,
                media_type: "image/png".into(),
                etag: Some("snapshot-world".into()),
                sequence: Some(sequence),
                mission_time: Some("143.5".into()),
                bytes: bytes.into_inner(),
            },
            etag: Some("snapshot-world".into()),
        }),
    });
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

#[test]
fn run_agents_tab() {
    let mut app = populated_run_app("render-agents");
    let id = section_request_id(&on_tab(&mut app, '3'), OperatorSection::Agents);
    app.handle_host_message(agents_reply(id, &["a-1", "a-2"]));
    snapshot("run-agents", &mut app);
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
fn below_minimum_renders_only_resize_required() {
    let (mut app, _clock) = ready_launch_app("render-resize");
    app.handle_resize(80, 24);
    assert_frame("resize-required-80x24.txt", &render(&mut app, 80, 24));
}
