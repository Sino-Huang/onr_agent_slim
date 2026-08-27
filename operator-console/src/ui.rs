//! Drawing for the Operator Console. Rendering never performs IO: it only
//! reads [`App`] snapshots produced by the state machine and host worker.
//!
//! Fixed layout at [`MIN_WIDTH`]x[`MIN_HEIGHT`] or larger; smaller terminals
//! render only the resize-required state.

use ratatui::Frame;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};

use crate::app::{
    App, AppState, CancellationState, Liveness, MIN_HEIGHT, MIN_WIDTH, OperatorTab, PaneFocus,
};

const HEADER_ROWS: u16 = 3;
const FOOTER_ROWS: u16 = 3;

fn dim() -> Style {
    Style::default().fg(Color::DarkGray)
}

fn hint() -> Style {
    Style::default().fg(Color::Yellow)
}

fn title_style() -> Style {
    Style::default().add_modifier(Modifier::BOLD)
}

fn status_style(status: &str) -> Style {
    match status {
        "queued" => Style::default().fg(Color::Cyan),
        "running" => Style::default().fg(Color::Green),
        "awaiting_human_decision" => Style::default().fg(Color::Magenta),
        "succeeded" => Style::default().fg(Color::Green),
        "failed" => Style::default().fg(Color::Red),
        "cancelled" => Style::default().fg(Color::Yellow),
        _ => Style::default(),
    }
}

/// Draw the current application state.
pub fn draw(frame: &mut Frame, app: &App) {
    let area = frame.area();
    if matches!(app.state, AppState::ResizeRequired { .. })
        || area.width < MIN_WIDTH
        || area.height < MIN_HEIGHT
    {
        draw_resize_required(frame, area, app.last_size);
        return;
    }
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(HEADER_ROWS),
            Constraint::Min(0),
            Constraint::Length(FOOTER_ROWS),
        ])
        .split(area);
    draw_header(frame, rows[0], app);
    match app.state.name() {
        "Connecting" => draw_connecting(frame, rows[1], app),
        "Editing" => draw_editing(frame, rows[1], app),
        "ReviewActivation" => draw_review(frame, rows[1], app),
        "Submitting" => draw_submitting(frame, rows[1], app),
        "Run" => match &app.cancellation {
            CancellationState::Idle if app.inspector.is_some() => {
                draw_artifact_inspector(frame, rows[1], app)
            }
            CancellationState::Idle if app.operator_view_available() => {
                draw_operator_dashboard(frame, rows[1], app)
            }
            CancellationState::Idle => draw_run_dashboard(frame, rows[1], app),
            CancellationState::Confirming => draw_cancellation_confirmation(frame, rows[1], app),
            CancellationState::Requested { .. } => draw_cancellation_requested(frame, rows[1], app),
        },
        _ => draw_error(frame, rows[1], app),
    }
    draw_footer(frame, rows[2], app);
}

fn draw_header(frame: &mut Frame, area: Rect, app: &App) {
    let api = app
        .health
        .as_ref()
        .map(|h| format!("api v{}.{}", h.api_version.major, h.api_version.minor))
        .unwrap_or_else(|| "api -".to_string());
    let session_short: String = app.session.session_id.chars().take(8).collect();
    let line = Line::from(vec![
        Span::styled(" onr operator console", title_style()),
        Span::raw("  "),
        Span::styled(
            format!("host {} · {api} · session {session_short}", app.host_addr),
            dim(),
        ),
        if app.legacy_view() {
            Span::styled(" · LEGACY VIEW", hint())
        } else {
            Span::raw("")
        },
    ]);
    let block = Block::default().borders(Borders::ALL);
    frame.render_widget(Paragraph::new(line).block(block), area);
}

fn footer_lines(app: &App) -> (String, Option<String>) {
    let keys = match app.state.name() {
        "Editing" => "Enter: newline · Alt+Enter: review activation · Ctrl+C: quit",
        "ReviewActivation" => "Enter: confirm and submit once · Esc: return to editing",
        "Submitting" => "waiting for host acknowledgement · Ctrl+C: quit",
        "Run" => match app.cancellation {
            CancellationState::Idle if app.inspector.is_some() => {
                "Left/p Right/n: page preview · Esc: close"
            }
            CancellationState::Idle => {
                if app.operator_view_available() {
                    match app.active_tab {
                        OperatorTab::Overview => {
                            "1-4/Tab: switch view · c: request cancellation · q: managed exit"
                        }
                        OperatorTab::Agents => {
                            "1-4/Tab: switch · Up/k Down/j: select · f: follow · PgUp/PgDn: detail · c/q"
                        }
                        OperatorTab::Environment => {
                            "1-4/Tab: switch · Up/k Down/j: browse · r: raw evidence · c/q"
                        }
                        OperatorTab::Artifacts => {
                            "1-4/Tab: switch · Up/k Down/j: select · Enter: inspect Artifact · c/q"
                        }
                    }
                } else {
                    "Legacy Host v1.0 · Tab: focus pane · Up/k Down/j: select · Enter: inspect Artifact · c/q"
                }
            }
            CancellationState::Confirming => "Enter: confirm cancellation · Esc: keep running",
            CancellationState::Requested { .. } => {
                "cancellation requested · polling current Mission Run · Ctrl+C: quit"
            }
        },
        "Error" => "Esc: return to editing · r: retry connection · Ctrl+C: quit",
        _ => "Ctrl+C: quit",
    };
    let extra = app.hint.clone().or_else(|| app.notice.clone());
    (keys.to_string(), extra)
}

fn draw_footer(frame: &mut Frame, area: Rect, app: &App) {
    let (keys, extra) = footer_lines(app);
    let mut lines = vec![Line::from(Span::styled(format!(" {keys}"), hint()))];
    if let Some(extra) = extra {
        lines.push(Line::from(Span::styled(format!(" {extra}"), hint())));
    }
    let block = Block::default().borders(Borders::ALL);
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_connecting(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Runtime Host ");
    let lines = vec![
        Line::from(""),
        Line::from(format!(
            " Connecting to Runtime Host at {} ...",
            app.host_addr
        )),
        Line::from(""),
        Line::from(Span::styled(
            " Waiting for health and API version handshake.",
            dim(),
        )),
    ];
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_editing(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Mission Intent ");
    let inner = block.inner(area);
    frame.render_widget(Paragraph::new(app.intent.as_str()).block(block), area);
    let (line, col) = app.cursor_line_col();
    let x = inner.x + (col as u16).min(inner.width.saturating_sub(1));
    let y = inner.y + (line as u16).min(inner.height.saturating_sub(1));
    frame.set_cursor_position((x, y));
}

fn draw_review(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Review Mission Activation ");
    let mut lines = vec![
        Line::from(" Confirm activation of this Mission Intent:"),
        Line::from(""),
    ];
    for line in app.intent.split('\n') {
        lines.push(Line::from(format!("   {line}")));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!(
            " activation request: {}",
            app.review_request_id().unwrap_or("-")
        ),
        dim(),
    )));
    lines.push(Line::from(Span::styled(
        format!(" console session:    {}", app.session.session_id),
        dim(),
    )));
    lines.push(Line::from(Span::styled(
        format!(" source authority:   {}", crate::app::SOURCE_AUTHORITY),
        dim(),
    )));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_submitting(frame: &mut Frame, area: Rect, _app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Mission Activation ");
    let lines = vec![
        Line::from(""),
        Line::from(" Submitting Mission Activation to the Runtime Host ..."),
        Line::from(""),
        Line::from(Span::styled(
            " The host persists the queued Mission Run before acknowledging.",
            dim(),
        )),
    ];
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn field(label: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!(" {label:<10}"), dim()),
        Span::raw(value.to_string()),
    ])
}

fn json_text(value: &serde_json::Value) -> String {
    if value.is_null() {
        "-".to_string()
    } else {
        serde_json::to_string(value).unwrap_or_else(|_| "-".to_string())
    }
}

fn draw_operator_dashboard(frame: &mut Frame, area: Rect, app: &App) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(3), Constraint::Min(0)])
        .split(area);
    draw_operator_tabs(frame, rows[0], app);
    match app.active_tab {
        OperatorTab::Overview => draw_operator_overview(frame, rows[1], app),
        OperatorTab::Agents => draw_operator_agents(frame, rows[1], app),
        OperatorTab::Environment => draw_operator_environment(frame, rows[1], app),
        OperatorTab::Artifacts => draw_operator_artifacts(frame, rows[1], app),
    }
}

fn draw_operator_tabs(frame: &mut Frame, area: Rect, app: &App) {
    let labels = [
        (OperatorTab::Overview, "1 Overview"),
        (OperatorTab::Agents, "2 Agents"),
        (OperatorTab::Environment, "3 Environment"),
        (OperatorTab::Artifacts, "4 Artifacts"),
    ];
    let mut spans = vec![Span::raw(" ")];
    for (tab, label) in labels {
        let style = if app.active_tab == tab {
            Style::default()
                .fg(Color::Black)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            dim()
        };
        spans.push(Span::styled(format!(" {label} "), style));
        spans.push(Span::raw(" "));
    }
    frame.render_widget(
        Paragraph::new(Line::from(spans)).block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Operator View "),
        ),
        area,
    );
}

fn draw_operator_overview(frame: &mut Frame, area: Rect, app: &App) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(9), Constraint::Min(0)])
        .split(area);
    let top = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(45), Constraint::Min(0)])
        .split(rows[0]);
    draw_run_panel(frame, top[0], app);
    draw_operator_phase_cards(frame, top[1], app);
    let bottom = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(68), Constraint::Percentage(32)])
        .split(rows[1]);
    draw_significant_activity(frame, bottom[0], app);
    draw_human_decisions(frame, bottom[1], app);
}

fn draw_operator_phase_cards(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Current Progress ");
    let lines = if let Some(overview) = app.operator_overview.as_ref() {
        let hyper = overview.latest_agents.hyper_agent.as_ref().map_or_else(
            || "no evidence".to_string(),
            |agent| format!("{} · {}", agent.phase, agent.completion_state),
        );
        let maneuver = overview
            .latest_agents
            .maneuver_control
            .as_ref()
            .map_or_else(
                || "no evidence".to_string(),
                |agent| format!("{} · {}", agent.phase, agent.completion_state),
            );
        let narrative = if overview.narrative.status == "unavailable" {
            "unavailable (run state unaffected)"
        } else {
            overview.narrative.status.as_str()
        };
        vec![
            field("Hyper:", &hyper),
            field("Maneuver:", &maneuver),
            field("FSM:", overview.fsm.state.as_deref().unwrap_or("-")),
            field(
                "Mission t:",
                &overview
                    .environment
                    .mission_time_seconds
                    .as_ref()
                    .map_or_else(|| "-".to_string(), ToString::to_string),
            ),
            field(
                "Evidence:",
                &format!(
                    "{} agent · {} event · {} artifact",
                    overview.counts.agents,
                    overview.counts.environment_events,
                    overview.counts.artifacts
                ),
            ),
            field("Narrative:", narrative),
        ]
    } else {
        vec![Line::from(Span::styled(
            " Waiting for operator-view evidence…",
            dim(),
        ))]
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_significant_activity(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Significant Activity ");
    let width = block.inner(area).width.saturating_sub(1) as usize;
    let lines = app.operator_overview.as_ref().map_or_else(
        || vec![Line::from(Span::styled(" No activity received.", dim()))],
        |overview| {
            if overview.recent_events.is_empty() {
                vec![Line::from(Span::styled(
                    " No significant activity recorded.",
                    dim(),
                ))]
            } else {
                overview
                    .recent_events
                    .iter()
                    .rev()
                    .map(|entry| {
                        Line::from(truncate(
                            &format!(
                                " #{} {} · {} · {}",
                                entry.observation_sequence,
                                entry.event_kind,
                                entry.component.as_deref().unwrap_or("unknown"),
                                entry.outcome.as_deref().unwrap_or("recorded")
                            ),
                            width,
                        ))
                    })
                    .collect()
            }
        },
    );
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_operator_agents(frame: &mut Frame, area: Rect, app: &App) {
    let columns = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(38), Constraint::Min(0)])
        .split(area);
    let follow = if app.agent_following {
        "following".to_string()
    } else {
        format!("paused · {} newer", app.newer_invocations)
    };
    let list_block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" Invocations · {follow} "));
    let width = list_block.inner(columns[0]).width.saturating_sub(1) as usize;
    let selected = app.selected_invocation().map(|(index, _)| index);
    let lines = if app.agent_invocations.is_empty() {
        vec![Line::from(Span::styled(
            " No Hyper or Maneuver invocations.",
            dim(),
        ))]
    } else {
        app.agent_invocations
            .iter()
            .enumerate()
            .map(|(index, invocation)| {
                let style = if selected == Some(index) {
                    Style::default().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                };
                Line::from(Span::styled(
                    truncate(
                        &format!(
                            " {} {} [{}] {}",
                            invocation.role,
                            invocation.phase,
                            invocation.completion_state,
                            invocation.name
                        ),
                        width,
                    ),
                    style,
                ))
            })
            .collect()
    };
    frame.render_widget(Paragraph::new(lines).block(list_block), columns[0]);
    draw_invocation_detail(frame, columns[1], app);
}

fn draw_invocation_detail(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Invocation Detail ");
    let Some((_, invocation)) = app.selected_invocation() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " Select an invocation to inspect.",
                dim(),
            )))
            .block(block),
            area,
        );
        return;
    };
    let reasoning = &invocation.recorded_debug_reasoning;
    let mut lines = vec![
        field("ID:", &invocation.invocation_id),
        field("Role:", &invocation.role),
        field("Phase:", &invocation.phase),
        field(
            "Status:",
            &format!("{} / {}", invocation.status, invocation.completion_state),
        ),
        field(
            "Timing:",
            &format!(
                "{} → {} · {} ms",
                invocation.started_at.as_deref().unwrap_or("-"),
                invocation.finished_at.as_deref().unwrap_or("live"),
                invocation
                    .duration_ms
                    .map_or_else(|| "-".to_string(), |value| value.to_string())
            ),
        ),
        Line::from(""),
        Line::from(Span::styled(
            format!(
                " {} (non-authoritative) · {}",
                reasoning.label, reasoning.disposition
            ),
            hint(),
        )),
    ];
    lines.extend(
        reasoning
            .content
            .as_deref()
            .unwrap_or("Debug evidence unavailable.")
            .lines()
            .map(|line| Line::from(format!(" {line}"))),
    );
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(" Response Content", hint())));
    lines.extend(
        invocation
            .content
            .as_deref()
            .unwrap_or("-")
            .lines()
            .map(|line| Line::from(format!(" {line}"))),
    );
    for (index, call) in invocation.tool_calls.iter().enumerate() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            format!(" Tool {}: {}", index + 1, call.name),
            hint(),
        )));
        lines.push(field("Args:", &json_text(&call.args)));
        lines.push(field("Result:", &json_text(&call.result)));
        lines.push(field("Error:", &json_text(&call.error)));
    }
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false })
            .scroll((app.agent_detail_scroll, 0)),
        area,
    );
}

fn draw_operator_environment(frame: &mut Frame, area: Rect, app: &App) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(9), Constraint::Min(0)])
        .split(area);
    let current_block = Block::default()
        .borders(Borders::ALL)
        .title(" Latest Authoritative State ");
    let current_lines = app.operator_environment.as_ref().map_or_else(
        || {
            vec![Line::from(Span::styled(
                " Waiting for environment evidence…",
                dim(),
            ))]
        },
        |environment| {
            vec![
                field("Position:", &json_text(&environment.position)),
                field("Velocity:", &json_text(&environment.velocity)),
                field(
                    "Mission t:",
                    &environment
                        .mission_time_seconds
                        .as_ref()
                        .map_or_else(|| "-".to_string(), ToString::to_string),
                ),
                field("FSM:", environment.fsm_state.as_deref().unwrap_or("-")),
                field("Maneuver:", &json_text(&environment.active_maneuver)),
                field("Feedback:", &json_text(&environment.maneuver_feedback)),
                field("Percepts:", &json_text(&environment.perceptions)),
            ]
        },
    );
    frame.render_widget(Paragraph::new(current_lines).block(current_block), rows[0]);

    let mode = if app.environment_raw {
        "raw"
    } else {
        "filtered"
    };
    let timeline_block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" Operational Timeline · {mode} "));
    let width = timeline_block.inner(rows[1]).width.saturating_sub(1) as usize;
    let selected = app
        .environment_timeline
        .len()
        .checked_sub(1 + usize::from(app.environment_scroll));
    let lines = if app.environment_timeline.is_empty() {
        vec![Line::from(Span::styled(
            " No environment events recorded.",
            dim(),
        ))]
    } else {
        app.environment_timeline
            .iter()
            .enumerate()
            .rev()
            .map(|(index, entry)| {
                let style = if selected == Some(index) {
                    Style::default().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                };
                Line::from(Span::styled(
                    truncate(
                        &format!(
                            " #{} {} · {} · {}",
                            entry.observation_sequence,
                            entry.event_kind,
                            entry.component.as_deref().unwrap_or("unknown"),
                            entry.outcome.as_deref().unwrap_or("recorded")
                        ),
                        width,
                    ),
                    style,
                ))
            })
            .collect()
    };
    frame.render_widget(Paragraph::new(lines).block(timeline_block), rows[1]);
}

fn draw_operator_artifacts(frame: &mut Frame, area: Rect, app: &App) {
    let columns = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(46), Constraint::Percentage(54)])
        .split(area);
    draw_artifacts(frame, columns[0], app);
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Artifact Preview ");
    let lines = match app.selected_artifact() {
        None => vec![Line::from(Span::styled(" No Artifact selected.", dim()))],
        Some((_, artifact)) => vec![
            field("Title:", &artifact.display.title),
            field(
                "Source:",
                artifact.source.as_deref().unwrap_or("public_inbox"),
            ),
            field("Kind:", &artifact.kind),
            field("Media:", &artifact.media_type),
            field(
                "Size:",
                &artifact
                    .byte_size
                    .map(human_bytes)
                    .unwrap_or_else(|| "-".to_string()),
            ),
            field("Ref:", artifact.r#ref.as_deref().unwrap_or("-")),
            field("Published:", &artifact.published_at),
            Line::from(""),
            Line::from(Span::styled(
                artifact.display.summary.as_deref().unwrap_or("No summary."),
                dim(),
            )),
            Line::from(""),
            Line::from(Span::styled(
                " Press Enter to open the paged inspector.",
                hint(),
            )),
        ],
    };
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        columns[1],
    );
}

fn draw_run_dashboard(frame: &mut Frame, area: Rect, app: &App) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(9),
            Constraint::Length(8),
            Constraint::Length(7),
        ])
        .split(area);

    let top = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(49), Constraint::Min(0)])
        .split(rows[0]);
    draw_run_panel(frame, top[0], app);
    draw_activities(frame, top[1], app);

    let conversation_selected = app
        .selected_artifact()
        .is_some_and(|(_, artifact)| artifact.classification == "conversation");
    let middle_constraints = if conversation_selected {
        [
            Constraint::Length(22),
            Constraint::Length(26),
            Constraint::Min(0),
        ]
    } else {
        [
            Constraint::Length(33),
            Constraint::Length(33),
            Constraint::Min(0),
        ]
    };
    let middle = Layout::default()
        .direction(Direction::Horizontal)
        .constraints(middle_constraints)
        .split(rows[1]);
    draw_observations(frame, middle[0], app);
    draw_artifacts(frame, middle[1], app);
    draw_conversation(frame, middle[2], app);

    let bottom = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(49), Constraint::Min(0)])
        .split(rows[2]);
    if app.recovered_owner() {
        draw_recovered_owner_intent(frame, bottom[0], app);
    } else {
        draw_narrative(frame, bottom[0], app);
    }
    draw_human_decisions(frame, bottom[1], app);
}

fn draw_narrative(frame: &mut Frame, area: Rect, app: &App) {
    const UNAVAILABLE_MESSAGE: &str =
        "Run Narrative generation failed; Mission Run state is unaffected.";

    let block = Block::default().borders(Borders::ALL).title(" Narrative ");
    let inner = block.inner(area);
    let content = Rect {
        x: inner.x.saturating_add(1),
        width: inner.width.saturating_sub(1),
        ..inner
    };
    let status = app
        .narrative
        .as_ref()
        .map(|narrative| narrative.status.as_str());
    let lines = match status {
        Some("available") => {
            let mut lines = vec![Line::from(Span::styled("(non-authoritative)", dim()))];
            if let Some(text) = app
                .narrative
                .as_ref()
                .and_then(|narrative| narrative.text.as_deref())
            {
                lines.extend(text.lines().map(|line| Line::from(line.to_string())));
            }
            lines
        }
        Some("unavailable") => vec![
            Line::from("Run Narrative unavailable."),
            Line::from(Span::styled(UNAVAILABLE_MESSAGE, dim())),
        ],
        _ => vec![Line::from(Span::styled(
            "No Run Narrative generated.",
            dim(),
        ))],
    };
    frame.render_widget(block, area);
    if matches!(status, Some("available") | Some("unavailable")) {
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), content);
    } else {
        frame.render_widget(Paragraph::new(lines), content);
    }
}

fn draw_run_panel(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Mission Run ");
    let lines = match app.run.as_ref() {
        Some(run) => {
            let mut status = vec![
                Span::styled(format!(" {:<10}", "Status:"), dim()),
                Span::styled(run.status.clone(), status_style(&run.status)),
            ];
            if matches!(app.cancellation, CancellationState::Requested { .. }) {
                status.push(Span::styled(" · cancellation requested", hint()));
            }
            let mut lines = vec![Line::from(status)];
            match app.liveness() {
                Liveness::Live => {}
                Liveness::Stale => lines.push(Line::from(Span::styled(
                    " stale - showing last received evidence",
                    hint(),
                ))),
                Liveness::Offline => lines.push(Line::from(Span::styled(
                    " offline - showing last received evidence",
                    Style::default().fg(Color::Red),
                ))),
            }
            lines.extend([
                field("Mission:", &run.mission_id),
                field("Run:", &run.mission_run_id),
                field("Created:", run.created_at.as_deref().unwrap_or("-")),
                field("Started:", run.started_at.as_deref().unwrap_or("-")),
                field("Finished:", run.finished_at.as_deref().unwrap_or("-")),
            ]);
            if app.liveness() == Liveness::Live {
                lines.push(field(
                    "Terminal:",
                    run.terminal_classification.as_deref().unwrap_or("-"),
                ));
            }
            lines
        }
        None => vec![Line::from(Span::styled(" No current Mission Run.", dim()))],
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn truncate(value: &str, width: usize) -> String {
    if value.chars().count() <= width {
        return value.to_string();
    }
    if width <= 1 {
        return "…".chars().take(width).collect();
    }
    let mut result: String = value.chars().take(width - 1).collect();
    result.push('…');
    result
}

fn draw_activities(frame: &mut Frame, area: Rect, app: &App) {
    let title = if app.pane_focus == PaneFocus::Activities && !app.artifacts.is_empty() {
        " * Run Activities "
    } else {
        " Run Activities "
    };
    let block = Block::default().borders(Borders::ALL).title(title);
    let width = block.inner(area).width.saturating_sub(1) as usize;
    let selected = app.selected_activity().map(|(index, _)| index);
    let lines = if app.activities.is_empty() {
        vec![Line::from(Span::styled(
            " No Run Activities recorded.",
            dim(),
        ))]
    } else {
        app.activities
            .iter()
            .enumerate()
            .map(|(index, activity)| {
                let text = format!(
                    " #{} {} [{}] {}",
                    activity.activity_sequence, activity.kind, activity.status, activity.summary
                );
                let style = if app.pane_focus == PaneFocus::Activities && selected == Some(index) {
                    Style::default().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                };
                Line::from(Span::styled(truncate(&text, width), style))
            })
            .collect()
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn human_bytes(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    if bytes < 1024 {
        return format!("{bytes} B");
    }
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit + 1 < UNITS.len() {
        value /= 1024.0;
        unit += 1;
    }
    format!("{value:.1} {}", UNITS[unit])
}

fn draw_artifacts(frame: &mut Frame, area: Rect, app: &App) {
    let focused = if app.operator_view_available() {
        app.active_tab == OperatorTab::Artifacts
    } else {
        app.pane_focus == PaneFocus::Artifacts
    };
    let title = if focused {
        " * Artifacts "
    } else {
        " Artifacts "
    };
    let block = Block::default().borders(Borders::ALL).title(title);
    let width = block.inner(area).width.saturating_sub(1) as usize;
    let selected = app.selected_artifact().map(|(index, _)| index);
    let lines = if app.artifacts.is_empty() {
        vec![
            Line::from(Span::styled(" No Artifacts published.", dim())),
            Line::from(Span::styled(" (reserved for a later view)", dim())),
        ]
    } else {
        app.artifacts
            .iter()
            .enumerate()
            .map(|(index, artifact)| {
                let size = artifact
                    .byte_size
                    .map_or_else(|| artifact.classification.clone(), human_bytes);
                let text = format!(" {} {} ({size})", artifact.kind, artifact.display.title);
                let style = if focused && selected == Some(index) {
                    Style::default().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                };
                Line::from(Span::styled(truncate(&text, width), style))
            })
            .collect()
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_conversation(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Conversation ");
    let width = block.inner(area).width as usize;
    let selected_conversation = app
        .selected_artifact()
        .is_some_and(|(_, artifact)| artifact.classification == "conversation");
    let lines = if app.artifacts.is_empty() {
        vec![
            Line::from(Span::styled(" No Conversation recorded.", dim())),
            Line::from(Span::styled(" (reserved for a later view)", dim())),
        ]
    } else if !selected_conversation {
        vec![Line::from(Span::styled(
            " No Conversation selected.",
            dim(),
        ))]
    } else if app.conversation_entries.is_empty() {
        vec![Line::from(Span::styled(
            " No Conversation entries recorded.",
            dim(),
        ))]
    } else {
        app.conversation_entries
            .iter()
            .map(|entry| {
                let text = if let Some(reference) = entry.content_ref.as_ref() {
                    format!(
                        " #{} {} [{}] [ref] {} ({})",
                        entry.sequence,
                        entry.author,
                        entry.kind,
                        reference.path,
                        human_bytes(reference.byte_size)
                    )
                } else {
                    let first_line = entry
                        .content
                        .as_deref()
                        .unwrap_or("")
                        .lines()
                        .next()
                        .unwrap_or("");
                    format!(
                        " #{} {} [{}] {first_line}",
                        entry.sequence, entry.author, entry.kind
                    )
                };
                Line::from(truncate(&text, width))
            })
            .collect()
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_artifact_inspector(frame: &mut Frame, area: Rect, app: &App) {
    let Some(inspector) = app.inspector.as_ref() else {
        return;
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" Artifact: {} ", inspector.artifact_id));
    let Some(page) = inspector.page.as_ref() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " Loading Artifact preview…",
                dim(),
            )))
            .block(block),
            area,
        );
        return;
    };
    if inspector.classification == "binary" {
        let descriptor = app
            .artifacts
            .iter()
            .find(|artifact| artifact.artifact_id == inspector.artifact_id);
        let mut lines = Vec::new();
        if let Some(artifact) = descriptor {
            lines.extend([
                field("Kind:", &artifact.kind),
                field("Media:", &artifact.media_type),
                field(
                    "Size:",
                    &artifact
                        .byte_size
                        .map(human_bytes)
                        .unwrap_or_else(|| "-".to_string()),
                ),
                field("Digest:", artifact.content_digest.as_deref().unwrap_or("-")),
                Line::from(vec![
                    Span::styled(" Published: ", dim()),
                    Span::raw(artifact.published_at.clone()),
                ]),
                field("Title:", &artifact.display.title),
                field(
                    "Summary:",
                    artifact.display.summary.as_deref().unwrap_or("-"),
                ),
                Line::from(""),
            ]);
        }
        lines.push(Line::from(Span::styled(
            " Binary Artifact: metadata only (no content bytes).",
            dim(),
        )));
        frame.render_widget(Paragraph::new(lines).block(block), area);
        return;
    }

    let content = page.content.as_deref().unwrap_or("");
    let end = page
        .next_offset
        .or(page.byte_size.filter(|_| page.eof))
        .unwrap_or_else(|| page.offset.saturating_add(content.len() as u64));
    let total = page
        .byte_size
        .map_or_else(|| "?".to_string(), |size| size.to_string());
    let mut status = format!(" bytes {}-{end} of {total}", page.offset);
    if page.eof {
        status.push_str(" · end of content");
    }
    if page.truncated {
        status.push_str(" · page truncated at UTF-8 boundary");
    }
    let mut lines = vec![Line::from(Span::styled(status, dim())), Line::from("")];
    lines.extend(content.split('\n').map(|line| Line::from(line.to_string())));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_observations(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Observations ");
    let inner = block.inner(area);
    let Some((_, activity)) = app.selected_activity() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " No Run Observations recorded.",
                dim(),
            )))
            .block(block),
            area,
        );
        return;
    };
    let linked = app.selected_observations();
    let width = inner.width.saturating_sub(1) as usize;
    let mut lines = vec![
        Line::from(truncate(&format!(" id: {}", activity.activity_id), width)),
        Line::from(truncate(
            &format!(" {} [{}]", activity.kind, activity.status),
            width,
        )),
        Line::from(truncate(
            &format!(
                " corr: {} · {} -> {}",
                activity.correlation_id.as_deref().unwrap_or("-"),
                activity.started_at.as_deref().unwrap_or("-"),
                activity.finished_at.as_deref().unwrap_or("-")
            ),
            width,
        )),
    ];
    for observation in &linked {
        if lines.len() >= inner.height as usize {
            break;
        }
        lines.push(Line::from(truncate(
            &format!(
                " #{} {} {} {}",
                observation.observation_sequence,
                observation.item.event_kind,
                observation.item.component,
                observation.item.outcome.as_deref().unwrap_or("-")
            ),
            width,
        )));
    }
    if lines.len() < inner.height as usize
        && let Some(first) = linked.first()
    {
        let payload = first
            .item
            .payload
            .get("target_service")
            .map(|value| serde_json::json!({"target": value}).to_string())
            .unwrap_or_else(|| {
                serde_json::to_string(&first.item.payload).unwrap_or_else(|_| "{}".to_string())
            });
        lines.push(Line::from(Span::styled(
            truncate(&format!(" {payload}"), width),
            dim(),
        )));
    }
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_recovered_owner_intent(frame: &mut Frame, area: Rect, app: &App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Recovered Owner ");
    let mut lines = vec![
        Line::from(Span::styled(" Recovered owner session", hint())),
        Line::from(""),
        Line::from(Span::styled(" Mission Intent:", dim())),
    ];
    if app.intent.is_empty() {
        lines.push(Line::from(Span::styled(" Intent has not loaded.", dim())));
    } else {
        lines.extend(
            app.intent
                .lines()
                .map(|line| Line::from(format!(" {line}"))),
        );
    }
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_cancellation_confirmation(frame: &mut Frame, area: Rect, app: &App) {
    let run_id = app
        .run
        .as_ref()
        .map(|run| run.mission_run_id.as_str())
        .unwrap_or("current run");
    let lines = vec![
        Line::from(""),
        Line::from(""),
        Line::from(format!(" Request cancellation of Mission Run {run_id}?")),
        Line::from(""),
        Line::from(Span::styled(
            " The Runtime Host records cancellation-requested and stops the",
            dim(),
        )),
        Line::from(Span::styled(
            " local Run Worker tree before reporting terminal cancelled.",
            dim(),
        )),
        Line::from(Span::styled(
            " The environment and submitted Maneuver Commands are untouched.",
            dim(),
        )),
        Line::from(""),
        Line::from(Span::styled(
            " Enter: confirm cancellation · Esc: keep running",
            hint(),
        )),
    ];
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Cancel Mission Run "),
        ),
        area,
    );
}

fn draw_cancellation_requested(frame: &mut Frame, area: Rect, app: &App) {
    let CancellationState::Requested {
        cancellation_request_id,
    } = &app.cancellation
    else {
        return;
    };
    let run_id = app
        .run
        .as_ref()
        .map(|run| run.mission_run_id.as_str())
        .unwrap_or("current run");
    let lines = vec![
        Line::from(""),
        Line::from(""),
        Line::from(format!(" Cancellation requested for Mission Run {run_id}.")),
        Line::from(""),
        Line::from(Span::styled(
            format!(" Request: {cancellation_request_id}"),
            dim(),
        )),
        Line::from(Span::styled(
            " Run status remains current until the Host reports terminal cancelled.",
            dim(),
        )),
        Line::from(""),
        Line::from(Span::styled(
            " Polling current Mission Run for Host confirmation.",
            hint(),
        )),
    ];
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Cancellation Requested "),
        ),
        area,
    );
}

/// The permanent HITL surface (issue #32): a status-only Human Decisions
/// placeholder driven solely by the Host's public, versioned Mission Run
/// Status. It binds no keys, offers no controls, and renders identically for
/// owner and observer consoles; decision submission is a later delivery.
fn draw_human_decisions(frame: &mut Frame, area: Rect, app: &App) {
    const AWAITING_HUMAN_DECISION: &str = "awaiting_human_decision";
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Human Decisions ");
    let awaiting = app
        .run
        .as_ref()
        .is_some_and(|run| run.status == AWAITING_HUMAN_DECISION);
    let lines = if awaiting {
        vec![
            Line::from(Span::styled(
                " AWAITING HUMAN DECISION",
                status_style(AWAITING_HUMAN_DECISION),
            )),
            Line::from(Span::styled(" Status: awaiting_human_decision", dim())),
            Line::from(Span::styled(
                " The Mission Run is paused, awaiting a Human",
                dim(),
            )),
            Line::from(Span::styled(" Decision.", dim())),
            Line::from(Span::styled(
                " Status-only view: no decision controls.",
                dim(),
            )),
        ]
    } else {
        vec![
            Line::from(Span::styled(
                " No Human Decision Requests require action.",
                dim(),
            )),
            Line::from(Span::styled(
                " Status-only view: no decision controls.",
                dim(),
            )),
        ]
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_error(frame: &mut Frame, area: Rect, app: &App) {
    let message = match &app.state {
        AppState::Error { message, .. } => message.clone(),
        _ => String::new(),
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .title(Span::styled(" Error ", Style::default().fg(Color::Red)));
    let lines = vec![
        Line::from(""),
        Line::from(format!(" {message}")),
        Line::from(""),
        Line::from(Span::styled(
            " The Runtime Host keeps its own state; no console state was lost.",
            dim(),
        )),
    ];
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_resize_required(frame: &mut Frame, area: Rect, last_size: (u16, u16)) {
    frame.render_widget(Clear, area);
    let lines = vec![
        Line::from(Span::styled(
            "Terminal too small",
            Style::default().fg(Color::Red).add_modifier(Modifier::BOLD),
        )),
        Line::from(""),
        Line::from(format!(
            "The operator console requires at least {MIN_WIDTH}x{MIN_HEIGHT}."
        )),
        Line::from(format!("Current: {}x{}", last_size.0, last_size.1)),
    ];
    let width = 52u16.min(area.width);
    let height = 6u16.min(area.height);
    let x = area.x + area.width.saturating_sub(width) / 2;
    let y = area.y + area.height.saturating_sub(height) / 2;
    let centered = Rect::new(x, y, width, height);
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Resize Required ");
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .alignment(ratatui::layout::Alignment::Center),
        centered,
    );
}
