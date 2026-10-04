//! Every list row and every inspector line is reachable: the Agents and
//! Artifacts lists keep the selection inside a scrolling viewport, and the
//! Artifact inspector scrolls wrapped lines within a byte page.

mod common;

use common::*;
use crossterm::event::KeyCode;
use operator_console::app::App;
use operator_console::host::{ContentPurpose, HostMessage, OperatorSection};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::style::Modifier;
use serde_json::json;

fn render(app: &mut App, width: u16, height: u16) -> Buffer {
    app.handle_resize(width, height);
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| operator_console::ui::draw(frame, app))
        .unwrap();
    terminal.backend().buffer().clone()
}

fn row_text(buffer: &Buffer, y: u16) -> String {
    (0..buffer.area.width)
        .map(|x| buffer[(x, y)].symbol())
        .collect()
}

/// Top-left cell of the first occurrence of `needle` (one cell per char).
fn find(buffer: &Buffer, needle: &str) -> Option<(u16, u16)> {
    let needle: Vec<char> = needle.chars().collect();
    (0..buffer.area.height).find_map(|y| {
        let cells: Vec<&str> = (0..buffer.area.width)
            .map(|x| buffer[(x, y)].symbol())
            .collect();
        cells
            .windows(needle.len())
            .position(|window| {
                window
                    .iter()
                    .zip(&needle)
                    .all(|(cell, c)| cell.chars().eq(std::iter::once(*c)))
            })
            .map(|x| (x as u16, y))
    })
}

fn highlighted(buffer: &Buffer, (x, y): (u16, u16)) -> bool {
    buffer[(x, y)].modifier.contains(Modifier::REVERSED)
}

fn screen(buffer: &Buffer) -> String {
    (0..buffer.area.height)
        .map(|y| row_text(buffer, y))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Agents page with invocations `inv-NN` for each `n`, in start order.
fn agents_page(request_id: u64, numbers: std::ops::RangeInclusive<u32>) -> HostMessage {
    section_reply(OperatorSection::Agents, request_id, None, |value| {
        let template = value["agents"][0].clone();
        value["agents"] = numbers
            .map(|n| {
                let mut agent = template.clone();
                agent["stable_id"] = json!(format!("inv-{n:02}"));
                agent["name"] = json!(format!("inv-{n:02}"));
                agent["phase"] = json!("p");
                agent["started_at"] = json!(format!("2026-08-24T12:{:02}:{:02}Z", n / 60, n % 60));
                agent
            })
            .collect();
    })
}

/// Run app on the Agents tab holding invocations `inv-01`..=`inv-60`.
fn sixty_agents(name: &str) -> App {
    let (mut app, _clock) = run_app(name);
    app.handle_key(key(KeyCode::Char('3')));
    let id = section_request_id(&app.take_commands(), OperatorSection::Agents);
    app.handle_host_message(agents_page(id, 1..=60));
    app
}

fn press(app: &mut App, code: KeyCode, times: usize) {
    for _ in 0..times {
        app.handle_key(key(code));
    }
}

#[test]
fn selecting_agent_row_40_of_60_at_100x30_renders_it_highlighted() {
    let mut app = sixty_agents("reach-agents-40");
    app.handle_key(key(KeyCode::Home));
    press(&mut app, KeyCode::Down, 39);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("inv-40"));

    let buffer = render(&mut app, 100, 30);
    let cell = find(&buffer, "inv-40").unwrap_or_else(|| panic!("{}", screen(&buffer)));
    assert!(highlighted(&buffer, cell), "{}", screen(&buffer));
    assert!(find(&buffer, "40/60 ▼").is_some(), "{}", screen(&buffer));
    assert!(find(&buffer, " ▲ ┐").is_some(), "rows above are marked");
    assert!(find(&buffer, "inv-01").is_none(), "row 1 scrolled out");

    // The selection keeps its screen row across a resize round trip and an
    // evidence update that adds newer invocations.
    let buffer = render(&mut app, 160, 45);
    assert!(highlighted(&buffer, find(&buffer, "inv-40").unwrap()));
    let buffer = render(&mut app, 100, 30);
    assert_eq!(find(&buffer, "inv-40"), Some(cell));
    app.request_poll();
    let id = section_request_id(&app.take_commands(), OperatorSection::Agents);
    app.handle_host_message(agents_page(id, 61..=70));
    assert_eq!(app.view.selected_invocation.as_deref(), Some("inv-40"));
    let buffer = render(&mut app, 100, 30);
    assert_eq!(find(&buffer, "inv-40"), Some(cell));
    assert!(highlighted(&buffer, cell));
    assert!(find(&buffer, "40/70 ▼").is_some(), "{}", screen(&buffer));
}

#[test]
fn selecting_artifact_row_40_of_60_at_100x30_renders_it_highlighted() {
    let (mut app, _clock) = run_app("reach-artifacts-40");
    app.handle_key(key(KeyCode::Char('7')));
    let id = section_request_id(&app.take_commands(), OperatorSection::Artifacts);
    app.handle_host_message(section_reply(
        OperatorSection::Artifacts,
        id,
        None,
        |page| {
            let template = page["artifacts"][0].clone();
            page["artifacts"] = (1..=60)
                .map(|n| {
                    let mut artifact = template.clone();
                    artifact["artifact_id"] = json!(format!("art-{n:02}"));
                    artifact["kind"] = json!("log");
                    artifact["display"]["title"] = json!(format!("art-{n:02}"));
                    artifact
                })
                .collect();
        },
    ));
    assert_eq!(app.view.selected_artifact.as_deref(), Some("art-01"));
    press(&mut app, KeyCode::Down, 39);
    assert_eq!(app.view.selected_artifact.as_deref(), Some("art-40"));

    let buffer = render(&mut app, 100, 30);
    let cell = find(&buffer, "log art-40").unwrap_or_else(|| panic!("{}", screen(&buffer)));
    assert!(highlighted(&buffer, cell), "{}", screen(&buffer));
    assert!(find(&buffer, "40/60 ▼").is_some(), "{}", screen(&buffer));
    assert!(find(&buffer, " ▲ ┐").is_some(), "rows above are marked");
    assert!(find(&buffer, "log art-01").is_none(), "row 1 scrolled out");

    app.handle_key(key(KeyCode::End));
    let buffer = render(&mut app, 100, 30);
    assert!(highlighted(&buffer, find(&buffer, "log art-60").unwrap()));
    assert!(find(&buffer, " 60/60 ").is_some(), "no ▼ at the end");
    app.handle_key(key(KeyCode::Home));
    let buffer = render(&mut app, 100, 30);
    assert!(highlighted(&buffer, find(&buffer, "log art-01").unwrap()));
    assert!(find(&buffer, " ▲ ┐").is_none(), "no ▲ at the top");
}

#[test]
fn agents_follow_newest_is_not_re_enabled_by_scrolling() {
    let mut app = sixty_agents("reach-agents-follow");
    assert!(app.view.agent_following);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("inv-60"));
    let buffer = render(&mut app, 100, 30);
    assert!(highlighted(&buffer, find(&buffer, "inv-60").unwrap()));

    app.handle_key(key(KeyCode::Up));
    assert!(!app.view.agent_following);
    // Scrolling back down to the newest row, by line or by End, stays paused.
    app.handle_key(key(KeyCode::Down));
    assert!(!app.view.agent_following);
    app.handle_key(key(KeyCode::Home));
    app.handle_key(key(KeyCode::End));
    app.handle_key(key(KeyCode::PageDown));
    assert!(!app.view.agent_following);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("inv-60"));
    render(&mut app, 100, 30);

    app.request_poll();
    let id = section_request_id(&app.take_commands(), OperatorSection::Agents);
    app.handle_host_message(agents_page(id, 61..=61));
    assert!(!app.view.agent_following);
    assert_eq!(app.view.newer_invocations, 1);
    assert_eq!(app.view.selected_invocation.as_deref(), Some("inv-60"));
    let buffer = render(&mut app, 100, 30);
    assert!(find(&buffer, "paused · 1 newer").is_some());
    assert!(highlighted(&buffer, find(&buffer, "inv-60").unwrap()));
    assert!(find(&buffer, "60/61 ▼").is_some(), "{}", screen(&buffer));

    app.handle_key(key(KeyCode::Char('f')));
    assert!(app.view.agent_following);
    let buffer = render(&mut app, 100, 30);
    assert!(highlighted(&buffer, find(&buffer, "inv-61").unwrap()));
}

/// Inspector content page holding lines `L001`..`L<count>`.
fn tall_page(artifact_id: &str, offset: u64, count: usize) -> HostMessage {
    let mut value = contract("v1", "mission-run-artifact-content.text-page.response.json");
    value["mission_run_id"] = json!(RUN_ID);
    value["artifact_id"] = json!(artifact_id);
    value["offset"] = json!(offset);
    value["next_offset"] = json!(offset + 4096);
    value["byte_size"] = json!(51234);
    let content: String = (1..=count).map(|n| format!("L{n:03}\n")).collect();
    value["content"] = json!(content);
    HostMessage::ArtifactContent {
        purpose: ContentPurpose::Inspector,
        mission_run_id: RUN_ID.to_string(),
        artifact_id: artifact_id.to_string(),
        requested_offset: offset,
        result: Ok(serde_json::from_value(value).unwrap()),
    }
}

#[test]
fn inspector_page_taller_than_the_viewport_scrolls_to_its_last_line() {
    let (mut app, _clock) = run_app("reach-inspector");
    app.handle_key(key(KeyCode::Char('7')));
    let id = section_request_id(&app.take_commands(), OperatorSection::Artifacts);
    app.handle_host_message(artifacts_reply(id));
    let artifact = app.view.selected_artifact.clone().unwrap();
    app.handle_key(key(KeyCode::Enter));
    app.take_commands();
    app.handle_host_message(tall_page(&artifact, 0, 83));

    let buffer = render(&mut app, 100, 30);
    assert!(
        find(&buffer, "line 1/83 · bytes 0-4095 of 51234").is_some(),
        "{}",
        screen(&buffer)
    );
    assert!(find(&buffer, "L001").is_some());
    assert!(
        find(&buffer, "L083").is_none(),
        "the page is taller than the viewport"
    );
    let rows = app.view.inspector.as_ref().unwrap().rows;

    app.handle_key(key(KeyCode::End));
    let buffer = render(&mut app, 100, 30);
    let last = find(&buffer, "L083").unwrap_or_else(|| panic!("{}", screen(&buffer)));
    // The last line sits on the last content row, just above the border.
    assert_eq!(row_text(&buffer, last.1 + 1).chars().next(), Some('└'));
    let top = 83 - rows + 1;
    assert!(find(&buffer, &format!("line {top}/83 ·")).is_some());
    app.handle_key(key(KeyCode::Down));
    let buffer = render(&mut app, 100, 30);
    assert_eq!(
        find(&buffer, "L083"),
        Some(last),
        "Down stops at the last line"
    );

    // Byte-page refreshes and resizes keep the position.
    app.request_poll();
    app.take_commands();
    app.handle_host_message(tall_page(&artifact, 0, 83));
    let buffer = render(&mut app, 100, 30);
    assert_eq!(find(&buffer, "L083"), Some(last));
    let buffer = render(&mut app, 160, 45);
    assert!(find(&buffer, "L083").is_some(), "{}", screen(&buffer));

    app.handle_key(key(KeyCode::Up));
    app.handle_key(key(KeyCode::PageUp));
    let buffer = render(&mut app, 160, 45);
    assert!(find(&buffer, "L083").is_none());
    app.handle_key(key(KeyCode::PageDown));
    app.handle_key(key(KeyCode::Home));
    let buffer = render(&mut app, 100, 30);
    assert!(find(&buffer, "line 1/83 ·").is_some());
    assert!(find(&buffer, "L001").is_some());

    // Left/Right still page by bytes and start the next page at its top.
    app.handle_key(key(KeyCode::End));
    render(&mut app, 100, 30);
    app.handle_key(key(KeyCode::Right));
    assert_eq!(app.view.inspector.as_ref().unwrap().offset, 4096);
    assert_eq!(app.view.inspector.as_ref().unwrap().scroll, 0);
    app.handle_host_message(tall_page(&artifact, 4096, 83));
    let buffer = render(&mut app, 100, 30);
    assert!(
        find(&buffer, "line 1/83 · bytes 4096-8191 of 51234").is_some(),
        "{}",
        screen(&buffer)
    );
    app.handle_key(key(KeyCode::Esc));
    assert!(app.view.inspector.is_none());
}
