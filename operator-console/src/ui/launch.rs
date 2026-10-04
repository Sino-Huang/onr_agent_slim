//! Launch screen (§4.1) and the activation review.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};

use super::layout::{Breakpoint, scrolling_list, truncate, wrapped, wrapped_field};
use super::theme::Theme;
use super::{draw_footer, status_line, title_line};
use crate::app::launch::{PERCEPTION_MODES, UPDATE_OWNERSHIP_MODES};
use crate::app::{App, LaunchField, LaunchState, SOURCE_AUTHORITY};

const KEYS: &str = "Tab field · ←/→ change · F2 demo prompts · F3 history · Alt+Enter review · r preflight · F1 help";
const PRESET_KEYS: &str =
    "Tab field · Enter preset picker · ←/→ cycle · F2 demo prompts · F3 history · Alt+Enter review";
const PREFLIGHT_KEYS: &str = "Tab field · ↑↓ check · Enter check detail · r re-run preflight · F3 history · Alt+Enter review";
/// Rows the Mission Intent editor keeps at minimum.
const MIN_INTENT_ROWS: u16 = 5;
/// Editor rows the scrollable Preflight list stops growing at.
const COMFORT_INTENT_ROWS: u16 = 7;
/// Rows the Stack/Preflight row keeps at minimum.
const MIN_TOP_ROWS: u16 = 8;

pub fn draw_launch(
    frame: &mut Frame,
    area: Rect,
    app: &mut App,
    theme: Theme,
    breakpoint: Breakpoint,
) {
    let stack_width = breakpoint.pick(50, 60, 72).min(area.width / 2);
    let preflight_width = area.width.saturating_sub(stack_width);
    let stack = stack_lines(&app.launch, theme, stack_width.saturating_sub(2) as usize);
    let preflight = preflight_panel(
        &app.launch,
        theme,
        preflight_width.saturating_sub(2) as usize,
    );
    let about = about_lines(&app.launch, theme, area.width.saturating_sub(2) as usize);
    // Rows, in priority order: the Stack panel in full, then "What this
    // runs" (keeping the editor's minimum), then the scrollable Preflight
    // list may grow while the editor keeps a comfortable height.
    let body = area.height.saturating_sub(1 + 3);
    let stack_rows = (stack.len() as u16 + 2).max(MIN_TOP_ROWS);
    let about_height = if about.is_empty() {
        0
    } else {
        (about.len() as u16 + 2).min(body.saturating_sub(MIN_INTENT_ROWS + stack_rows))
    };
    let wanted = stack.len().max(preflight.rows.len()) as u16 + 2;
    let room = body.saturating_sub(COMFORT_INTENT_ROWS + about_height);
    let top_height = wanted.min(room.max(stack_rows)).max(MIN_TOP_ROWS);
    let [header, top, about_area, intent, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(top_height),
        Constraint::Length(about_height),
        Constraint::Min(3),
        Constraint::Length(3),
    ])
    .areas(area);
    frame.render_widget(Paragraph::new(title_line(app, theme)), header);
    let [stack_area, preflight_area] =
        Layout::horizontal([Constraint::Length(stack_width), Constraint::Min(0)]).areas(top);
    frame.render_widget(
        Paragraph::new(stack).block(Block::default().borders(Borders::ALL).title(" Stack ")),
        stack_area,
    );
    let block = Block::default()
        .borders(Borders::ALL)
        .title(preflight.title);
    let widget = if preflight.list {
        let rows = preflight.rows;
        scrolling_list(
            block,
            preflight_area,
            theme,
            rows.len(),
            preflight.selected,
            &mut app.launch.preflight_offset,
            |index| rows[index].clone(),
        )
    } else {
        Paragraph::new(preflight.rows).block(block)
    };
    frame.render_widget(widget, preflight_area);
    if about_height > 0 {
        frame.render_widget(
            Paragraph::new(about).block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" What this runs "),
            ),
            about_area,
        );
    }
    draw_intent(frame, intent, app, theme);
    let launch = &app.launch;
    let status = match launch.launch_blocker() {
        Some(blocker) => Line::from(Span::styled(format!(" ✖ {blocker}"), theme.hint())),
        None => Line::from(Span::styled(
            " ✔ Ready: Alt+Enter to review and launch",
            theme.good(),
        )),
    };
    let keys = match launch.focus {
        LaunchField::Preflight => PREFLIGHT_KEYS,
        LaunchField::Preset => PRESET_KEYS,
        _ => KEYS,
    };
    draw_footer(frame, footer, theme, keys, status_line(app, theme, status));
    if launch.demo_picker.is_some() {
        super::overlays::draw_demo_picker(frame, area, launch, theme);
    }
    if launch.preset_picker.is_some() {
        super::overlays::draw_preset_picker(frame, area, launch, theme);
    }
    if launch.preflight_detail {
        super::overlays::draw_preflight_check(frame, area, launch, theme);
    }
}

/// "What this runs": the Host's descriptions for the selected preset and
/// toggles, one labelled wrapped row group each. Empty for an older Host.
fn about_lines(launch: &LaunchState, theme: Theme, width: usize) -> Vec<Line<'static>> {
    launch
        .what_this_runs()
        .into_iter()
        .flat_map(|(label, text)| wrapped_field(theme, label, &text, width))
        .collect()
}

/// `‹ a │ [b] │ c✗ ›`: the current value in brackets, unsupported values
/// dimmed and marked `✗`.
fn options_spans(
    theme: Theme,
    options: &[(&str, bool)],
    current: &str,
    focused: bool,
) -> Vec<Span<'static>> {
    let mut spans = vec![Span::styled("‹ ", theme.dim())];
    for (index, (value, supported)) in options.iter().enumerate() {
        if index > 0 {
            spans.push(Span::styled(" │ ", theme.dim()));
        }
        if *value == current {
            let style = if focused {
                theme.selected()
            } else {
                theme.title()
            };
            spans.push(Span::styled(format!("[{value}]"), style));
        } else if *supported {
            spans.push(Span::raw((*value).to_string()));
        } else {
            spans.push(Span::styled(format!("{value}✗"), theme.dim()));
        }
    }
    spans.push(Span::styled(" ›", theme.dim()));
    spans
}

fn field_label(theme: Theme, label: &str, focused: bool) -> Span<'static> {
    if focused {
        Span::styled(format!("▸{label:<11}"), theme.title())
    } else {
        Span::styled(format!(" {label:<11}"), theme.dim())
    }
}

/// Single-value selector row: `‹ [value] ›`.
fn value_row(theme: Theme, label: &str, value: String, focused: bool) -> Line<'static> {
    Line::from(vec![
        field_label(theme, label, focused),
        Span::styled("‹ ", theme.dim()),
        Span::styled(
            format!("[{value}]"),
            if focused {
                theme.selected()
            } else {
                theme.title()
            },
        ),
        Span::styled(" ›", theme.dim()),
    ])
}

fn stack_lines(launch: &LaunchState, theme: Theme, width: usize) -> Vec<Line<'static>> {
    let Some(preset) = launch.preset() else {
        return match launch.presets_error.as_deref() {
            Some(error) => wrapped(
                &format!("✖ Stack presets unavailable: {error}"),
                width,
                1,
                theme.error(),
            ),
            None => vec![Line::from(Span::styled(
                " Loading Stack presets…",
                theme.dim(),
            ))],
        };
    };
    let focus = launch.focus;
    let airsim_options = launch.airsim_options();
    let airsim: Vec<(&str, bool)> = [false, true]
        .into_iter()
        .map(|value| {
            (
                if value { "on" } else { "off" },
                airsim_options.contains(&value),
            )
        })
        .collect();
    let perception_options = launch.perception_options();
    let perception: Vec<(&str, bool)> = PERCEPTION_MODES
        .iter()
        .map(|mode| (*mode, perception_options.iter().any(|value| value == mode)))
        .collect();
    let updates: Vec<(&str, bool)> = UPDATE_OWNERSHIP_MODES
        .iter()
        .filter(|mode| **mode == launch.update_ownership || focus == LaunchField::Updates)
        .map(|mode| (*mode, true))
        .collect();

    let mut lines = vec![value_row(
        theme,
        "Preset",
        truncate(&preset.title, width.saturating_sub(18)),
        focus == LaunchField::Preset,
    )];
    let mut row = |label: &str, field: LaunchField, options: &[(&str, bool)], current: &str| {
        let mut spans = vec![field_label(theme, label, focus == field)];
        spans.extend(options_spans(theme, options, current, focus == field));
        lines.push(Line::from(spans));
    };
    row(
        "AirSim",
        LaunchField::Airsim,
        &airsim,
        if launch.airsim { "on" } else { "off" },
    );
    row(
        "Perception",
        LaunchField::Perception,
        &perception,
        &launch.perception,
    );
    row(
        "Updates",
        LaunchField::Updates,
        &updates,
        &launch.update_ownership,
    );
    lines.push(value_row(
        theme,
        "Sim limit",
        format!("{} s", launch.simulation_limit_seconds),
        focus == LaunchField::SimLimit,
    ));
    lines.push(Line::from(Span::styled(
        truncate(
            &format!(" {} · {}", preset.preset_id, preset.mission_mode),
            width,
        ),
        theme.dim(),
    )));
    if let Some(reason) = preset.unsupported_reason.as_deref() {
        lines.extend(wrapped(&format!("ⓘ {reason}"), width, 1, theme.hint()));
    }
    lines
}

/// The Preflight panel's title and rows. `list` rows are one per check,
/// drawn as a scrolling list; otherwise they are a status message.
struct PreflightPanel {
    title: Line<'static>,
    rows: Vec<Line<'static>>,
    list: bool,
    /// Highlighted row while the panel has focus.
    selected: Option<usize>,
}

/// One row per check, failures first, then warnings, then passes; a row
/// that does not fit is cut with `…` and Enter shows it in full.
fn preflight_panel(launch: &LaunchState, theme: Theme, width: usize) -> PreflightPanel {
    let focused = launch.focus == LaunchField::Preflight;
    let current = launch.current_preflight();
    let pending = launch.preflight_pending();
    let state = match (current, pending) {
        (_, true) => " · running…",
        (Some(preflight), false) if preflight.allows_launch() => " · launchable",
        (Some(_), false) => " · launch blocked",
        (None, false) => "",
    };
    let name = if focused {
        " ▸Preflight"
    } else {
        " Preflight"
    };
    let mut title = vec![
        Span::styled(
            name,
            if focused {
                theme.title()
            } else {
                Style::default()
            },
        ),
        Span::raw(state),
    ];
    let checks = launch.preflight_checks();
    if !checks.is_empty() {
        title.push(Span::raw(" ·"));
        for status in ["fail", "warn", "pass"] {
            let count = checks.iter().filter(|check| check.status == status).count();
            if count > 0 {
                title.push(Span::raw(" "));
                title.push(theme.check_mark(status));
                title.push(Span::raw(count.to_string()));
            }
        }
    }
    title.push(Span::raw(" "));
    let title = Line::from(title);
    if checks.is_empty() {
        let rows = match launch.preflight_error.as_deref() {
            Some(error) => wrapped(
                &format!("✖ Preflight unavailable: {error}"),
                width,
                1,
                theme.error(),
            ),
            None if launch.presets.is_none() => vec![Line::from(Span::styled(
                " Waiting for Stack presets…",
                theme.dim(),
            ))],
            None => vec![Line::from(Span::styled(
                " Running preflight checks…",
                theme.dim(),
            ))],
        };
        return PreflightPanel {
            title,
            rows,
            list: false,
            selected: None,
        };
    }
    let stale = current.is_none() || pending;
    let selected = launch.selected_check_index().filter(|_| focused);
    let rows = checks
        .iter()
        .enumerate()
        .map(|(index, check)| {
            let mut text = check.label.clone();
            if let Some(detail) = check.detail.as_deref() {
                text.push_str(&format!(" ({detail})"));
            }
            if let Some(hint) = check.hint.as_deref()
                && check.status != "pass"
            {
                text.push_str(&format!(" — {hint}"));
            }
            let style = if selected == Some(index) {
                theme.selected()
            } else if stale {
                theme.dim()
            } else {
                Style::default()
            };
            Line::from(vec![
                Span::raw(" "),
                theme.check_mark(&check.status),
                Span::styled(
                    format!(" {}", truncate(&text, width.saturating_sub(3))),
                    style,
                ),
            ])
        })
        .collect();
    PreflightPanel {
        title,
        rows,
        list: true,
        selected,
    }
}

fn draw_intent(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let focused = app.launch.focus == LaunchField::Intent;
    let title = if focused {
        " ▸Mission Intent "
    } else {
        " Mission Intent "
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .title(Span::styled(title, theme.title()));
    let inner = block.inner(area);
    let content = Rect {
        x: inner.x + 1,
        width: inner.width.saturating_sub(2),
        ..inner
    };
    frame.render_widget(block, area);
    let wrapped = app.launch.editor.wrap(content.width as usize);
    let (row, col) = wrapped.cursor;
    let scroll = row.saturating_sub(content.height.saturating_sub(1) as usize);
    let lines: Vec<Line> = wrapped
        .lines
        .into_iter()
        .skip(scroll)
        .map(Line::from)
        .collect();
    frame.render_widget(Paragraph::new(lines), content);
    if focused && app.launch.demo_picker.is_none() && !app.help_open && !app.history.open {
        frame.set_cursor_position((content.x + col as u16, content.y + (row - scroll) as u16));
    }
}

pub fn draw_review(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(0),
        Constraint::Length(3),
    ])
    .areas(area);
    frame.render_widget(Paragraph::new(title_line(app, theme)), header);
    let launch = &app.launch;
    let width = body.width.saturating_sub(2) as usize;
    let mut lines = vec![
        Line::from(" Confirm activation of this Mission Intent:"),
        Line::from(""),
    ];
    lines.extend(wrapped(
        launch.editor.text(),
        width,
        3,
        ratatui::style::Style::default(),
    ));
    lines.push(Line::from(""));
    if let Some(selection) = launch.selection() {
        let title = launch.preset().map_or("", |preset| preset.title.as_str());
        lines.extend(wrapped_field(
            theme,
            "Stack:",
            &format!(
                "{title} ({}) · AirSim {} · perception {} · {} · limit {} s",
                selection.preset_id,
                if selection.airsim { "on" } else { "off" },
                selection.perception,
                selection.update_ownership,
                selection.simulation_limit_seconds
            ),
            width,
        ));
    }
    if let Some(preflight) = launch.current_preflight() {
        let count = |status: &str| {
            preflight
                .checks
                .iter()
                .filter(|check| check.status == status)
                .count()
        };
        lines.push(Line::from(vec![
            Span::styled(format!(" {:<11}", "Preflight:"), theme.dim()),
            theme.check_mark("pass"),
            Span::raw(format!(" {} pass  ", count("pass"))),
            theme.check_mark("warn"),
            Span::raw(format!(" {} warn  ", count("warn"))),
            theme.check_mark("fail"),
            Span::raw(format!(" {} fail", count("fail"))),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!(
            " activation request: {}",
            app.review_request_id().unwrap_or("-")
        ),
        theme.dim(),
    )));
    lines.push(Line::from(Span::styled(
        format!(" console session:    {}", app.session.session_id),
        theme.dim(),
    )));
    lines.push(Line::from(Span::styled(
        format!(" source authority:   {SOURCE_AUTHORITY}"),
        theme.dim(),
    )));
    frame.render_widget(
        Paragraph::new(lines)
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Review Mission Activation "),
            )
            .wrap(Wrap { trim: false }),
        body,
    );
    draw_footer(
        frame,
        footer,
        theme,
        "Enter: confirm and launch once · Esc: back to the Launch screen",
        status_line(app, theme, Line::from("")),
    );
}
