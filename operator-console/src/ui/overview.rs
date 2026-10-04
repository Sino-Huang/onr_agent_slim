//! Run screen chrome (header strip, tab bar, phase stepper, keys) and the
//! Overview tab.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::layout::{
    Breakpoint, LABEL_WIDTH, clock_duration, field, seconds_between, short_duration, short_field,
    short_id, truncate, wrap_line, wrapped, wrapped_field,
};
use super::theme::{Badge, Theme};
use crate::app::run::{parse_rfc3339, run_elapsed_seconds};
use crate::app::{App, CancellationState, Liveness, ReceiptExportState, RunTab};
use crate::host::{OperatorStack, ReceiptExport, RunPhase, RunReceipt, RunRecord, StackTeardown};

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
    let mut head = Vec::new();
    if let Some(historical) = app.historical() {
        head.push(Span::styled(" HISTORICAL ", theme.historical()));
        if historical.unavailable.is_some() {
            head.push(Span::styled(" Run Root missing", theme.error()));
        } else if let Some((elapsed, _)) = app.historical_loading() {
            head.push(Span::styled(
                format!(" loading from disk {} s", elapsed.as_secs()),
                theme.hint(),
            ));
        }
    }
    head.extend([
        Span::styled(" RUN ", theme.title()),
        Span::raw(short_id(&run.mission_run_id, 12)),
        Span::raw(" "),
        theme.status_dot(&run.status),
        Span::styled(format!(" {}", run.status), theme.run_status(&run.status)),
    ]);
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
            // A terminal run's maneuver is history, not a live action.
            let label = if run.is_terminal() {
                format!("last {maneuver}")
            } else {
                format!("▶ {maneuver}")
            };
            segments.push((1, vec![Span::raw(label)]));
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

/// Footer line while a historical run loads from disk: how long, and how
/// many reads timed out and are retried meanwhile.
pub(crate) fn historical_loading_line(app: &App, theme: Theme) -> Option<Line<'static>> {
    let (elapsed, timed_out) = app.historical_loading()?;
    let mut text = format!(
        " Loading from disk · {} s · first open rebuilds the Host's view",
        elapsed.as_secs()
    );
    if timed_out > 0 {
        text.push_str(&format!(
            " · {timed_out} read{} timed out, retrying",
            if timed_out == 1 { "" } else { "s" }
        ));
    }
    Some(Line::from(Span::styled(text, theme.hint())))
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
    phase_line(
        app.view
            .overview
            .as_ref()
            .and_then(|overview| overview.phase.as_ref()),
        mission_rejected(app),
        theme,
    )
}

/// Whether the displayed run ended with its Mission Intent rejected.
pub(crate) fn mission_rejected(app: &App) -> bool {
    app.run
        .as_ref()
        .and_then(|run| run.terminal_detail.as_ref())
        .is_some_and(|detail| detail.kind == "mission_rejected")
}

/// The phase stepper for `phase` (the live overview's or a frozen copy).
pub(crate) fn phase_line(phase: Option<&RunPhase>, rejected: bool, theme: Theme) -> Line<'static> {
    let Some(phase) = phase else {
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
    // Once confirmed, the waiting banner shows teardown; the keys stay live.
    if app.cancellation == CancellationState::Confirming && !app.cancellation_in_progress() {
        return "Enter: confirm cancellation · Esc: keep running · Ctrl+Q: detach".to_string();
    }
    let terminal = app.run.as_ref().is_some_and(|run| run.is_terminal());
    if app.presenting() {
        return presentation_keys(app, terminal);
    }
    if app.view.inspector.is_some() {
        return "↑↓ PgUp/PgDn scroll · Home/End top/bottom · ←/p →/n byte page · Esc close"
            .to_string();
    }
    let tab_keys = match app.view.tab {
        RunTab::Agents => "↑↓ Home/End select · f follow · PgUp/Dn detail · ",
        RunTab::World => "s source · p pause · F4 present · ",
        RunTab::Stack => "↑↓ select · f follow · PgUp/PgDn scroll · ",
        RunTab::Artifacts => "↑↓ Home/End select · Enter inspect · ",
        RunTab::Progress => {
            "↑↓ move · ←→ fold · f follow · i importance · / search · n/N matches · "
        }
        RunTab::BeliefContext => "↑↓ entity · ",
        RunTab::Overview => "",
    };
    if app.viewing_history() {
        let back = app.history_return_label();
        let failure = if app.failure_open() {
            "Enter dismiss · l log tail · y copy run id/root · "
        } else if app.failure_classified() {
            "l log tail · y copy run id/root · "
        } else {
            ""
        };
        return format!(
            "HISTORICAL read-only · Esc back to {back} · 1-7/Tab tabs · {tab_keys}{failure}F3 history"
        );
    }
    let run_keys = if app.failure_open() {
        "Enter dismiss · l log tail · y copy run id/root · e new intent · q exit"
    } else if app.failure_classified() {
        "l log tail · y copy run id/root · x receipt · e new intent · q exit · F3 history"
    } else if terminal {
        "x export receipt · e new intent · q exit · F3 history"
    } else if app.cancellation_in_progress() {
        "w teardown · Ctrl+Q: detach"
    } else {
        "c cancel · q managed exit · F3 history"
    };
    format!("1-7/Tab tabs · {tab_keys}? help · {run_keys}")
}

/// Key line of the F4 presentation layout.
fn presentation_keys(app: &App, terminal: bool) -> String {
    let freeze = if app.view.presentation.is_frozen() {
        "p resume"
    } else {
        "p freeze · s source"
    };
    let run_keys = if app.viewing_history() {
        format!(
            "HISTORICAL read-only · Esc back to {}",
            app.history_return_label()
        )
    } else if app.failure_open() {
        "Enter dismiss · l log tail · q exit".to_string()
    } else if terminal {
        "e new intent · q exit".to_string()
    } else if app.cancellation_in_progress() {
        "Ctrl+Q: detach".to_string()
    } else {
        "c cancel · q managed exit".to_string()
    };
    format!("F4 back to tabs · v next card · {freeze} · ? help · {run_keys}")
}

pub fn draw_overview(
    frame: &mut Frame,
    area: Rect,
    app: &mut App,
    theme: Theme,
    breakpoint: Breakpoint,
) {
    if breakpoint == Breakpoint::Compact {
        let [column, _] =
            Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
                .areas(area);
        let (lines, receipt) = run_panel_lines(app, theme, column.width.saturating_sub(2) as usize);
        let [top, bottom] = Layout::vertical([
            Constraint::Length(panel_height(&lines, 12, receipt)),
            Constraint::Min(0),
        ])
        .areas(area);
        let [run, progress] =
            Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).areas(top);
        draw_run_panel(frame, run, lines, receipt);
        super::progress::draw_progress_preview(
            frame,
            progress,
            &app.view.progress,
            theme,
            app.unix_now(),
        );
        let [activity, hitl] =
            Layout::horizontal([Constraint::Percentage(64), Constraint::Percentage(36)])
                .areas(bottom);
        draw_significant_activity(frame, activity, app, theme);
        draw_human_decisions(frame, hitl, app, theme);
        return;
    }
    let [left, right] =
        Layout::horizontal([Constraint::Percentage(52), Constraint::Percentage(48)]).areas(area);
    let (lines, receipt) = run_panel_lines(app, theme, left.width.saturating_sub(2) as usize);
    let [run, progress, hitl] = Layout::vertical([
        Constraint::Length(panel_height(&lines, 10, receipt)),
        Constraint::Min(8),
        Constraint::Length(6),
    ])
    .areas(left);
    draw_run_panel(frame, run, lines, receipt);
    super::progress::draw_progress_preview(
        frame,
        progress,
        &app.view.progress,
        theme,
        app.unix_now(),
    );
    draw_human_decisions(frame, hitl, app, theme);
    let [world, belief, context] = Layout::vertical([
        Constraint::Min(10),
        Constraint::Length(7),
        Constraint::Length(7),
    ])
    .areas(right);
    super::world::draw_preview(frame, app, world, theme);
    super::belief_context::draw_belief_mini(frame, belief, app.view.beliefs.as_ref(), theme);
    super::belief_context::draw_context_mini(
        frame,
        context,
        app.view.context.as_ref(),
        app.run.as_ref().is_some_and(RunRecord::is_terminal),
        theme,
    );
}

/// The Mission Run panel's height: `minimum`, grown to fit a receipt (other
/// content keeps the fixed height).
fn panel_height(lines: &[Line], minimum: u16, receipt: bool) -> u16 {
    if receipt {
        (lines.len() as u16 + 2).max(minimum)
    } else {
        minimum
    }
}

fn draw_run_panel(frame: &mut Frame, area: Rect, lines: Vec<Line<'static>>, receipt: bool) {
    let block = Block::default().borders(Borders::ALL).title(if receipt {
        " Mission Run · receipt "
    } else {
        " Mission Run "
    });
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

/// The panel's rows, and whether they include a terminal receipt (the v1.5
/// `overview.receipt`, the stack's teardown receipt, or both).
fn run_panel_lines(app: &App, theme: Theme, width: usize) -> (Vec<Line<'static>>, bool) {
    let Some(run) = app.run.as_ref() else {
        return (
            vec![Line::from(Span::styled(
                " No current Mission Run.",
                theme.dim(),
            ))],
            false,
        );
    };
    let receipt = app
        .view
        .overview
        .as_ref()
        .and_then(|overview| overview.receipt.as_ref())
        .filter(|_| run.is_terminal());
    // With a receipt the status row is labelled for what it is: the Run
    // Worker's lifecycle, never the mission verdict (that is the audit row).
    let label = if receipt.is_some() {
        "Lifecycle:"
    } else {
        "Status:"
    };
    let mut status = vec![
        Span::styled(format!(" {label:<11}"), theme.dim()),
        Span::styled(run.status.clone(), theme.run_status(&run.status)),
    ];
    if let Some(classification) = run.terminal_classification.as_deref() {
        status.push(Span::styled(
            format!(" · {classification}"),
            theme.run_status(&run.status),
        ));
    }
    if app.cancellation_in_progress() {
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
    let mut finished = run.finished_at.as_deref().unwrap_or("-").to_string();
    if let Some(seconds) = receipt
        .and_then(|receipt| receipt.wall_seconds.as_ref())
        .and_then(serde_json::Number::as_f64)
    {
        finished.push_str(&format!(
            " · {} wall",
            short_duration(seconds.round() as i64)
        ));
    }
    lines.push(short_field(theme, "Finished:", &finished, width));
    if let Some(historical) = app.historical() {
        lines.push(Line::from(Span::styled(
            " Historical run · read-only · Esc returns",
            theme.hint(),
        )));
        if let Some(message) = historical.unavailable.as_deref() {
            lines.extend(wrapped(
                &format!("✖ Run Root unavailable: {message}"),
                width,
                1,
                theme.error(),
            ));
        }
    } else if app.recovered_owner() {
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
    if let Some(receipt) = receipt {
        lines.extend(final_rows(receipt, theme, width));
    }
    let teardown = app
        .view
        .stack
        .stack
        .as_ref()
        .filter(|_| run.is_terminal())
        .and_then(|stack| Some((stack, stack.teardown.as_ref()?)));
    if let Some((stack, teardown)) = teardown {
        lines.extend(teardown_receipt(stack, teardown, theme, width));
    }
    if let Some(receipt) = receipt {
        if app.viewing_history() {
            // Export is a mutation: history shows only what was exported.
            let text = match receipt.export.exported_at.as_deref() {
                Some(at) => format!("exported {at} · read-only in history"),
                None => "not exported · read-only in history".to_string(),
            };
            lines.extend(wrapped_field(theme, "Export:", &text, width));
        } else {
            lines.extend(export_rows(
                &receipt.export,
                &app.view.receipt_export,
                theme,
                width,
            ));
        }
    }
    (lines, receipt.is_some() || teardown.is_some())
}

/// The receipt's final FSM state, plan revision and Mission time, then the
/// audit verdict exactly as the audit artifact records it.
fn final_rows(receipt: &RunReceipt, theme: Theme, width: usize) -> Vec<Line<'static>> {
    let last = &receipt.last;
    let mut parts = vec![match last.fsm_state.as_deref() {
        Some(state) => format!("FSM {state}"),
        None => "no FSM state recorded".to_string(),
    }];
    if let Some(revision) = last.plan_revision {
        parts.push(format!("plan r{revision}"));
    }
    if let Some(time) = last.mission_time_seconds.as_ref() {
        parts.push(format!("t={time} s"));
    }
    let mut lines = wrapped_field(theme, "Final:", &parts.join(" · "), width);
    let audit = &receipt.audit;
    let (text, style) = match audit.status.as_str() {
        "pass" => (
            format!("PASS · {}", audit.path.as_deref().unwrap_or("audit")),
            theme.good(),
        ),
        "fail" => (
            format!(
                "FAIL · {} failure{}: {}",
                audit.failures.len(),
                if audit.failures.len() == 1 { "" } else { "s" },
                audit.failures.join(", ")
            ),
            theme.error(),
        ),
        "not_recorded" => ("not recorded (no audit artifact)".to_string(), theme.hint()),
        "unreadable" => (
            format!(
                "unreadable ({} is not a PASS/FAIL audit)",
                audit.path.as_deref().unwrap_or("audit")
            ),
            theme.error(),
        ),
        other => (other.to_string(), theme.hint()),
    };
    lines.push(Line::from(vec![
        Span::styled(format!(" {:<11}", "Audit:"), theme.dim()),
        Span::styled(truncate(&text, width.saturating_sub(LABEL_WIDTH)), style),
    ]));
    lines
}

/// Where `x` writes the receipt, or what the Host answered.
fn export_rows(
    export: &ReceiptExport,
    state: &ReceiptExportState,
    theme: Theme,
    width: usize,
) -> Vec<Line<'static>> {
    let file = export
        .path
        .rsplit('/')
        .next()
        .unwrap_or(export.path.as_str());
    let text = match state {
        ReceiptExportState::Idle => match export.exported_at.as_deref() {
            Some(at) => format!("exported {at} · x rewrites {file}"),
            None => format!("x writes {file}"),
        },
        ReceiptExportState::Exporting => "writing through the Host…".to_string(),
        ReceiptExportState::Exported(exported) => {
            // The Host's path, broken only after `/`, in the value column.
            let status = if exported.replaced {
                format!("✔ overwrote {file} (earlier export)")
            } else {
                format!("✔ wrote {file}")
            };
            let mut lines = vec![field(theme, "Export:", &status)];
            lines.extend(
                path_rows(&exported.path, width.saturating_sub(LABEL_WIDTH))
                    .iter()
                    .map(|row| field(theme, "", row)),
            );
            return lines;
        }
        ReceiptExportState::Failed(message) => {
            return wrap_line(message, width.saturating_sub(LABEL_WIDTH))
                .into_iter()
                .enumerate()
                .map(|(index, row)| {
                    Line::from(vec![
                        Span::styled(
                            format!(" {:<11}", if index == 0 { "Export:" } else { "" }),
                            theme.dim(),
                        ),
                        Span::styled(row, theme.error()),
                    ])
                })
                .collect();
        }
    };
    wrapped_field(theme, "Export:", &text, width)
}

/// `path` in rows of at most `width` characters, broken after a `/` where
/// possible (a component longer than a row is split).
fn path_rows(path: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut rows = vec![String::new()];
    for component in path.split_inclusive('/') {
        let mut rest = component;
        while !rest.is_empty() {
            let row = rows.last_mut().expect("rows is never empty");
            let room = width - row.chars().count();
            if rest.chars().count() <= room {
                row.push_str(rest);
                break;
            }
            if !row.is_empty() {
                rows.push(String::new());
                continue;
            }
            let cut = rest
                .char_indices()
                .nth(width)
                .map_or(rest.len(), |(at, _)| at);
            row.push_str(&rest[..cut]);
            rest = &rest[cut..];
            rows.push(String::new());
        }
    }
    rows.retain(|row| !row.is_empty());
    rows
}

/// The v1.5 teardown receipt: the Run Worker, the services by stop mode, and
/// the Harbor configuration restoration exactly as the Host reported it.
fn teardown_receipt(
    stack: &OperatorStack,
    teardown: &StackTeardown,
    theme: Theme,
    width: usize,
) -> Vec<Line<'static>> {
    let mut worker = format!("worker {}", teardown.worker);
    if let (Some(started), Some(finished)) = (
        teardown.started_at.as_deref(),
        teardown.finished_at.as_deref().and_then(parse_rfc3339),
    ) && let Some(seconds) = seconds_between(started, finished)
    {
        worker.push_str(&format!(" · took {}", short_duration(seconds)));
    }
    let count = |mode: &str| {
        stack
            .services
            .iter()
            .filter(|service| service.stop_mode.as_deref() == Some(mode))
            .count()
    };
    let (graceful, forced) = (count("graceful"), count("forced"));
    let services = match (graceful, forced) {
        (0, 0) => "none were running".to_string(),
        (graceful, 0) => format!("{graceful} stopped · all graceful"),
        (0, forced) => format!("{forced} stopped · all forced"),
        (graceful, forced) => {
            format!(
                "{} stopped · {graceful} graceful, {forced} forced",
                graceful + forced
            )
        }
    };
    let harbor = &teardown.harbor_config;
    let harbor = match (harbor.state.as_str(), harbor.reported_by.as_deref()) {
        ("confirmed", Some(source)) => format!("config restored ({source} reported)"),
        ("confirmed", None) => "config restored".to_string(),
        ("not_applicable", _) => "not applicable (no AirSim engine)".to_string(),
        ("unknown", _) => "restoration unknown (no report)".to_string(),
        (other, _) => other.to_string(),
    };
    let mut lines = wrapped_field(theme, "Teardown:", &worker, width);
    lines.extend(wrapped_field(theme, "Services:", &services, width));
    lines.extend(wrapped_field(theme, "Harbor:", &harbor, width));
    lines
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
