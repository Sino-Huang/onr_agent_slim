//! Modal overlays: `?` help, cancellation confirmation/progress, and the F2
//! demo prompt picker.

use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};

use super::layout::{centered, truncate, wrapped_field};
use super::theme::{Badge, Importance, Theme};
use crate::app::run::run_elapsed_seconds;
use crate::app::{App, CancellationState, LaunchState};

fn modal(frame: &mut Frame, area: Rect, title: Line<'static>, lines: Vec<Line<'static>>) {
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(lines)
            .block(Block::default().borders(Borders::ALL).title(title))
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn key_row(theme: Theme, keys: &str, action: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("  {keys:<21}"), theme.title()),
        Span::raw(action.to_string()),
    ])
}

pub fn draw_help(frame: &mut Frame, area: Rect, theme: Theme) {
    let mut lines = vec![Line::from(Span::styled(" Global", theme.hint()))];
    lines.extend([
        key_row(theme, "1-7 · Tab/Shift+Tab", "switch Run tabs"),
        key_row(theme, "? · F1", "toggle this help"),
        key_row(theme, "c", "request cancellation of the owned run"),
        key_row(
            theme,
            "q · Ctrl+C",
            "managed exit (cancels an owned active run first)",
        ),
        key_row(
            theme,
            "Ctrl+Q",
            "detach: quit, run keeps going, session recoverable",
        ),
        key_row(
            theme,
            "e",
            "after a terminal run: new Mission Intent, same stack",
        ),
        Line::from(""),
        Line::from(Span::styled(" Launch screen", theme.hint())),
        key_row(theme, "Tab · Shift+Tab", "next / previous field"),
        key_row(theme, "←/→", "change the focused toggle"),
        key_row(
            theme,
            "F2",
            "demo prompts (preset mission, rejection battery)",
        ),
        key_row(theme, "r", "re-run preflight"),
        key_row(
            theme,
            "Alt+Enter",
            "review and launch (disabled while a check fails)",
        ),
        Line::from(""),
        Line::from(Span::styled(" Tabs", theme.hint())),
        key_row(
            theme,
            "Agents",
            "↑↓ select · f follow newest · PgUp/PgDn detail",
        ),
        key_row(
            theme,
            "Progress",
            "↑↓ move · ←→ fold · f follow · i importance",
        ),
        key_row(theme, "/ · n/N", "search Progress · next/previous match"),
        key_row(
            theme,
            "Enter · Esc",
            "accept/close search (typing captures tab/q/c)",
        ),
        key_row(
            theme,
            "World",
            "s cycle world/annotated front/front/3rd · p pause/resume",
        ),
        key_row(
            theme,
            "Stack",
            "↑↓ service · f follow log · PgUp/PgDn scroll",
        ),
        key_row(
            theme,
            "Artifacts",
            "↑↓ select · Enter inspect · ←/→ page · Esc close",
        ),
        key_row(
            theme,
            "Belief/Context",
            "↑↓ · j/k select belief entity (detail below table)",
        ),
    ]);
    let mut legend = vec![Span::raw(" ")];
    for importance in Importance::ALL {
        legend.push(theme.importance_mark(importance));
        legend.push(Span::raw(format!(" {}  ", importance.name())));
    }
    lines.push(Line::from(legend));
    let mut badges = vec![Span::raw(" ")];
    for badge in [
        Badge::Hyp,
        Badge::Man,
        Badge::Cc,
        Badge::Fsm,
        Badge::Bel,
        Badge::Env,
        Badge::Per,
        Badge::Stk,
    ] {
        badges.push(theme.badge(badge));
        badges.push(Span::raw(" "));
    }
    badges.push(theme.ai_badge());
    badges.push(Span::styled(" = LLM text, non-authoritative", theme.ai()));
    lines.push(Line::from(badges));
    let height = (lines.len() as u16 + 2).min(area.height);
    modal(
        frame,
        centered(area, 84, height),
        Line::from(Span::styled(" Help · Esc or ? to close ", theme.title())),
        lines,
    );
}

pub fn draw_cancellation(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let run_id = app
        .run
        .as_ref()
        .map_or("current run", |run| run.mission_run_id.as_str())
        .to_string();
    match &app.cancellation {
        CancellationState::Idle => {}
        CancellationState::Confirming => modal(
            frame,
            centered(area, 72, 10),
            Line::from(Span::styled(" Cancel Mission Run ", theme.error())),
            vec![
                Line::from(""),
                Line::from(format!(" Request cancellation of Mission Run {run_id}?")),
                Line::from(""),
                Line::from(Span::styled(
                    " The Runtime Host records cancellation-requested, then stops the",
                    theme.dim(),
                )),
                Line::from(Span::styled(
                    " Run Worker and its Environment Stack before reporting cancelled.",
                    theme.dim(),
                )),
                Line::from(""),
                Line::from(Span::styled(
                    " Enter: confirm cancellation · Esc: keep running",
                    theme.hint(),
                )),
            ],
        ),
        CancellationState::Requested {
            cancellation_request_id,
        } => modal(
            frame,
            centered(area, 72, 9),
            Line::from(Span::styled(" Cancellation Requested ", theme.hint())),
            vec![
                Line::from(""),
                Line::from(format!(" Cancellation requested for Mission Run {run_id}.")),
                Line::from(Span::styled(
                    format!(" Request: {cancellation_request_id}"),
                    theme.dim(),
                )),
                Line::from(Span::styled(
                    " Run status stays current until the Host reports terminal cancelled.",
                    theme.dim(),
                )),
                Line::from(""),
                Line::from(Span::styled(
                    " Polling the current Mission Run for Host confirmation.",
                    theme.hint(),
                )),
            ],
        ),
    }
}

pub fn draw_demo_picker(frame: &mut Frame, area: Rect, launch: &LaunchState, theme: Theme) {
    let selected = launch.demo_picker.unwrap_or(0);
    let prompts = launch.demo_prompts();
    let width = 76u16.min(area.width);
    let text_width = width.saturating_sub(8) as usize;
    let mut lines = vec![Line::from("")];
    for (index, (label, text)) in prompts.iter().enumerate() {
        let style = if index == selected {
            theme.selected()
        } else {
            ratatui::style::Style::default()
        };
        let marker = if index == selected { "▸" } else { " " };
        lines.push(Line::from(Span::styled(
            format!(" {marker} {label}"),
            style.patch(theme.title()),
        )));
        lines.push(Line::from(Span::styled(
            format!("     \"{}\"", truncate(text, text_width)),
            theme.dim(),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        " ↑↓ choose · Enter use as Mission Intent · Esc close",
        theme.hint(),
    )));
    let height = (lines.len() as u16 + 2).min(area.height);
    modal(
        frame,
        centered(area, width, height),
        Line::from(Span::styled(" Demo prompts (F2) ", theme.title())),
        lines,
    );
}

pub fn draw_rejection(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let Some(run) = app.run.as_ref() else {
        return;
    };
    let Some(detail) = run.terminal_detail.as_ref() else {
        return;
    };
    let width = 92u16.min(area.width);
    let inner = usize::from(width.saturating_sub(2));
    let mut lines = vec![
        Line::from(Span::styled(
            " Hyper Agent · stage 1 (intent parsing)",
            theme.error(),
        )),
        Line::from(""),
    ];
    lines.extend(wrapped_field(
        theme,
        "Intent:",
        &format!("\"{}\"", app.launch.editor.text()),
        inner,
    ));
    lines.extend(wrapped_field(
        theme,
        "Reason:",
        detail.reason.as_deref().unwrap_or("No reason recorded"),
        inner,
    ));
    let planner_files = app
        .view
        .artifacts
        .iter()
        .filter(|artifact| artifact.source.as_deref() == Some("planner"))
        .count();
    let statecharts = app
        .view
        .artifacts
        .iter()
        .filter(|artifact| artifact.kind == "accepted-statechart.json")
        .count();
    let mut maneuvers = std::collections::HashSet::new();
    if let Some(environment) = app.view.environment.as_ref() {
        for entry in &environment.timeline {
            if let Some(id) = entry
                .payload
                .get("maneuver_id")
                .and_then(serde_json::Value::as_str)
            {
                maneuvers.insert(id);
            }
        }
    }
    if let Some(context) = app.view.context.as_ref()
        && let Some(maneuver) = context.active_maneuver.as_ref()
        && let Some(id) = maneuver.maneuver_id.as_deref()
    {
        maneuvers.insert(id);
    }
    let work = if app
        .view
        .cursor(crate::host::OperatorSection::Artifacts)
        .is_none()
        || app
            .view
            .cursor(crate::host::OperatorSection::Environment)
            .is_none()
        || app.view.context.is_none()
    {
        "Loading recorded work evidence…".to_string()
    } else {
        format!(
            "{planner_files} planner files · {statecharts} statecharts · {} maneuvers (recorded)",
            maneuvers.len()
        )
    };
    lines.extend(wrapped_field(theme, "Work:", &work, inner));
    let elapsed = run_elapsed_seconds(run, app.unix_now()).map_or_else(
        || "not recorded".to_string(),
        |seconds| format!("{seconds} s"),
    );
    lines.extend(wrapped_field(theme, "Elapsed:", &elapsed, inner));
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        if run.is_terminal() {
            " e edit a new Mission Intent · Enter view run details"
        } else {
            " Finalizing run · Enter view details · edit available after cleanup"
        },
        theme.hint(),
    )));
    let height = (lines.len() as u16 + 2).min(area.height);
    modal(
        frame,
        centered(area, width, height),
        Line::from(Span::styled(" ✖ MISSION REJECTED ", theme.error())),
        lines,
    );
}
