//! The real progress tree and its selected evidence. Tree structure, styled
//! rows and full JSON details are cached by ProgressView, never cloned per draw.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use tui_tree_widget::Tree;

use super::theme::Theme;
use crate::app::progress::ProgressView;

pub fn draw_progress(frame: &mut Frame, area: Rect, view: &mut ProgressView, theme: Theme) {
    view.ensure_theme(theme);
    let regions = Layout::vertical([Constraint::Min(4), Constraint::Length(2)]).split(area);
    let panes = if area.width >= 140 {
        Layout::horizontal([Constraint::Percentage(58), Constraint::Percentage(42)])
            .split(regions[0])
    } else {
        Layout::vertical([Constraint::Percentage(58), Constraint::Percentage(42)]).split(regions[0])
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
    frame.render_widget(Paragraph::new(Vec::from(lines)), regions[1]);
}

/// A compact read-only hierarchy preview for Overview. It shows the current
/// narrative plus the newest summaries without taking selection from the full
/// Progress tab or cloning its TreeState.
pub fn draw_progress_preview(frame: &mut Frame, area: Rect, view: &ProgressView, theme: Theme) {
    let mut lines = vec![Line::from(vec![
        Span::styled("◆ Run Narrative · ", theme.title()),
        theme.ai_badge(),
        Span::styled(" · non-authoritative", theme.dim()),
    ])];
    let narrative = view
        .narrative
        .as_ref()
        .and_then(|narrative| narrative.text.as_deref())
        .unwrap_or("Waiting for the Run Narrative from the Host");
    lines.push(Line::from(Span::styled(narrative, theme.ai())));
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
            .block(Block::default().borders(Borders::ALL).title(" Progress ")),
        area,
    );
}
