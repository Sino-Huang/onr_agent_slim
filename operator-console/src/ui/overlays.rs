//! Modal overlays: `?` help, cancellation confirmation, the F2
//! demo prompt picker, the Enter-on-Preset picker, the Preflight check
//! detail, the mission rejection card, the infrastructure failure card and
//! the F3 run history.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};

use super::layout::{
    centered, scrolling_list, short_duration, short_field, short_id, truncate, wrapped,
    wrapped_field,
};
use super::theme::{Badge, Importance, Theme};
use crate::app::run::run_elapsed_seconds;
use crate::app::{App, CancellationState, LaunchState};
use crate::host::MissionRunSummary;

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
        key_row(
            theme,
            "w",
            "inspect the wait (service/prep log, agent call, teardown)",
        ),
        key_row(
            theme,
            "c · q · Ctrl+C",
            "cancel the owned run · managed exit (cancels first)",
        ),
        key_row(
            theme,
            "F3 · F4",
            "F3 history (Esc back) · F4 presentation (v card, p freeze)",
        ),
        key_row(
            theme,
            "Ctrl+Q",
            "detach: quit, run keeps going, session recoverable",
        ),
        key_row(
            theme,
            "e · x · l · y",
            "e new intent · x export receipt · failure: l log, y copy id",
        ),
        Line::from(""),
        Line::from(Span::styled(" Launch screen", theme.hint())),
        key_row(theme, "Tab · Shift+Tab", "next / previous field"),
        key_row(
            theme,
            "←/→ · Enter",
            "change the toggle · Enter on Preset: named picker",
        ),
        key_row(
            theme,
            "F2",
            "demo prompts (preset mission, rejection battery)",
        ),
        key_row(
            theme,
            "r · ↑↓ Enter",
            "re-run preflight · on Preflight: select, open a check",
        ),
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
            "↑↓ Home/End select · f follow newest · PgUp/PgDn detail",
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
            "↑↓ service or prep step · f follow log · PgUp/PgDn scroll",
        ),
        key_row(
            theme,
            "Artifacts",
            "↑↓ Home/End select · Enter open the inspector",
        ),
        key_row(
            theme,
            "Inspector",
            "↑↓ PgUp/PgDn Home/End scroll · ←/→ byte page · Esc close",
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
        Line::from(Span::styled(
            " Help · Esc, ? or F1 to close ",
            theme.title(),
        )),
        lines,
    );
}

/// The confirmation dialog. Once confirmed (the request in flight, then
/// accepted) the waiting banner shows teardown and the tabs stay readable.
pub fn draw_cancellation(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    if app.cancellation != CancellationState::Confirming || app.cancellation_in_progress() {
        return;
    }
    let run_id = app
        .run
        .as_ref()
        .map_or("current run", |run| run.mission_run_id.as_str())
        .to_string();
    modal(
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
    );
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

/// Enter on the Preset field: every preset by name with the Host's
/// description of the highlighted one, what it offers and the defaults it
/// applies. An older Host sends no description; only its ids are shown.
pub fn draw_preset_picker(frame: &mut Frame, area: Rect, launch: &LaunchState, theme: Theme) {
    let Some(presets) = launch.presets.as_ref() else {
        return;
    };
    let selected = launch.preset_picker.unwrap_or(0);
    let width = 84u16.min(area.width);
    let text_width = width.saturating_sub(2) as usize;
    let id_width = presets
        .presets
        .iter()
        .map(|preset| preset.preset_id.chars().count())
        .max()
        .unwrap_or(0);
    let title_width = text_width.saturating_sub(id_width + 14);
    let mut lines = vec![Line::from("")];
    for (index, preset) in presets.presets.iter().enumerate() {
        let (marker, style) = if index == selected {
            ("▸", theme.selected())
        } else {
            (" ", ratatui::style::Style::default())
        };
        let mut spans = vec![
            Span::styled(
                format!(
                    " {marker} {:<title_width$} ",
                    truncate(&preset.title, title_width)
                ),
                style,
            ),
            Span::styled(preset.preset_id.clone(), theme.dim()),
        ];
        if index == launch.preset_index {
            spans.push(Span::styled(" · current", theme.dim()));
        }
        lines.push(Line::from(spans));
    }
    if let Some(preset) = presets.presets.get(selected) {
        lines.push(Line::from(""));
        if let Some(description) = preset.description.as_deref() {
            lines.extend(wrapped_field(theme, "Runs", description, text_width));
        }
        lines.push(short_field(theme, "Mode", &preset.mission_mode, text_width));
        let airsim: Vec<&str> = preset
            .supports
            .airsim
            .iter()
            .map(|value| if *value { "on" } else { "off" })
            .collect();
        lines.push(short_field(
            theme,
            "Offers",
            &format!(
                "AirSim {} · perception {}",
                airsim.join("|"),
                preset.supports.perception.join("|")
            ),
            text_width,
        ));
        let defaults = &preset.defaults;
        lines.push(short_field(
            theme,
            "Defaults",
            &format!(
                "AirSim {} · perception {} · {} · {} s",
                if defaults.airsim { "on" } else { "off" },
                defaults.perception,
                defaults.update_ownership,
                defaults.simulation_limit_seconds
            ),
            text_width,
        ));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        " ↑↓ choose · Enter select (applies its defaults) · Esc cancel",
        theme.hint(),
    )));
    let height = (lines.len() as u16 + 2).min(area.height);
    modal(
        frame,
        centered(area, width, height),
        Line::from(Span::styled(" Stack preset ", theme.title())),
        lines,
    );
}

/// Full detail of the selected Preflight check: label, status, detail, hint,
/// and the Host's copyable diagnostic command, which the console never runs.
pub fn draw_preflight_check(frame: &mut Frame, area: Rect, launch: &LaunchState, theme: Theme) {
    let checks = launch.preflight_checks();
    let (Some(index), Some(check)) = (launch.selected_check_index(), launch.selected_check())
    else {
        return;
    };
    let width = 84u16.min(area.width);
    let text_width = width.saturating_sub(2) as usize;
    let status = match check.status.as_str() {
        "fail" => "fail · blocks launch",
        "warn" => "warn · does not block launch",
        "pass" => "pass",
        other => other,
    };
    let mut lines = vec![
        Line::from(""),
        Line::from(vec![
            Span::raw(" "),
            theme.check_mark(&check.status),
            Span::styled(format!(" {}", check.label), theme.title()),
        ]),
    ];
    lines.extend(wrapped_field(theme, "Status", status, text_width));
    lines.extend(wrapped_field(theme, "Check", &check.check_id, text_width));
    for (label, value) in [("Detail", &check.detail), ("Hint", &check.hint)] {
        lines.extend(wrapped_field(
            theme,
            label,
            value.as_deref().unwrap_or("-"),
            text_width,
        ));
    }
    if let Some(command) = check.remediation.as_deref() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            " Diagnose (read-only; copy and run it yourself, the console never does):",
            theme.dim(),
        )));
        lines.extend(wrapped(
            command,
            text_width.saturating_sub(1),
            3,
            theme.title(),
        ));
    }
    if launch.current_preflight().is_none() || launch.preflight_pending() {
        lines.push(Line::from(""));
        lines.extend(wrapped(
            "ⓘ Preflight is re-running; this answer may be out of date.",
            text_width.saturating_sub(1),
            1,
            theme.hint(),
        ));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        " ↑↓ previous/next check · r re-run preflight · Esc close",
        theme.hint(),
    )));
    let height = (lines.len() as u16 + 2).min(area.height);
    modal(
        frame,
        centered(area, width, height),
        Line::from(Span::styled(
            format!(" Preflight check {}/{} ", index + 1, checks.len()),
            theme.title(),
        )),
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

/// The infrastructure failure card: a double red border and `✖ RUN FAILED`
/// set it apart from the mission rejection card.
pub fn draw_failure(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let Some(card) = app.failure_card() else {
        return;
    };
    let width = 92u16.min(area.width);
    let inner = usize::from(width.saturating_sub(2));
    let mut lines = vec![
        Line::from(Span::styled(format!(" {}", card.headline), theme.error())),
        Line::from(""),
    ];
    lines.extend(wrapped_field(theme, "Stage:", &card.stage, inner));
    lines.extend(wrapped_field(theme, "Reason:", &card.reason, inner));
    let service = match &card.failed_service {
        Some((name, "stack status")) => format!("{name} (from the stack status)"),
        Some((name, _)) => name.clone(),
        None => "none recorded".to_string(),
    };
    lines.extend(wrapped_field(theme, "Service:", &service, inner));
    lines.extend(wrapped_field(
        theme,
        "Last step:",
        &format!("{} (last completed phase step)", card.last_done_step),
        inner,
    ));
    lines.extend(wrapped_field(
        theme,
        "Cleanup:",
        &card.cleanup.describe(),
        inner,
    ));
    lines.extend(wrapped_field(theme, "Run:", &card.mission_run_id, inner));
    lines.extend(wrapped_field(
        theme,
        "Run root:",
        card.run_root
            .as_deref()
            .unwrap_or("not reported by this Host (API < 1.5)"),
        inner,
    ));
    if let Some(last_line) = card.last_line.as_deref() {
        lines.extend(wrapped_field(theme, "Log line:", last_line, inner));
        lines.push(Line::from(Span::styled(
            format!(
                " {:11}last line of {}: context only, not the cause",
                "", card.log.artifact_id
            ),
            theme.dim(),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!(
            " l log tail ({}) · 2 Progress · y copy run id + run root",
            card.log.artifact_id
        ),
        theme.hint(),
    )));
    lines.push(Line::from(Span::styled(
        if app.viewing_history() {
            " Enter dismiss · Esc back (historical run: read-only)"
        } else if card.cleanup.allows_new_intent() {
            " Enter dismiss · e new Mission Intent · q exit"
        } else {
            " Enter dismiss · e new Mission Intent after cleanup · q exit"
        },
        theme.hint(),
    )));
    let height = (lines.len() as u16 + 2).min(area.height);
    let rect = centered(area, width, height);
    frame.render_widget(Clear, rect);
    frame.render_widget(
        Paragraph::new(lines)
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_type(BorderType::Double)
                    .border_style(theme.error())
                    .title(Line::from(Span::styled(
                        format!(" ✖ RUN FAILED · {} ", card.classification),
                        theme.error(),
                    ))),
            )
            .wrap(Wrap { trim: false }),
        rect,
    );
}

/// Column widths of one history row; preset and status share what the fixed
/// columns leave.
struct HistoryColumns {
    preset: usize,
    status: usize,
}

impl HistoryColumns {
    /// Marker, start time, wall duration, run id and tag columns.
    const FIXED: usize = 2 + 17 + 8 + 14 + 10;

    fn new(width: usize) -> Self {
        let rest = width.saturating_sub(Self::FIXED);
        let preset = rest / 2;
        Self {
            preset,
            status: rest - preset,
        }
    }
}

/// `2026-10-03 13:42` from the Host's ISO-8601 UTC `created_at`.
fn history_started(row: &MissionRunSummary) -> String {
    row.mission_run
        .created_at
        .as_deref()
        .map_or_else(
            || "-".to_string(),
            |at| at.chars().take(16).collect::<String>(),
        )
        .replace('T', " ")
}

/// Preset plus the toggles that differ from a bare simulated run.
fn history_preset(row: &MissionRunSummary) -> String {
    match row.mission_run.stack.as_ref() {
        Some(stack) => {
            let mut text = stack.preset_id.clone();
            if stack.airsim {
                text.push_str(" +AirSim");
            }
            if stack.perception != "off" {
                text.push_str(&format!(" +{}", stack.perception));
            }
            text
        }
        None => "stack not recorded".to_string(),
    }
}

fn history_status(row: &MissionRunSummary) -> String {
    let run = &row.mission_run;
    let mark = match run.status.as_str() {
        "succeeded" => "✔",
        "failed" => "✖",
        "cancelled" => "■",
        _ => "◐",
    };
    match run.terminal_classification.as_deref() {
        Some(classification) => format!("{mark} {} · {classification}", run.status),
        None => format!("{mark} {}", run.status),
    }
}

fn history_wall(row: &MissionRunSummary) -> String {
    match row
        .wall_seconds
        .as_ref()
        .and_then(serde_json::Number::as_f64)
    {
        Some(seconds) => short_duration(seconds.round() as i64),
        None if row.mission_run.is_terminal() => "-".to_string(),
        None => "live".to_string(),
    }
}

fn history_line(
    row: &MissionRunSummary,
    columns: &HistoryColumns,
    owned: Option<&str>,
    selected: bool,
    theme: Theme,
) -> Line<'static> {
    let run = &row.mission_run;
    let (tag, tag_style) = if !row.run_root_available {
        ("✖ no root", theme.error())
    } else if row.current {
        ("● current", theme.good())
    } else if owned == Some(run.mission_run_id.as_str()) {
        ("◆ owned", theme.hint())
    } else {
        ("", theme.dim())
    };
    let base = if selected {
        theme.selected()
    } else if row.run_root_available {
        ratatui::style::Style::default()
    } else {
        theme.dim()
    };
    let (preset, status) = (columns.preset, columns.status);
    Line::from(vec![
        Span::styled(if selected { "▸ " } else { "  " }, base),
        Span::styled(format!("{:<17}", history_started(row)), base),
        Span::styled(
            format!("{:<preset$}", truncate(&history_preset(row), preset)),
            base,
        ),
        Span::styled(
            format!("{:<status$}", truncate(&history_status(row), status)),
            base.patch(theme.run_status(&run.status)),
        ),
        Span::styled(format!("{:>6}  ", history_wall(row)), base),
        Span::styled(format!("{:<14}", short_id(&run.mission_run_id, 12)), base),
        Span::styled(format!("{tag:<10}"), base.patch(tag_style)),
    ])
}

/// Everything the history row has, for the selected run.
fn history_detail(row: &MissionRunSummary) -> String {
    let run = &row.mission_run;
    let mut parts = vec![run.mission_run_id.clone()];
    match run.stack.as_ref() {
        Some(stack) => parts.push(format!(
            "{} · AirSim {} · perception {}",
            stack.preset_id,
            if stack.airsim { "on" } else { "off" },
            stack.perception
        )),
        None => parts.push("stack not recorded (before API v1.2)".to_string()),
    }
    if let Some(toggles) = row.toggles.as_ref() {
        if let Some(ownership) = toggles.update_ownership.as_deref() {
            parts.push(ownership.to_string());
        }
        if let Some(limit) = toggles.simulation_limit_seconds.as_ref() {
            parts.push(format!("limit {limit} s"));
        }
    }
    parts.push(if row.run_root_available {
        "Run Root on disk".to_string()
    } else {
        "Run Root missing".to_string()
    });
    parts.join(" · ")
}

/// F3: the Host's run history, newest first; filters cycle over the loaded
/// rows and scrolling past the last one loads the next older page.
pub fn draw_history(frame: &mut Frame, area: Rect, app: &mut App, theme: Theme) {
    let owned = app.owned_run_id().map(str::to_string);
    let rect = centered(
        area,
        area.width.saturating_sub(4),
        area.height.saturating_sub(2),
    );
    frame.render_widget(Clear, rect);
    let block = Block::default()
        .borders(Borders::ALL)
        .title(Line::from(Span::styled(
            " Run history (F3) · newest first ",
            theme.title(),
        )));
    let inner = block.inner(rect);
    frame.render_widget(block, rect);
    let [filters, header, list, detail, message, keys] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas(inner);
    let width = usize::from(inner.width);
    let columns = HistoryColumns::new(width);
    let history = &mut app.history;
    let indices: Vec<usize> = (0..history.rows.len())
        .filter(|index| history.matches(&history.rows[*index]))
        .collect();
    let selected = history.selected_index();
    let paging = if history.loading() {
        "loading…"
    } else if history.complete {
        "all loaded"
    } else {
        "▼ older runs load on scroll"
    };
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            truncate(
                &format!(
                    " Status {} (s) · Preset {} (p) · {} of {} loaded run{} shown · {paging}",
                    history.status_filter.label(),
                    history.preset_filter.as_deref().unwrap_or("all"),
                    indices.len(),
                    history.rows.len(),
                    if history.rows.len() == 1 { "" } else { "s" },
                ),
                width,
            ),
            theme.hint(),
        ))),
        filters,
    );
    let (preset, status) = (columns.preset, columns.status);
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(
                "  {:<17}{:<preset$}{:<status$}{:>6}  {:<14}",
                "Started (UTC)", "Preset", "Status", "Wall", "Run"
            ),
            theme.dim(),
        ))),
        header,
    );
    let list_block = Block::default().borders(Borders::TOP | Borders::BOTTOM);
    if indices.is_empty() {
        let text = if history.error.is_some() {
            ""
        } else if history.loading() {
            " Loading run history from the Runtime Host…"
        } else if history.rows.is_empty() {
            " No Mission Runs are recorded on this Runtime Host."
        } else {
            " No loaded run matches the filters."
        };
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(text, theme.dim()))).block(list_block),
            list,
        );
    } else {
        let rows = &history.rows;
        let widget = scrolling_list(
            list_block,
            list,
            theme,
            indices.len(),
            selected,
            &mut history.offset,
            |index| {
                history_line(
                    &rows[indices[index]],
                    &columns,
                    owned.as_deref(),
                    Some(index) == selected,
                    theme,
                )
            },
        );
        frame.render_widget(widget, list);
    }
    if let Some(row) = selected.and_then(|index| history.rows.get(indices[index])) {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!(
                    " {}",
                    truncate(&history_detail(row), width.saturating_sub(1))
                ),
                theme.dim(),
            ))),
            detail,
        );
    }
    if let Some(text) = history.error.as_deref().or(history.message.as_deref()) {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!(" ✖ {}", truncate(text, width.saturating_sub(3))),
                theme.error(),
            ))),
            message,
        );
    }
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            truncate(
                " ↑↓ PgUp/PgDn select · Enter open read-only · s status · p preset · r reload · Esc close",
                width,
            ),
            theme.hint(),
        ))),
        keys,
    );
}
