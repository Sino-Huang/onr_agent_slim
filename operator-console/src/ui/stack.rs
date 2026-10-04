//! Stack tab: Environment Stack prep steps and services with state marks, and
//! the selected row's auto-following log tail. Prep steps sit in plan
//! position (`prepare` before the services, `post_ready` after them) with
//! their measured durations. During teardown (v1.5) a `stopping` service
//! shows its elapsed grace and a stopped one its `graceful`/`forced` mode.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::layout::{Breakpoint, field, human_bytes, seconds_between, short_duration, truncate};
use super::theme::{Badge, Importance, Theme};
use crate::app::run::parse_rfc3339;
use crate::app::{App, StackRow, stack_rows};
use crate::host::{StackService, StackStepRecord};

fn service_badge(name: &str) -> Badge {
    if name.contains("perception") {
        Badge::Per
    } else if name.contains("engine") || name.contains("physical") || name.contains("mission4") {
        Badge::Env
    } else {
        Badge::Stk
    }
}

/// Lines under the list for the selected row: a blank line plus its fields.
fn detail_rows(row: Option<StackRow<'_>>) -> u16 {
    match row {
        None => 0,
        Some(StackRow::Step(_)) => 4,
        Some(StackRow::Service(service)) => {
            5 + u16::from(service.previous_ready_seconds.is_some())
                + u16::from(stop_detail(service).is_some())
        }
    }
}

pub fn draw_stack(frame: &mut Frame, area: Rect, app: &App, theme: Theme, breakpoint: Breakpoint) {
    let view = &app.view.stack;
    let list_rows = view
        .stack
        .as_ref()
        .map_or(1, |stack| stack_rows(stack).count().max(1)) as u16;
    let detail = detail_rows(view.selected_row().map(|(_, row)| row));
    let (services, log) = if breakpoint == Breakpoint::Compact {
        let [services, log] = Layout::vertical([
            Constraint::Length((list_rows + detail + 2).min(area.height / 2)),
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
    let selected = view.selected_row();
    let now = app.unix_now();
    let mut lines: Vec<Line> = stack_rows(stack)
        .enumerate()
        .map(|(index, row)| {
            let (state, text) = match row {
                StackRow::Step(step) => (step.state.as_str(), step_text(step, now)),
                StackRow::Service(service) => (service.state.as_str(), service_text(service, now)),
            };
            let style = if selected.is_some_and(|(selected, _)| selected == index) {
                theme.selected()
            } else {
                Style::default()
            };
            Line::from(vec![
                Span::raw(" "),
                theme.service_mark(state),
                Span::raw(" "),
                theme.badge(service_badge(row.name())),
                Span::styled(format!(" {}", truncate(&text, width)), style),
            ])
        })
        .collect();
    if stack.services.is_empty() && stack.steps.is_empty() {
        lines.push(Line::from(Span::styled(
            " No services in this stack plan.",
            theme.dim(),
        )));
    }
    match selected.map(|(_, row)| row) {
        Some(StackRow::Step(step)) => {
            lines.push(Line::from(""));
            lines.push(field(
                theme,
                "Stage:",
                if step.stage == "prepare" {
                    "prepare (before the services)"
                } else {
                    "post-ready (after the services)"
                },
            ));
            lines.push(field(theme, "Started:", &step.started_at));
            lines.push(field(
                theme,
                "Finished:",
                step.finished_at.as_deref().unwrap_or("-"),
            ));
        }
        Some(StackRow::Service(service)) => {
            lines.extend(service_detail(service, theme, width));
        }
        None => {}
    }
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn service_text(service: &StackService, now: i64) -> String {
    let mut text = format!("{:<18} {:<8}", service.name, service.state);
    if let Some(timing) = service_timing(service, now) {
        text.push_str(&format!(" {timing}"));
    }
    if let Some(port) = service.port {
        text.push_str(&format!(" :{port}"));
    }
    if let Some(pid) = service.pid {
        text.push_str(&format!(" pid {pid}"));
    }
    if !service.required {
        text.push_str(" (optional)");
    }
    text
}

/// `name state M:SS`: elapsed of the budget while running, the measured
/// duration once finished.
fn step_text(step: &StackStepRecord, now: i64) -> String {
    let mut text = format!("{:<18} {:<8}", step.name, step.state);
    let timing = match step.finished_at.as_deref().and_then(parse_rfc3339) {
        Some(finished) => seconds_between(&step.started_at, finished).map(short_duration),
        None if step.state == "running" => seconds_between(&step.started_at, now).map(|elapsed| {
            match step.timeout_seconds.as_f64() {
                Some(budget) => format!(
                    "{} / {}",
                    short_duration(elapsed),
                    short_duration(budget.round() as i64)
                ),
                None => short_duration(elapsed),
            }
        }),
        None => None,
    };
    if let Some(timing) = timing {
        text.push_str(&format!(" {timing}"));
    }
    text
}

fn service_detail(service: &StackService, theme: Theme, width: usize) -> Vec<Line<'static>> {
    let importance = Importance::parse(&service.importance).unwrap_or(Importance::Routine);
    let mut lines = vec![
        Line::from(""),
        field(
            theme,
            "Started:",
            service.started_at.as_deref().unwrap_or("-"),
        ),
    ];
    if service.state == "starting" {
        lines.push(field(
            theme,
            "Waiting:",
            service.waiting_for.as_deref().unwrap_or("readiness"),
        ));
    } else {
        lines.push(field(
            theme,
            "Ready:",
            service.ready_at.as_deref().unwrap_or("-"),
        ));
    }
    if let Some(seconds) = service
        .previous_ready_seconds
        .as_ref()
        .and_then(serde_json::Number::as_f64)
    {
        // History from the previous run of the same preset, not an estimate.
        lines.push(field(
            theme,
            "Last run:",
            &format!(
                "ready in {} · history, same preset",
                short_duration(seconds.round() as i64)
            ),
        ));
    }
    if let Some(stop) = stop_detail(service) {
        lines.push(field(theme, "Stop:", &stop));
    }
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
    lines
}

/// `Stop:` detail once teardown signalled the service (or the Host reaped
/// it): mode, the grace period before SIGKILL, then the time (last, as the
/// narrow panel may clip it).
fn stop_detail(service: &StackService) -> Option<String> {
    let grace = service
        .stop_grace_seconds
        .as_ref()
        .and_then(serde_json::Number::as_f64)
        .map(|grace| format!(" · grace {}", short_duration(grace.round() as i64)));
    let grace = grace.as_deref().unwrap_or("");
    match (service.stop_mode.as_deref(), service.stopped_at.as_deref()) {
        (Some(mode), stopped) => Some(format!("{mode}{grace} · at {}", stopped.unwrap_or("-"))),
        (None, _) => service
            .stop_requested_at
            .as_deref()
            .map(|requested| format!("SIGTERM sent{grace} · at {requested}")),
    }
}

/// `in M:SS` (start to ready) for a ready service; `M:SS / M:SS` (elapsed
/// of the readiness budget) for a starting one; `M:SS / grace M:SS` for a
/// stopping one; the stop mode for a stopped one.
fn service_timing(service: &StackService, now: i64) -> Option<String> {
    let budget = |elapsed: i64, budget: Option<&serde_json::Number>, label: &str| {
        let elapsed = short_duration(elapsed);
        match budget.and_then(serde_json::Number::as_f64) {
            Some(budget) => format!(
                "{elapsed} / {label}{}",
                short_duration(budget.round() as i64)
            ),
            None => elapsed,
        }
    };
    match service.state.as_str() {
        "stopping" => {
            let elapsed = seconds_between(service.stop_requested_at.as_deref()?, now)?;
            Some(budget(
                elapsed,
                service.stop_grace_seconds.as_ref(),
                "grace ",
            ))
        }
        "stopped" => service.stop_mode.clone(),
        "starting" => {
            let elapsed = seconds_between(service.started_at.as_deref()?, now)?;
            Some(budget(elapsed, service.ready_timeout_seconds.as_ref(), ""))
        }
        "ready" => {
            let started = service.started_at.as_deref()?;
            let ready = parse_rfc3339(service.ready_at.as_deref()?)?;
            Some(format!(
                "in {}",
                short_duration(seconds_between(started, ready)?)
            ))
        }
        _ => None,
    }
}

fn draw_log(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let view = &app.view.stack;
    let name = view
        .selected_row()
        .map_or("no service", |(_, row)| row.name());
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
