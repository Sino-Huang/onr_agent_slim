//! Artifacts tab: descriptor list, preview (including conversation entries),
//! and the paged content inspector.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};

use super::layout::{Breakpoint, field, human_bytes, truncate, wrapped, wrapped_field};
use super::theme::Theme;
use crate::app::App;

pub fn draw_artifacts(
    frame: &mut Frame,
    area: Rect,
    app: &App,
    theme: Theme,
    breakpoint: Breakpoint,
) {
    let [list, preview] = Layout::horizontal([
        Constraint::Percentage(breakpoint.pick(46, 42, 38)),
        Constraint::Min(0),
    ])
    .areas(area);
    draw_list(frame, list, app, theme);
    draw_preview(frame, preview, app, theme);
}

fn draw_list(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let view = &app.view;
    let block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" Artifacts · {} ", view.artifacts.len()));
    let width = block.inner(area).width.saturating_sub(1) as usize;
    let selected = view.selected_artifact().map(|(index, _)| index);
    let lines = if view.artifacts.is_empty() {
        vec![Line::from(Span::styled(
            " No Artifacts published.",
            theme.dim(),
        ))]
    } else {
        view.artifacts
            .iter()
            .enumerate()
            .map(|(index, artifact)| {
                let size = artifact
                    .byte_size
                    .map_or_else(|| artifact.classification.clone(), human_bytes);
                let text = format!(" {} {} ({size})", artifact.kind, artifact.display.title);
                let style = if selected == Some(index) {
                    theme.selected()
                } else {
                    ratatui::style::Style::default()
                };
                Line::from(Span::styled(truncate(&text, width), style))
            })
            .collect()
    };
    frame.render_widget(Paragraph::new(lines).block(block), area);
}

fn draw_preview(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Artifact Preview ");
    let width = block.inner(area).width.saturating_sub(1) as usize;
    let view = &app.view;
    let lines = match view.selected_artifact() {
        None => vec![Line::from(Span::styled(
            " No Artifact selected.",
            theme.dim(),
        ))],
        Some((_, artifact)) => {
            let size = artifact
                .byte_size
                .map(human_bytes)
                .unwrap_or_else(|| "-".to_string());
            let mut lines = Vec::new();
            for (label, value) in [
                ("Title:", artifact.display.title.as_str()),
                (
                    "Source:",
                    artifact.source.as_deref().unwrap_or("public_inbox"),
                ),
                ("Kind:", artifact.kind.as_str()),
                ("Class:", artifact.classification.as_str()),
                ("Media:", artifact.media_type.as_str()),
                ("Size:", size.as_str()),
                ("Ref:", artifact.r#ref.as_deref().unwrap_or("-")),
                ("Published:", artifact.published_at.as_str()),
            ] {
                lines.extend(wrapped_field(theme, label, value, width + 1));
            }
            lines.push(Line::from(""));
            lines.extend(wrapped(
                artifact.display.summary.as_deref().unwrap_or("No summary."),
                width + 1,
                1,
                theme.dim(),
            ));
            lines.push(Line::from(""));
            if artifact.classification == "conversation" {
                lines.push(Line::from(Span::styled(" Conversation", theme.hint())));
                if view.conversation_entries.is_empty() {
                    lines.push(Line::from(Span::styled(
                        " No Conversation entries recorded.",
                        theme.dim(),
                    )));
                }
                for entry in &view.conversation_entries {
                    let text = match entry.content_ref.as_ref() {
                        Some(reference) => format!(
                            " #{} {} [{}] [ref] {} ({})",
                            entry.sequence,
                            entry.author,
                            entry.kind,
                            reference.path,
                            human_bytes(reference.byte_size)
                        ),
                        None => format!(
                            " #{} {} [{}] {}",
                            entry.sequence,
                            entry.author,
                            entry.kind,
                            entry
                                .content
                                .as_deref()
                                .unwrap_or("")
                                .lines()
                                .next()
                                .unwrap_or("")
                        ),
                    };
                    lines.push(Line::from(truncate(&text, width)));
                }
                if view.conversation_entries_truncated {
                    lines.push(Line::from(Span::styled(
                        " Showing the first entries; the Host retains the full conversation.",
                        theme.hint(),
                    )));
                }
            } else if artifact.is_inspectable() {
                lines.push(Line::from(Span::styled(
                    " Press Enter to open the paged inspector.",
                    theme.hint(),
                )));
            }
            lines
        }
    };
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}

pub fn draw_inspector(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let Some(inspector) = app.view.inspector.as_ref() else {
        return;
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .title(format!(" Artifact: {} ", inspector.artifact_id));
    let Some(page) = inspector.page.as_ref() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                " Loading Artifact preview…",
                theme.dim(),
            )))
            .block(block),
            area,
        );
        return;
    };
    if inspector.classification == "binary" {
        let mut lines = Vec::new();
        if let Some(artifact) = app
            .view
            .artifacts
            .iter()
            .find(|artifact| artifact.artifact_id == inspector.artifact_id)
        {
            lines.extend([
                field(theme, "Kind:", &artifact.kind),
                field(theme, "Media:", &artifact.media_type),
                field(
                    theme,
                    "Size:",
                    &artifact
                        .byte_size
                        .map(human_bytes)
                        .unwrap_or_else(|| "-".to_string()),
                ),
                field(
                    theme,
                    "Digest:",
                    artifact.content_digest.as_deref().unwrap_or("-"),
                ),
                field(theme, "Published:", &artifact.published_at),
                field(theme, "Title:", &artifact.display.title),
                field(
                    theme,
                    "Summary:",
                    artifact.display.summary.as_deref().unwrap_or("-"),
                ),
                Line::from(""),
            ]);
        }
        lines.push(Line::from(Span::styled(
            " Binary Artifact: metadata only (no content bytes).",
            theme.dim(),
        )));
        frame.render_widget(Paragraph::new(lines).block(block), area);
        return;
    }
    let content = page.content.as_deref().unwrap_or("");
    let total = page
        .byte_size
        .map_or_else(|| "?".to_string(), |size| size.to_string());
    let mut status = format!(" bytes {}-{} of {total}", page.offset, page.end_offset());
    if page.eof {
        status.push_str(" · end of content");
    }
    if page.truncated {
        status.push_str(" · page truncated at UTF-8 boundary");
    }
    let mut lines = vec![
        Line::from(Span::styled(status, theme.dim())),
        Line::from(""),
    ];
    lines.extend(content.split('\n').map(|line| Line::from(line.to_string())));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        area,
    );
}
