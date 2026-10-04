//! The real progress tree and its selected evidence. Tree structure, styled
//! rows and full JSON details are cached by ProgressView, never cloned per draw.
//! The Run Narrative header (age and coverage) is rebuilt per draw so its age
//! ticks with wall time between polls.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use tui_tree_widget::Tree;

use super::layout::{seconds_between, truncate, wrap_line};
use super::theme::Theme;
use crate::app::progress::ProgressView;
use crate::host::ProgressNarrative;

/// Narrative rows the Overview preview shows before pointing to Progress.
pub const PREVIEW_NARRATIVE_ROWS: usize = 4;
const WAITING: &str = "Waiting for the Run Narrative from the Host";

/// Freshness and coverage of the AI Run Narrative in one line, e.g.
/// `AI narrative · 42 s old · through record #763 · 18 newer`.
///
/// The age is wall time since `generated_at`. Coverage is the narrative's
/// `source_watermark` (an operational-record sequence); the newer count needs
/// the Host's `latest_operational_sequence` (API v1.5) and is left out for
/// older Hosts rather than guessed. Before a narrative exists the line says
/// `coverage pending`; the final attempt says `final`, and a failed attempt
/// says `unavailable`.
pub fn narrative_header(narrative: Option<&ProgressNarrative>, now_unix: i64) -> String {
    let mut parts = vec!["AI narrative".to_string()];
    let Some(narrative) = narrative.filter(|narrative| narrative.status != "none") else {
        parts.push("coverage pending".to_string());
        return parts.join(" · ");
    };
    let terminal = narrative.terminal == Some(true);
    if terminal {
        parts.push("final".to_string());
    }
    let age = narrative
        .generated_at
        .as_deref()
        .and_then(|generated| seconds_between(generated, now_unix))
        .map(age_text);
    match narrative.status.as_str() {
        "available" => {
            if let Some(age) = age {
                parts.push(format!("{age} old"));
            }
            parts.push(match narrative.source_watermark {
                0 => "no records covered".to_string(),
                watermark => format!("through record #{watermark}"),
            });
            match narrative.newer_records() {
                // A final narrative that covers every record needs no count.
                Some(0) if terminal => {}
                Some(newer) => parts.push(format!("{newer} newer")),
                None => {}
            }
        }
        "unavailable" => {
            parts.push("unavailable".to_string());
            if let Some(age) = age {
                parts.push(format!("attempted {age} ago"));
            }
        }
        other => parts.push(other.to_string()),
    }
    parts.join(" · ")
}

/// Seconds below two minutes, minutes below two hours, then hours.
fn age_text(seconds: i64) -> String {
    if seconds < 120 {
        format!("{seconds} s")
    } else if seconds < 7200 {
        format!("{} min", seconds / 60)
    } else {
        format!("{} h", seconds / 3600)
    }
}

/// `header` split at its ` · ` separators into rows of at most `width`
/// columns; continuation rows start with `· `.
fn header_rows(header: &str, width: usize) -> Vec<String> {
    let mut rows: Vec<String> = Vec::new();
    for part in header.split(" · ") {
        match rows.last_mut() {
            Some(row) if row.chars().count() + 3 + part.chars().count() <= width => {
                row.push_str(" · ");
                row.push_str(part);
            }
            Some(_) => rows.push(format!("· {part}")),
            None => rows.push(part.to_string()),
        }
    }
    rows
}

pub fn draw_progress(
    frame: &mut Frame,
    area: Rect,
    view: &mut ProgressView,
    theme: Theme,
    now_unix: i64,
) {
    view.ensure_theme(theme);
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(4),
        Constraint::Length(2),
    ])
    .areas(area);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(" ◆ ", theme.title()),
            Span::styled(
                narrative_header(view.narrative.as_ref(), now_unix),
                theme.ai(),
            ),
        ])),
        header,
    );
    let panes = if area.width >= 140 {
        Layout::horizontal([Constraint::Percentage(58), Constraint::Percentage(42)]).split(body)
    } else {
        Layout::vertical([Constraint::Percentage(58), Constraint::Percentage(42)]).split(body)
    };
    let tree = Tree::new(&view.items)
        .expect("progress IDs are unique")
        .block(Block::default().borders(Borders::ALL).title(format!(
            " Progress · ≥ {} · {} ",
            view.minimum.name(),
            if view.follow_newest {
                "FOLLOW"
            } else {
                "paused"
            },
        )))
        .highlight_style(theme.selected())
        .highlight_symbol("› ")
        .node_closed_symbol("▸ ")
        .node_open_symbol("▾ ");
    frame.render_stateful_widget(tree, panes[0], &mut view.tree_state);
    let detail_style = if view.detail_ai {
        theme.ai()
    } else {
        Default::default()
    };
    frame.render_widget(
        Paragraph::new(view.detail.as_str())
            .style(detail_style)
            .wrap(Wrap { trim: false })
            .scroll((view.detail_scroll, 0))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Selected evidence · PgUp/PgDn "),
            ),
        panes[1],
    );
    let search_status = if view.search.is_empty() {
        " / search · n/N next/previous match".to_string()
    } else {
        format!(
            " /{}{} · {}/{} matches · n/N next/previous",
            view.search,
            if view.search_editing { "▏" } else { "" },
            view.match_position.map_or(0, |index| index + 1),
            view.match_count()
        )
    };
    let lines = [
        Line::from(Span::styled(
            search_status,
            if view.search_editing {
                theme.hint()
            } else {
                theme.dim()
            },
        )),
        Line::from(Span::styled(
            if view.search_editing {
                " Enter/Esc finish search · Backspace erase · Ctrl+U clear · Ctrl+C managed exit"
            } else {
                " ↑↓ select · ←→ expand/collapse · Enter toggle · f follow newest · i minimum importance"
            },
            theme.hint(),
        )),
    ];
    frame.render_widget(Paragraph::new(Vec::from(lines)), footer);
}

/// A compact read-only hierarchy preview for Overview. It shows the narrative
/// header, at most [`PREVIEW_NARRATIVE_ROWS`] narrative rows (the full text is
/// on Progress) and the newest summaries, without taking selection from the
/// Progress tab or cloning its TreeState.
pub fn draw_progress_preview(
    frame: &mut Frame,
    area: Rect,
    view: &ProgressView,
    theme: Theme,
    now_unix: i64,
) {
    let block = Block::default().borders(Borders::ALL).title(" Progress ");
    let width = block.inner(area).width as usize;
    let mut lines = vec![Line::from(vec![
        Span::styled("◆ Run Narrative · ", theme.title()),
        theme.ai_badge(),
        Span::styled(" · non-authoritative", theme.dim()),
    ])];
    lines.extend(
        header_rows(&narrative_header(view.narrative.as_ref(), now_unix), width)
            .into_iter()
            .map(|row| Line::from(Span::styled(row, theme.dim()))),
    );
    // An unavailable narrative has no text, and the header already says so.
    let narrative = match view.narrative.as_ref() {
        Some(narrative) if narrative.status == "unavailable" => narrative.text.as_deref(),
        Some(narrative) => narrative.text.as_deref().or(Some(WAITING)),
        None => Some(WAITING),
    };
    let mut rows = narrative.map_or_else(Vec::new, |text| wrap_line(text, width));
    let capped = rows.len() > PREVIEW_NARRATIVE_ROWS;
    if capped {
        rows.truncate(PREVIEW_NARRATIVE_ROWS);
        let last = rows.pop().unwrap_or_default();
        rows.push(truncate(&format!("{last}…"), width));
    }
    lines.extend(
        rows.into_iter()
            .map(|row| Line::from(Span::styled(row, theme.ai()))),
    );
    if capped {
        lines.push(Line::from(Span::styled(
            "2 Progress: full narrative",
            theme.hint(),
        )));
    }
    for (importance, label) in &view.preview_summaries {
        lines.push(Line::from(vec![
            theme.importance_mark(*importance),
            Span::raw(" "),
            theme.ai_badge(),
            Span::styled(
                label.as_str(),
                theme.importance(*importance).patch(theme.ai()),
            ),
        ]));
    }
    if view.narrative.is_none() && view.nodes.is_empty() {
        lines.push(Line::from(Span::styled(
            "Progress evidence has not arrived yet.",
            theme.dim(),
        )));
    }
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .block(block),
        area,
    );
}
