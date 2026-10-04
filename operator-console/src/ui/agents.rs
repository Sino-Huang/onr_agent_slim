//! Agents tab: Hyper and Maneuver invocations plus the selected detail with
//! Recorded Debug Reasoning labelled non-authoritative.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::layout::{Breakpoint, json_text, scrolling_list, truncate, wrapped, wrapped_field};
use super::theme::{Badge, Theme};
use crate::app::App;
use crate::host::OperatorAgentInvocation;

pub(crate) fn role_badge(role: &str) -> Badge {
    if role.contains("maneuver") {
        Badge::Man
    } else {
        Badge::Hyp
    }
}

pub fn draw_agents(
    frame: &mut Frame,
    area: Rect,
    app: &mut App,
    theme: Theme,
    breakpoint: Breakpoint,
) {
    let [list, detail] = Layout::horizontal([
        Constraint::Length(breakpoint.pick(40, 56, 64)),
        Constraint::Min(0),
    ])
    .areas(area);
    let selected = app.view.selected_invocation().map(|(index, _)| index);
    let view = &mut app.view;
    let follow = if view.agent_following {
        "following".to_string()
    } else {
        format!("paused · {} newer", view.newer_invocations)
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" Invocations · {follow} "));
    if view.agents.is_empty() {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " No Hyper or Maneuver invocations.",
                theme.dim(),
            )))
            .block(block),
            list,
        );
    } else {
        let width = block.inner(list).width.saturating_sub(5) as usize;
        let agents = &view.agents;
        let rows = scrolling_list(
            block,
            list,
            theme,
            agents.len(),
            selected,
            &mut view.agent_list_offset,
            |index| {
                let invocation = &agents[index];
                let text = truncate(
                    &format!(
                        " {} [{}] {}",
                        invocation.phase, invocation.completion_state, invocation.name
                    ),
                    width,
                );
                let style = if selected == Some(index) {
                    theme.selected()
                } else {
                    ratatui::style::Style::default()
                };
                Line::from(vec![
                    Span::raw(" "),
                    theme.badge(role_badge(&invocation.role)),
                    Span::styled(text, style),
                ])
            },
        );
        frame.render_widget(rows, list);
    }
    draw_invocation_detail(frame, detail, app, theme);
}

fn draw_invocation_detail(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Invocation Detail ");
    let Some((_, invocation)) = app.view.selected_invocation() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " Select an invocation to inspect.",
                theme.dim(),
            )))
            .block(block),
            area,
        );
        return;
    };
    let width = block.inner(area).width as usize;
    frame.render_widget(
        Paragraph::new(invocation_lines(invocation, theme, width))
            .block(block)
            .scroll((app.view.agent_detail_scroll, 0)),
        area,
    );
}

/// Invocation detail rows: identity, status and timing, the recorded
/// (non-authoritative) reasoning, the response content and tool calls.
pub(crate) fn invocation_lines(
    invocation: &OperatorAgentInvocation,
    theme: Theme,
    width: usize,
) -> Vec<Line<'static>> {
    let reasoning = &invocation.recorded_debug_reasoning;
    let mut lines = Vec::new();
    for (label, value) in [
        ("ID:", invocation.invocation_id.clone()),
        ("Role:", invocation.role.clone()),
        ("Phase:", invocation.phase.clone()),
        (
            "Status:",
            format!("{} / {}", invocation.status, invocation.completion_state),
        ),
        (
            "Timing:",
            format!(
                "{} → {} · {} ms",
                invocation.started_at.as_deref().unwrap_or("-"),
                invocation.finished_at.as_deref().unwrap_or("live"),
                invocation
                    .duration_ms
                    .map_or_else(|| "-".to_string(), |value| value.to_string())
            ),
        ),
    ] {
        lines.extend(wrapped_field(theme, label, &value, width));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(vec![
        Span::raw(" "),
        theme.ai_badge(),
        Span::styled(
            truncate(
                &format!(
                    " {} (non-authoritative) · {}",
                    reasoning.label, reasoning.disposition
                ),
                width.saturating_sub(3),
            ),
            theme.hint(),
        ),
    ]));
    lines.extend(wrapped(
        reasoning
            .content
            .as_deref()
            .unwrap_or("Debug evidence unavailable."),
        width,
        1,
        theme.ai(),
    ));
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(" Response Content", theme.hint())));
    lines.extend(wrapped(
        invocation.content.as_deref().unwrap_or("-"),
        width,
        1,
        ratatui::style::Style::default(),
    ));
    for (index, call) in invocation.tool_calls.iter().enumerate() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            format!(" Tool {}: {}", index + 1, call.name),
            theme.hint(),
        )));
        lines.extend(wrapped_field(theme, "Args:", &json_text(&call.args), width));
        lines.extend(wrapped_field(
            theme,
            "Result:",
            &json_text(&call.result),
            width,
        ));
        lines.extend(wrapped_field(
            theme,
            "Error:",
            &json_text(&call.error),
            width,
        ));
    }
    lines
}
