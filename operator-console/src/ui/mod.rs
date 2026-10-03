//! Drawing for the Operator Console. Rendering never performs IO: it only
//! reads [`App`] snapshots produced by the state machine and workers.
//!
//! The layout adapts to the terminal (see [`layout::Breakpoint`]) from the
//! 100x30 minimum; smaller terminals render only the resize-required state.

pub mod agents;
pub mod artifacts;
pub mod belief_context;
pub mod launch;
pub mod layout;
pub mod overlays;
pub mod overview;
pub mod progress;
pub mod stack;
pub mod theme;
pub mod world;

use std::sync::LazyLock;

use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};

use crate::app::{App, AppState, MIN_HEIGHT, MIN_WIDTH, RunTab};
use layout::Breakpoint;
use theme::{Importance, Theme};

/// Theme from the environment, read once.
static ENV_THEME: LazyLock<Theme> = LazyLock::new(Theme::from_env);

/// Draw the current application state with the environment theme.
pub fn draw(frame: &mut Frame, app: &mut App) {
    draw_with_theme(frame, app, *ENV_THEME);
}

/// Draw the current application state with an explicit theme.
pub fn draw_with_theme(frame: &mut Frame, app: &mut App, theme: Theme) {
    let area = frame.area();
    if matches!(app.state, AppState::ResizeRequired { .. })
        || area.width < MIN_WIDTH
        || area.height < MIN_HEIGHT
    {
        draw_resize_required(frame, area, app.last_size, theme);
        return;
    }
    let breakpoint = Breakpoint::of(area);
    match &app.state {
        AppState::Launch => launch::draw_launch(frame, area, app, theme, breakpoint),
        AppState::ReviewActivation => launch::draw_review(frame, area, app, theme),
        AppState::Run => draw_run(frame, area, app, theme, breakpoint),
        AppState::Connecting => draw_notice_screen(
            frame,
            area,
            app,
            theme,
            " Runtime Host ",
            vec![
                Line::from(format!(
                    " Connecting to Runtime Host at {} ...",
                    app.host_addr
                )),
                Line::from(""),
                Line::from(Span::styled(
                    " Waiting for health and API version handshake (requires API v1.2+).",
                    theme.dim(),
                )),
            ],
            "Ctrl+C: quit",
        ),
        AppState::Submitting => draw_notice_screen(
            frame,
            area,
            app,
            theme,
            " Mission Activation ",
            vec![
                Line::from(" Submitting Mission Activation to the Runtime Host ..."),
                Line::from(""),
                Line::from(Span::styled(
                    " The Host persists the queued Mission Run before acknowledging.",
                    theme.dim(),
                )),
            ],
            "waiting for Host acknowledgement · Ctrl+C: quit",
        ),
        AppState::Error {
            message,
            retry_connect,
        } => draw_notice_screen(
            frame,
            area,
            app,
            theme,
            " Error ",
            vec![
                Line::from(Span::styled(format!(" {message}"), theme.error())),
                Line::from(""),
                Line::from(Span::styled(
                    " The Runtime Host keeps its own state; no console state was lost.",
                    theme.dim(),
                )),
            ],
            match (*retry_connect, app.health.is_some()) {
                (true, true) => "r: retry connection · Esc: Launch screen · q/Ctrl+C: quit",
                (true, false) => "r: retry connection · q/Ctrl+C: quit",
                (false, _) => "Esc: return to the Launch screen · q/Ctrl+C: quit",
            },
        ),
        AppState::ResizeRequired { .. } => unreachable!("handled above"),
    }
    if app.help_open {
        overlays::draw_help(frame, area, theme);
    }
}

/// Console title line shared by every screen.
pub(crate) fn title_line(app: &App, theme: Theme) -> Line<'static> {
    let api = app.health.as_ref().map_or_else(
        || "api -".to_string(),
        |health| format!("v{}.{}", health.api_version.major, health.api_version.minor),
    );
    let session: String = app.session.session_id.chars().take(8).collect();
    let mut spans = vec![
        Span::styled(" ONR Operator Console", theme.title()),
        Span::styled(
            format!(" ─ host {} {api}", host_authority(app)),
            theme.dim(),
        ),
    ];
    if let Some(vllm) = app.launch.preflight.as_ref().and_then(|preflight| {
        preflight
            .checks
            .iter()
            .find(|check| check.check_id == "vllm")
    }) {
        spans.push(Span::styled(" ─ vLLM ", theme.dim()));
        spans.push(theme.check_mark(&vllm.status));
        if let Some(model) = vllm.detail.as_deref() {
            let model = model.split(" at ").next().unwrap_or(model);
            let model = model.rsplit('/').next().unwrap_or(model);
            spans.push(Span::styled(format!(" {model}"), theme.dim()));
        }
    }
    spans.push(Span::styled(format!(" ─ session {session}"), theme.dim()));
    Line::from(spans)
}

fn host_authority(app: &App) -> &str {
    app.host_addr
        .strip_prefix("http://")
        .unwrap_or(&app.host_addr)
}

/// Footer: a separator, the key line, and a status line.
pub(crate) fn draw_footer(
    frame: &mut Frame,
    area: Rect,
    theme: Theme,
    keys: &str,
    status: Line<'static>,
) {
    let lines = vec![
        Line::from(Span::styled(format!(" {keys}"), theme.hint())),
        status,
    ];
    frame.render_widget(
        Paragraph::new(lines).block(Block::default().borders(Borders::TOP)),
        area,
    );
}

/// Footer status: hint, then notice, else `fallback`.
pub(crate) fn status_line(app: &App, theme: Theme, fallback: Line<'static>) -> Line<'static> {
    match app.hint.as_ref().or(app.notice.as_ref()) {
        Some(message) => Line::from(Span::styled(format!(" {message}"), theme.hint())),
        None => fallback,
    }
}

/// Importance legend with optional counts.
pub(crate) fn legend_line(app: &App, theme: Theme) -> Line<'static> {
    let counts = app
        .view
        .overview
        .as_ref()
        .and_then(|overview| overview.counts_by_importance);
    let mut spans = vec![Span::raw(" ")];
    for importance in Importance::ALL {
        spans.push(theme.importance_mark(importance));
        let count = counts.map(|counts| match importance {
            Importance::Critical => counts.critical,
            Importance::Warning => counts.warning,
            Importance::Notable => counts.notable,
            Importance::Routine => counts.routine,
            Importance::Debug => counts.debug,
        });
        let text = match count {
            Some(count) => format!(" {} {count}  ", importance.name()),
            None => format!(" {}  ", importance.name()),
        };
        spans.push(Span::styled(text, theme.dim()));
    }
    Line::from(spans)
}

#[allow(clippy::too_many_arguments)]
fn draw_notice_screen(
    frame: &mut Frame,
    area: Rect,
    app: &App,
    theme: Theme,
    title: &'static str,
    mut lines: Vec<Line<'static>>,
    keys: &str,
) {
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(0),
        Constraint::Length(3),
    ])
    .areas(area);
    frame.render_widget(Paragraph::new(title_line(app, theme)), header);
    lines.insert(0, Line::from(""));
    frame.render_widget(
        Paragraph::new(lines)
            .block(Block::default().borders(Borders::ALL).title(title))
            .wrap(Wrap { trim: false }),
        body,
    );
    draw_footer(
        frame,
        footer,
        theme,
        keys,
        status_line(app, theme, Line::from("")),
    );
}

fn draw_run(frame: &mut Frame, area: Rect, app: &mut App, theme: Theme, breakpoint: Breakpoint) {
    let [header, tabs, stepper, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(0),
        Constraint::Length(3),
    ])
    .areas(area);
    frame.render_widget(
        Paragraph::new(overview::header_strip(app, theme, header.width)),
        header,
    );
    frame.render_widget(Paragraph::new(overview::tab_bar(app, theme)), tabs);
    frame.render_widget(Paragraph::new(overview::phase_stepper(app, theme)), stepper);
    if app.view.inspector.is_some() {
        artifacts::draw_inspector(frame, body, app, theme);
    } else {
        match app.view.tab {
            RunTab::Overview => overview::draw_overview(frame, body, app, theme, breakpoint),
            RunTab::Progress => progress::draw_progress(frame, body, &mut app.view.progress, theme),
            RunTab::Agents => agents::draw_agents(frame, body, app, theme, breakpoint),
            RunTab::BeliefContext => belief_context::draw_belief_context(
                frame,
                body,
                app.view.beliefs.as_ref(),
                app.view.selected_belief_entity.as_deref(),
                app.view.context.as_ref(),
                theme,
            ),
            RunTab::World => world::draw_world(frame, app, body, theme),
            RunTab::Stack => stack::draw_stack(frame, body, app, theme, breakpoint),
            RunTab::Artifacts => artifacts::draw_artifacts(frame, body, app, theme, breakpoint),
        }
    }
    overlays::draw_cancellation(frame, body, app, theme);
    if app.rejection_open() {
        overlays::draw_rejection(frame, body, app, theme);
    }
    draw_footer(
        frame,
        footer,
        theme,
        &overview::run_keys(app),
        status_line(app, theme, legend_line(app, theme)),
    );
}

fn draw_resize_required(frame: &mut Frame, area: Rect, last_size: (u16, u16), theme: Theme) {
    frame.render_widget(Clear, area);
    let lines = vec![
        Line::from(Span::styled("Terminal too small", theme.error())),
        Line::from(""),
        Line::from(format!(
            "The operator console requires at least {MIN_WIDTH}x{MIN_HEIGHT}."
        )),
        Line::from(format!("Current: {}x{}", last_size.0, last_size.1)),
    ];
    let centered = layout::centered(area, 52, 6);
    frame.render_widget(
        Paragraph::new(lines)
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Resize Required "),
            )
            .alignment(Alignment::Center),
        centered,
    );
}
