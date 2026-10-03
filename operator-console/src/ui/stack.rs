//! Stack tab: Environment Stack services with state marks and the selected
//! service's auto-following log tail.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::layout::{Breakpoint, field, human_bytes, truncate};
use super::theme::{Badge, Importance, Theme};
use crate::app::App;

fn service_badge(name: &str) -> Badge {
    if name.contains("perception") {
        Badge::Per
    } else if name.contains("engine") || name.contains("physical") || name.contains("mission4") {
        Badge::Env
    } else {
        Badge::Stk
    }
}

pub fn draw_stack(frame: &mut Frame, area: Rect, app: &App, theme: Theme, breakpoint: Breakpoint) {
    let view = &app.view.stack;
    let service_rows = view
        .stack
        .as_ref()
        .map_or(1, |stack| stack.services.len().max(1)) as u16;
    let (services, log) = if breakpoint == Breakpoint::Compact {
        let [services, log] = Layout::vertical([
            Constraint::Length((service_rows + 7).min(area.height / 2)),
            Constraint::Min(0),
        ])
        .areas(area);
        (services, log)
    } else {
        let [services, log] = Layout::horizontal([
            Constraint::Length(breakpoint.pick(56, 60, 72)),
            Constraint::Min(0),
        ])
        .areas(area);
        (services, log)
    };
    draw_services(frame, services, app, theme);
    draw_log(frame, log, app, theme);
}

fn draw_services(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let view = &app.view.stack;
    let Some(stack) = view.stack.as_ref() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " Waiting for the Host stack section…",
                theme.dim(),
            )))
            .block(Block::default().borders(Borders::ALL).title(" Services ")),
            area,
        );
        return;
    };
    let toggles = &stack.toggles;
    let block = Block::default().borders(Borders::ALL).title(format!(
        " Services · {} · AirSim {} · perception {} ",
        stack.preset_id,
        if toggles.airsim { "on" } else { "off" },
        toggles.perception
    ));
    let width = block.inner(area).width.saturating_sub(8) as usize;
    let selected = view.selected_service().map(|(index, _)| index);
    let mut lines: Vec<Line> = stack
        .services
        .iter()
        .enumerate()
        .map(|(index, service)| {
            let mut text = format!("{:<18} {:<8}", service.name, service.state);
            if let Some(port) = service.port {
                text.push_str(&format!(" :{port}"));
            }
            if let Some(pid) = service.pid {
                text.push_str(&format!(" pid {pid}"));
            }
            if !service.required {
                text.push_str(" (optional)");
            }
            let style = if selected == Some(index) {
                theme.selected()
            } else {
                ratatui::style::Style::default()
            };
            Line::from(vec![
                Span::raw(" "),
                theme.service_mark(&service.state),
                Span::raw(" "),
                theme.badge(service_badge(&service.name)),
                Span::styled(format!(" {}", truncate(&text, width)), style),
            ])
        })
        .collect();
    if stack.services.is_empty() {
        lines.push(Line::from(Span::styled(
            " No services in this stack plan.",
            theme.dim(),
        )));
    }
    if let Some((_, service)) = view.selected_service() {
        let importance = Importance::parse(&service.importance).unwrap_or(Importance::Routine);
        lines.push(Line::from(""));
        lines.push(field(
            theme,
            "Started:",
            service.started_at.as_deref().unwrap_or("-"),
        ));
        lines.push(field(
            theme,
            "Ready:",
            service.ready_at.as_deref().unwrap_or("-"),
        ));
        lines.push(field(
            theme,
            "Exit:",
            &service
                .exit_code
                .map_or_else(|| "-".to_string(), |code| code.to_string()),
        ));
        lines.push(Line::from(vec![
            Span::styled(format!(" {:<11}", "Last line:"), theme.dim()),
            theme.importance_mark(importance),
            Span::styled(
                format!(
                    " {}",
                    truncate(service.last_line.as_deref().unwrap_or("-"), width)
                ),
                theme.importance(importance),
            ),
        ]));
    }
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_log(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let view = &app.view.stack;
    let name = view
        .selected_service()
        .map_or("no service", |(_, service)| service.name.as_str());
    let Some(tail) = view.tail.as_ref() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " The selected service has no log Artifact.",
                theme.dim(),
            )))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(format!(" Log · {name} ")),
            ),
            area,
        );
        return;
    };
    let mode = if view.follow {
        "following".to_string()
    } else {
        format!("paused −{}", view.scroll_back)
    };
    let size = tail
        .byte_size
        .map(human_bytes)
        .unwrap_or_else(|| "?".to_string());
    let block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" Log · {name} · {size} · {mode} "));
    let inner = block.inner(area);
    let height = inner.height as usize;
    let width = inner.width.saturating_sub(1) as usize;
    let mut lines: Vec<&str> = tail.visible_lines().collect();
    let mut status = None;
    if let Some(error) = tail.error.as_deref() {
        status = Some(Line::from(Span::styled(
            format!(" log read failed: {error}"),
            theme.error(),
        )));
    } else if lines.is_empty() {
        status = Some(Line::from(Span::styled(" (no log lines yet)", theme.dim())));
    }
    let reserve = usize::from(status.is_some());
    let visible = height.saturating_sub(reserve);
    let scroll_back = if view.follow {
        0
    } else {
        usize::from(view.scroll_back)
    };
    let end = lines.len().saturating_sub(scroll_back);
    let start = end.saturating_sub(visible);
    lines.truncate(end);
    let mut rendered: Vec<Line> = lines[start..]
        .iter()
        .map(|line| Line::from(format!(" {}", truncate(line, width))))
        .collect();
    rendered.extend(status);
    frame.render_widget(Paragraph::new(rendered).block(block), area);
}
