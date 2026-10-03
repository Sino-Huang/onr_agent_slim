//! Run screen chrome (header strip, tab bar, phase stepper, keys) and the
//! Overview tab.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::layout::{Breakpoint, clock_duration, short_field, short_id, truncate, wrapped};
use super::theme::{Badge, Theme};
use crate::app::run::run_elapsed_seconds;
use crate::app::{App, CancellationState, Liveness, RunTab};

/// One-line run header: id, status, elapsed, mission time, FSM, maneuver,
/// stack services, Host liveness. Optional segments are dropped, least
/// important first, until the line fits `width`.
pub fn header_strip(app: &App, theme: Theme, width: u16) -> Line<'static> {
    let Some(run) = app.run.as_ref() else {
        return Line::from(vec![
            Span::styled(" RUN ", theme.title()),
            Span::styled("waiting for the current Mission Run…", theme.dim()),
        ]);
    };
    let mut head = vec![
        Span::styled(" RUN ", theme.title()),
        Span::raw(short_id(&run.mission_run_id, 12)),
        Span::raw(" "),
        theme.status_dot(&run.status),
        Span::styled(format!(" {}", run.status), theme.run_status(&run.status)),
    ];
    if let Some(elapsed) = run_elapsed_seconds(run, app.unix_now()) {
        head.push(Span::raw(format!(" {}", clock_duration(elapsed))));
    }
    // (priority, spans): lower priority is dropped first.
    let mut segments: Vec<(u8, Vec<Span<'static>>)> = Vec::new();
    if let Some(overview) = app.view.overview.as_ref() {
        if let Some(time) = overview.environment.mission_time_seconds.as_ref() {
            segments.push((3, vec![Span::raw(format!("t={time} s"))]));
        }
        if let Some(state) = overview.fsm.state.as_deref() {
            segments.push((4, vec![Span::raw(format!("FSM {state}"))]));
        }
        if let Some(maneuver) = maneuver_label(&overview.active_maneuver) {
            segments.push((1, vec![Span::raw(format!("▶ {maneuver}"))]));
        }
    }
    if let Some(stack) = app.view.stack.stack.as_ref() {
        let mut spans = vec![Span::raw("stack ")];
        spans.extend(
            stack
                .services
                .iter()
                .map(|service| theme.service_mark(&service.state)),
        );
        segments.push((2, spans));
    }
    let mut host = vec![Span::raw("host ")];
    host.extend(liveness_spans(app.liveness(), theme));
    segments.push((5, host));

    let span_width = |spans: &[Span]| spans.iter().map(Span::width).sum::<usize>();
    let separator_width = 3;
    let total = |segments: &[(u8, Vec<Span>)]| {
        span_width(&head)
            + segments
                .iter()
                .map(|(_, spans)| separator_width + span_width(spans))
                .sum::<usize>()
    };
    while total(&segments) > usize::from(width) {
        let Some(lowest) = segments
            .iter()
            .enumerate()
            .min_by_key(|(_, (priority, _))| *priority)
            .map(|(index, _)| index)
        else {
            break;
        };
        segments.remove(lowest);
    }
    let mut spans = head;
    for (_, segment) in segments {
        spans.push(Span::styled(" │ ", theme.dim()));
        spans.extend(segment);
    }
    Line::from(spans)
}

fn liveness_spans(liveness: Liveness, theme: Theme) -> [Span<'static>; 2] {
    match liveness {
        Liveness::Live => [Span::styled("●", theme.good()), Span::raw(" live")],
        Liveness::Stale => [Span::styled("▲", theme.hint()), Span::raw(" stale")],
        Liveness::Offline => [Span::styled("✖", theme.error()), Span::raw(" offline")],
        Liveness::Idle => [Span::styled("○", theme.dim()), Span::raw(" idle")],
    }
}

/// `action lifecycle [progress%]` from the overview's active maneuver.
fn maneuver_label(value: &serde_json::Value) -> Option<String> {
    let object = value.as_object()?;
    let name = object
        .get("action")
        .or_else(|| object.get("maneuver_id"))?
        .as_str()?;
    let mut label = name.to_string();
    if let Some(state) = object
        .get("lifecycle")
        .or_else(|| object.get("status"))
        .and_then(|value| value.as_str())
    {
        label.push(' ');
        label.push_str(state);
    }
    if let Some(progress) = object.get("progress").and_then(serde_json::Value::as_f64) {
        label.push_str(&format!(" {:.0}%", progress * 100.0));
    }
    Some(label)
}

pub fn tab_bar(app: &App, theme: Theme) -> Line<'static> {
    let mut spans = vec![Span::raw(" ")];
    for (index, tab) in RunTab::ALL.into_iter().enumerate() {
        let label = format!(" {} {} ", index + 1, tab.label());
        if app.view.tab == tab {
            spans.push(Span::styled(label, theme.active_tab()));
        } else {
            spans.push(Span::styled(label, theme.dim()));
        }
    }
    Line::from(spans)
}

/// Phase stepper from `overview.phase`.
pub fn phase_stepper(app: &App, theme: Theme) -> Line<'static> {
    let rejected = app
        .run
        .as_ref()
        .and_then(|run| run.terminal_detail.as_ref())
        .is_some_and(|detail| detail.kind == "mission_rejected");
    let Some(phase) = app
        .view
        .overview
        .as_ref()
        .and_then(|overview| overview.phase.as_ref())
    else {
        if rejected {
            return Line::from(Span::styled(" ✖ Intent rejected", theme.error()));
        }
        return Line::from(Span::styled(
            " Phase: waiting for overview.phase from the Host",
            theme.dim(),
        ));
    };
    let mut spans = vec![Span::raw(" ")];
    for (index, step) in phase.steps.iter().enumerate() {
        if index > 0 {
            spans.push(Span::styled(" ─ ", theme.dim()));
        }
        let status = if rejected && step.id == "intent" {
            "failed"
        } else {
            &step.status
        };
        spans.push(theme.step_mark(status));
        let label = match step.detail.as_deref() {
            Some(detail) if matches!(step.status.as_str(), "active" | "failed") => {
                format!(" {} ({detail})", step.label)
            }
            _ => format!(" {}", step.label),
        };
        let style = if status == "failed" {
            theme.error()
        } else if step.id == phase.current {
            theme.title()
        } else if step.status == "pending" {
            theme.dim()
        } else {
            ratatui::style::Style::default()
        };
        spans.push(Span::styled(label, style));
    }
    Line::from(spans)
}

/// Key line for the Run screen.
pub fn run_keys(app: &App) -> String {
    if app.view.inspector.is_some() {
        return "←/p →/n: page preview · Esc: close".to_string();
    }
    match app.cancellation {
        CancellationState::Confirming => {
            return "Enter: confirm cancellation · Esc: keep running · Ctrl+Q: detach".to_string();
        }
        CancellationState::Requested { .. } => {
            return "cancellation requested · polling the current Mission Run · Ctrl+Q: detach"
                .to_string();
        }
        CancellationState::Idle => {}
    }
    let terminal = app.run.as_ref().is_some_and(|run| run.is_terminal());
    let tab_keys = match app.view.tab {
        RunTab::Agents => "↑↓ select · f follow · PgUp/PgDn detail · ",
        RunTab::World => "s source · p pause · ",
        RunTab::Stack => "↑↓ service · f follow · PgUp/PgDn scroll · ",
        RunTab::Artifacts => "↑↓ select · Enter inspect · ",
        RunTab::Progress => {
            "↑↓ move · ←→ fold · f follow · i importance · / search · n/N matches · "
        }
        RunTab::BeliefContext => "↑↓ entity · ",
        RunTab::Overview => "",
    };
    let run_keys = if terminal {
        "e new intent · q exit"
    } else {
        "c cancel · q managed exit"
    };
    format!("1-7/Tab tabs · {tab_keys}? help · {run_keys}")
}

pub fn draw_overview(
    frame: &mut Frame,
    area: Rect,
    app: &mut App,
    theme: Theme,
    breakpoint: Breakpoint,
) {
    if breakpoint == Breakpoint::Compact {
        let [top, bottom] =
            Layout::vertical([Constraint::Length(12), Constraint::Min(0)]).areas(area);
        let [run, progress] =
            Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).areas(top);
        draw_run_panel(frame, run, app, theme);
        super::progress::draw_progress_preview(frame, progress, &app.view.progress, theme);
        let [activity, hitl] =
            Layout::horizontal([Constraint::Percentage(64), Constraint::Percentage(36)])
                .areas(bottom);
        draw_significant_activity(frame, activity, app, theme);
        draw_human_decisions(frame, hitl, app, theme);
        return;
    }
    let [left, right] =
        Layout::horizontal([Constraint::Percentage(52), Constraint::Percentage(48)]).areas(area);
    let [run, progress, hitl] = Layout::vertical([
        Constraint::Length(10),
        Constraint::Min(8),
        Constraint::Length(6),
    ])
    .areas(left);
    draw_run_panel(frame, run, app, theme);
    super::progress::draw_progress_preview(frame, progress, &app.view.progress, theme);
    draw_human_decisions(frame, hitl, app, theme);
    let [world, belief, context] = Layout::vertical([
        Constraint::Min(10),
        Constraint::Length(7),
        Constraint::Length(7),
    ])
    .areas(right);
    super::world::draw_preview(frame, app, world, theme);
    super::belief_context::draw_belief_mini(frame, belief, app.view.beliefs.as_ref(), theme);
    super::belief_context::draw_context_mini(frame, context, app.view.context.as_ref(), theme);
}

fn draw_run_panel(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Mission Run ");
    let width = block.inner(area).width as usize;
    let lines = match app.run.as_ref() {
        Some(run) => {
            let mut status = vec![
                Span::styled(format!(" {:<11}", "Status:"), theme.dim()),
                Span::styled(run.status.clone(), theme.run_status(&run.status)),
            ];
            if let Some(classification) = run.terminal_classification.as_deref() {
                status.push(Span::styled(
                    format!(" · {classification}"),
                    theme.run_status(&run.status),
                ));
            }
            if matches!(app.cancellation, CancellationState::Requested { .. }) {
                status.push(Span::styled(" · cancellation requested", theme.hint()));
            }
            let mut lines = vec![Line::from(status)];
            match app.liveness() {
                Liveness::Live | Liveness::Idle => {}
                Liveness::Stale => lines.push(Line::from(Span::styled(
                    " ▲ stale - showing last received evidence",
                    theme.hint(),
                ))),
                Liveness::Offline => lines.push(Line::from(Span::styled(
                    " ✖ offline - showing last received evidence",
                    theme.error(),
                ))),
            }
            lines.push(short_field(theme, "Mission:", &run.mission_id, width));
            lines.push(short_field(theme, "Run:", &run.mission_run_id, width));
            if let Some(stack) = run.stack.as_ref() {
                lines.push(short_field(
                    theme,
                    "Stack:",
                    &format!(
                        "{} · AirSim {} · perception {}",
                        stack.preset_id,
                        if stack.airsim { "on" } else { "off" },
                        stack.perception
                    ),
                    width,
                ));
            }
            lines.push(short_field(
                theme,
                "Started:",
                run.started_at
                    .as_deref()
                    .or(run.created_at.as_deref())
                    .unwrap_or("-"),
                width,
            ));
            lines.push(short_field(
                theme,
                "Finished:",
                run.finished_at.as_deref().unwrap_or("-"),
                width,
            ));
            if app.recovered_owner() {
                lines.push(Line::from(Span::styled(
                    " Recovered owner session",
                    theme.hint(),
                )));
            }
            if let Some(detail) = run.terminal_detail.as_ref() {
                lines.extend(wrapped(
                    &format!("✖ {}", detail.summary()),
                    width,
                    1,
                    theme.error(),
                ));
            }
            lines
        }
        None => vec![Line::from(Span::styled(
            " No current Mission Run.",
            theme.dim(),
        ))],
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_significant_activity(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Significant Activity ");
    let width = block.inner(area).width.saturating_sub(6) as usize;
    let lines = match app.view.overview.as_ref() {
        None => vec![Line::from(Span::styled(
            " No activity received.",
            theme.dim(),
        ))],
        Some(overview) if overview.recent_events.is_empty() => vec![Line::from(Span::styled(
            " No significant activity recorded.",
            theme.dim(),
        ))],
        Some(overview) => overview
            .recent_events
            .iter()
            .rev()
            .map(|entry| {
                let component = entry.component.as_deref().unwrap_or("unknown");
                let badge = Badge::for_source(component, Some(entry.event_kind.as_str()));
                Line::from(vec![
                    Span::raw(" "),
                    theme.badge(badge),
                    Span::raw(truncate(
                        &format!(
                            " #{} {} · {} · {}",
                            entry.observation_sequence,
                            entry.event_kind,
                            component,
                            entry.outcome.as_deref().unwrap_or("recorded")
                        ),
                        width,
                    )),
                ])
            })
            .collect(),
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

/// The permanent HITL surface (issue #32): a status-only Human Decisions
/// placeholder driven solely by the Host's public, versioned Mission Run
/// Status. It binds no keys, offers no controls, and renders identically for
/// owner and observer consoles; decision submission is a later delivery.
fn draw_human_decisions(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    const AWAITING_HUMAN_DECISION: &str = "awaiting_human_decision";
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Human Decisions ");
    let awaiting = app
        .run
        .as_ref()
        .is_some_and(|run| run.status == AWAITING_HUMAN_DECISION);
    let width = block.inner(area).width as usize;
    let mut lines = Vec::new();
    if awaiting {
        lines.extend(wrapped(
            "AWAITING HUMAN DECISION",
            width,
            1,
            theme.run_status(AWAITING_HUMAN_DECISION),
        ));
        for text in [
            "Status: awaiting_human_decision",
            "The Mission Run is paused, awaiting a Human Decision.",
        ] {
            lines.extend(wrapped(text, width, 1, theme.dim()));
        }
    } else {
        lines.extend(wrapped(
            "No Human Decision Requests require action.",
            width,
            1,
            theme.dim(),
        ));
    }
    lines.extend(wrapped(
        "Status-only view: no decision controls.",
        width,
        1,
        theme.dim(),
    ));
    frame.render_widget(Paragraph::new(lines).block(block), area);
}
