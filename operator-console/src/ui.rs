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

use crate::app::{App, AppState, CancellationState, Liveness, MIN_HEIGHT, MIN_WIDTH};

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
            CancellationState::Idle => {
                "Up/k Down/j: select activity · c: request cancellation · Ctrl+C: quit"
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
    frame.render_widget(Paragraph::new(lines).block(block), area);
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

    let middle = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(33),
            Constraint::Length(33),
            Constraint::Min(0),
        ])
        .split(rows[1]);
    draw_observations(frame, middle[0], app);
    draw_reserved(frame, middle[1], " Artifacts ", "No Artifacts published.");
    draw_reserved(
        frame,
        middle[2],
        " Conversation ",
        "No Conversation recorded.",
    );

    let bottom = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(49), Constraint::Min(0)])
        .split(rows[2]);
    if app.recovered_owner() {
        draw_recovered_owner_intent(frame, bottom[0], app);
    } else {
        draw_reserved(
            frame,
            bottom[0],
            " Narrative ",
            "No Run Narrative generated.",
        );
    }
    draw_reserved(
        frame,
        bottom[1],
        " Human Decisions ",
        "No Human Decision Requests require action.",
    );
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
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Run Activities ");
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
                let style = if selected == Some(index) {
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

fn draw_reserved(frame: &mut Frame, area: Rect, title: &str, empty: &str) {
    let block = Block::default().borders(Borders::ALL).title(title);
    let lines = vec![
        Line::from(Span::styled(format!(" {empty}"), dim())),
        Line::from(Span::styled(" (reserved for a later view)", dim())),
    ];
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
