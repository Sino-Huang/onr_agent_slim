//! Bayesian evidence and the persisted Context Coordination snapshot.
//! Missing values remain missing: gauges use published fractions and trends use
//! only the per-revision means supplied by the Host.

use std::collections::BTreeSet;

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Modifier;
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, Borders, Cell, Gauge, Paragraph, Row, Sparkline, Table, TableState, Wrap,
};

use super::layout::{json_text, truncate, wrapped};
use super::theme::{Importance, Theme};
use crate::host::{MissionSnapshotSummary, OperatorBeliefs, OperatorContext};

/// Belief metadata rows above the entity table.
const METADATA_ROWS: u16 = 2;
/// Selected-entity detail: gauge, deltas, history label, trend, honest/CI,
/// outcomes and variance.
const DETAIL_ROWS: u16 = 7;

/// `selected_entity` is a stable entity ID; unset or unpublished selects the
/// first entity.
pub fn draw_belief_context(
    frame: &mut Frame,
    area: Rect,
    beliefs: Option<&OperatorBeliefs>,
    selected_entity: Option<&str>,
    context: Option<&OperatorContext>,
    theme: Theme,
) {
    let [left, right] =
        Layout::horizontal([Constraint::Percentage(44), Constraint::Percentage(56)]).areas(area);
    draw_beliefs(frame, left, beliefs, selected_entity, theme);
    draw_context(frame, right, context, theme);
}

fn block(title: &'static str, theme: Theme) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .title(title)
        .title_style(theme.title())
}

fn absent(frame: &mut Frame, area: Rect, title: &'static str, theme: Theme) {
    frame.render_widget(
        Paragraph::new("Waiting for the Host; data not yet available.")
            .style(theme.dim())
            .wrap(Wrap { trim: true })
            .block(block(title, theme)),
        area,
    );
}

fn number(value: Option<f64>) -> String {
    value.map_or_else(|| "—".into(), |value| format!("{value:.4}"))
}

fn delta(value: Option<f64>) -> String {
    value.map_or_else(|| "—".into(), |value| format!("{value:+.4}"))
}

fn revision(value: Option<u64>) -> String {
    value.map_or_else(|| "—".into(), |value| value.to_string())
}

fn draw_beliefs(
    frame: &mut Frame,
    area: Rect,
    beliefs: Option<&OperatorBeliefs>,
    selected_entity: Option<&str>,
    theme: Theme,
) {
    let Some(beliefs) = beliefs else {
        absent(frame, area, " Bayesian Belief ", theme);
        return;
    };
    let panel = block(" Bayesian Belief ", theme);
    let inner = panel.inner(area);
    frame.render_widget(panel, area);
    let Some(kind) = beliefs.belief_kind.as_deref() else {
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled("No Bayesian belief", theme.title())),
                Line::from(
                    beliefs
                        .reason
                        .as_deref()
                        .unwrap_or("No belief reason supplied by the Host."),
                ),
            ])
            .wrap(Wrap { trim: true }),
            inner,
        );
        return;
    };
    let selected = beliefs.selected_index(selected_entity);
    // The selected entity's detail always fits; the table scrolls in what remains.
    let detail_height = DETAIL_ROWS.min(inner.height.saturating_sub(METADATA_ROWS + 2));
    let table_height = u16::try_from(beliefs.entities.len())
        .unwrap_or(u16::MAX)
        .saturating_add(1)
        .min(inner.height.saturating_sub(METADATA_ROWS + detail_height));
    let [metadata, table, detail] = Layout::vertical([
        Constraint::Length(METADATA_ROWS),
        Constraint::Length(table_height),
        Constraint::Min(detail_height),
    ])
    .areas(inner);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(format!("{kind} · revision {}", revision(beliefs.revision))),
            Line::from(Span::styled(
                beliefs.created_at.as_deref().unwrap_or("time unavailable"),
                theme.dim(),
            )),
        ]),
        metadata,
    );
    let rows = beliefs.entities.iter().map(|entity| {
        Row::new(vec![
            Cell::from(entity.label.as_str()),
            Cell::from(format!("{:.4}", entity.mean)),
            Cell::from(delta(entity.delta_since_previous)),
            Cell::from(delta(entity.delta_since_prior)),
        ])
    });
    let mut state = TableState::new().with_selected(selected);
    frame.render_stateful_widget(
        Table::new(
            rows,
            [
                Constraint::Min(8),
                Constraint::Length(6),
                Constraint::Length(7),
                Constraint::Length(7),
            ],
        )
        .column_spacing(1)
        .header(Row::new(["Entity", "Mean", "Δ prev", "Δ prior"]).style(theme.title()))
        .row_highlight_style(theme.selected())
        .highlight_symbol("▶"),
        table,
        &mut state,
    );
    match selected {
        Some(index) => draw_entity(frame, detail, index, beliefs, theme),
        None => frame.render_widget(
            Paragraph::new("No entity marginals published.").style(theme.dim()),
            detail,
        ),
    }
}

fn draw_entity(
    frame: &mut Frame,
    area: Rect,
    index: usize,
    beliefs: &OperatorBeliefs,
    theme: Theme,
) {
    let entity = &beliefs.entities[index];
    let [gauge, deltas, history, trend, details] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(0),
    ])
    .areas(area);
    frame.render_widget(
        Paragraph::new(format!(
            "{}/{} · Δ prev {} · Δ prior {}",
            index + 1,
            beliefs.entities.len(),
            delta(entity.delta_since_previous),
            delta(entity.delta_since_prior),
        )),
        deltas,
    );
    frame.render_widget(
        Gauge::default()
            .ratio(entity.mean.clamp(0.0, 1.0))
            .label(format!("{} {:.4}", entity.label, entity.mean))
            .use_unicode(true)
            .gauge_style(
                theme
                    .importance(Importance::Routine)
                    .add_modifier(Modifier::REVERSED),
            ),
        gauge,
    );
    let available = entity.means_by_revision.iter().any(Option::is_some);
    let history_label = if available {
        match (beliefs.history.first(), beliefs.history.last()) {
            (Some(first), Some(last)) => format!(
                "Mean history · revisions {}–{}",
                first.revision, last.revision
            ),
            _ => "Mean history · revision labels unavailable".into(),
        }
    } else {
        "Mean history unavailable".into()
    };
    frame.render_widget(Paragraph::new(history_label).style(theme.dim()), history);
    draw_trend(frame, trend, &entity.means_by_revision, theme);
    let mut lines = vec![Line::from(format!(
        "Honest {} · CI {}",
        number(entity.honest_probability),
        entity.credible_interval.map_or_else(
            || "—".into(),
            |[low, high]| format!("[{low:.4}, {high:.4}]")
        ),
    ))];
    if let Some(counts) = &entity.outcome_counts {
        lines.push(Line::from(format!(
            "Outcomes {}",
            counts
                .iter()
                .map(|(name, count)| format!("{name}={count}"))
                .collect::<Vec<_>>()
                .join(" · "),
        )));
    }
    if let Some(variance) = entity.variance {
        lines.push(Line::from(format!("Variance {variance:.4}")));
    }
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), details);
}

/// Render contiguous known stretches separately: an absent revision is a blank
/// column, not an invented zero or an interpolated observation.
fn draw_trend(frame: &mut Frame, area: Rect, means: &[Option<f64>], theme: Theme) {
    let start = means.len().saturating_sub(usize::from(area.width));
    let visible = &means[start..];
    let mut index = 0;
    while index < visible.len() {
        if visible[index].is_none() {
            index += 1;
            continue;
        }
        let first = index;
        let mut values = Vec::new();
        while index < visible.len() {
            let Some(mean) = visible[index] else { break };
            values.push((mean.clamp(0.0, 1.0) * 10_000.0).round() as u64);
            index += 1;
        }
        frame.render_widget(
            Sparkline::default()
                .data(&values)
                .max(10_000)
                .style(theme.importance(Importance::Routine)),
            Rect::new(
                area.x + first as u16,
                area.y,
                values.len() as u16,
                area.height,
            ),
        );
    }
}

fn source_lines(
    snapshot: &MissionSnapshotSummary,
    theme: Theme,
    compact: bool,
) -> Vec<Line<'static>> {
    let names: BTreeSet<_> = snapshot
        .source_health
        .keys()
        .chain(snapshot.source_freshness.keys())
        .chain(snapshot.source_revisions.keys())
        .chain(snapshot.missing_sources.iter())
        .collect();
    names
        .into_iter()
        .map(|name| {
            let missing = snapshot.missing_sources.contains(name);
            let health = snapshot
                .source_health
                .get(name)
                .map(String::as_str)
                .unwrap_or("unknown");
            let fresh = snapshot.source_freshness.get(name).copied();
            let warning = missing || fresh == Some(false) || !matches!(health, "healthy" | "ok");
            let importance = if warning {
                Importance::Warning
            } else {
                Importance::Routine
            };
            let label = if compact {
                match name.as_str() {
                    "active_maneuver" => "Maneuver",
                    "bayesian_belief_snapshot" => "Belief",
                    "environment_data" => "Environment",
                    "fsm_status" => "FSM",
                    _ => name.as_str(),
                }
            } else {
                name.as_str()
            };
            Line::from(vec![
                theme.importance_mark(importance),
                Span::styled(
                    format!(
                        " {label}: {} · {} · r{}",
                        if missing { "missing" } else { health },
                        match fresh {
                            Some(true) => "fresh",
                            Some(false) => "stale",
                            None => "freshness unknown",
                        },
                        revision(snapshot.source_revisions.get(name).copied()),
                    ),
                    theme.importance(importance),
                ),
            ])
        })
        .collect()
}

fn compact_readiness(value: &serde_json::Value) -> String {
    if value.is_null() {
        return "readiness unavailable".into();
    }
    // The persisted readiness object has a time gate and live evidence. Keep
    // the time gate visible even when the compact pane cannot fit all evidence.
    if let Some(seconds) = value.get("not_before").and_then(|gate| gate.get("seconds")) {
        return format!("not before {seconds}s");
    }
    json_text(value)
}

fn section(frame: &mut Frame, area: Rect, title: &str, lines: Vec<Line<'static>>, theme: Theme) {
    let [header, content] =
        Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).areas(area);
    frame.render_widget(Paragraph::new(title).style(theme.title()), header);
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), content);
}

fn draw_context(frame: &mut Frame, area: Rect, context: Option<&OperatorContext>, theme: Theme) {
    let Some(context) = context else {
        absent(frame, area, " Mission Context ", theme);
        return;
    };
    let panel = block(" Mission Context ", theme);
    let inner = panel.inner(area);
    frame.render_widget(panel, area);
    let compact = inner.height < 32;
    let source_count = context.mission_snapshot.as_ref().map_or(0, |snapshot| {
        snapshot
            .source_health
            .keys()
            .chain(snapshot.source_freshness.keys())
            .chain(snapshot.source_revisions.keys())
            .chain(snapshot.missing_sources.iter())
            .collect::<BTreeSet<_>>()
            .len()
    });
    let snapshot_height = if context.mission_snapshot.is_some() {
        source_count as u16 + if compact { 2 } else { 4 }
    } else {
        2
    };
    let candidates = context
        .fsm_status
        .as_ref()
        .map_or(0, |fsm| fsm.transition_candidates.len());
    let fsm_height = if compact {
        2 + (candidates as u16 * 2).max(1)
    } else {
        3 + (candidates as u16 * 4).max(1)
    };
    let [
        snapshot_area,
        fsm_area,
        maneuver_area,
        intent_area,
        hyper_area,
    ] = Layout::vertical([
        Constraint::Length(snapshot_height),
        Constraint::Length(fsm_height),
        Constraint::Length(4),
        Constraint::Length(if compact { 3 } else { 7 }),
        Constraint::Min(if compact { 2 } else { 3 }),
    ])
    .areas(inner);

    let mut lines = Vec::new();
    if let Some(snapshot) = &context.mission_snapshot {
        lines.push(Line::from(format!(
            "Version {} · plan r{}",
            snapshot.version,
            revision(snapshot.plan_revision)
        )));
        if !compact {
            lines.push(Line::from(format!(
                "Time {} · sequence {}",
                snapshot.created_at.as_deref().unwrap_or("unavailable"),
                revision(snapshot.sequence)
            )));
        }
        lines.extend(source_lines(snapshot, theme, compact));
        if !compact {
            lines.push(Line::from(format!(
                "Missing sources: {}",
                if snapshot.missing_sources.is_empty() {
                    "none".into()
                } else {
                    snapshot.missing_sources.join(", ")
                }
            )));
        }
    } else {
        lines.push(Line::from(Span::styled(
            "Snapshot unavailable",
            theme.dim(),
        )));
    }
    section(
        frame,
        snapshot_area,
        "Mission Snapshot · source health / freshness",
        lines,
        theme,
    );

    let mut lines = Vec::new();
    if let Some(fsm) = &context.fsm_status {
        lines.push(Line::from(format!(
            "Active: {} · {}",
            fsm.active_state.as_deref().unwrap_or("unavailable"),
            fsm.status.as_deref().unwrap_or("status unavailable")
        )));
        if !compact {
            lines.push(Line::from(format!(
                "Plan r{} · statechart r{} · event {}",
                revision(fsm.plan_revision),
                revision(fsm.statechart_revision),
                fsm.last_applied_event.as_deref().unwrap_or("unavailable")
            )));
        }
        if fsm.transition_candidates.is_empty() {
            lines.push(Line::from("No enabled Transition Candidates"));
        }
        for candidate in &fsm.transition_candidates {
            if compact {
                let target = format!(
                    "→ {} · {}",
                    candidate.target,
                    compact_readiness(&candidate.readiness)
                );
                lines.push(Line::from(truncate(&target, usize::from(fsm_area.width))));
                let condition = format!(
                    "If {}",
                    candidate.condition.as_deref().unwrap_or("unconditional")
                );
                lines.push(Line::from(truncate(
                    &condition,
                    usize::from(fsm_area.width),
                )));
            } else {
                lines.push(Line::from(format!(
                    "{}: {} → {}",
                    candidate.event, candidate.source, candidate.target
                )));
                lines.push(Line::from(format!(
                    "Condition: {}",
                    candidate.condition.as_deref().unwrap_or("unconditional")
                )));
                lines.push(Line::from(format!(
                    "Readiness: {}",
                    json_text(&candidate.readiness)
                )));
            }
        }
    } else {
        lines.push(Line::from(Span::styled(
            "FSM status unavailable",
            theme.dim(),
        )));
    }
    section(
        frame,
        fsm_area,
        "FSM · enabled Transition Candidates",
        lines,
        theme,
    );

    let mut lines = Vec::new();
    if let Some(maneuver) = &context.active_maneuver {
        lines.push(Line::from(format!(
            "{} · {}{}",
            maneuver.action.as_deref().unwrap_or("action unavailable"),
            maneuver.status.as_deref().unwrap_or("status unavailable"),
            maneuver
                .phase
                .as_ref()
                .map_or_else(String::new, |phase| format!(" · {phase}"))
        )));
        lines.push(Line::from(format!(
            "Deadline {}s · remaining {}s",
            number(maneuver.deadline_seconds),
            number(maneuver.remaining_seconds)
        )));
        let [summary, progress] =
            Layout::vertical([Constraint::Min(0), Constraint::Length(1)]).areas(maneuver_area);
        section(frame, summary, "Active Maneuver", lines, theme);
        if let Some(fraction) = maneuver.progress {
            frame.render_widget(
                Gauge::default()
                    .ratio(fraction.clamp(0.0, 1.0))
                    .use_unicode(true)
                    .label(format!("{:.1}%", fraction * 100.0))
                    .gauge_style(
                        theme
                            .importance(Importance::Routine)
                            .add_modifier(Modifier::REVERSED),
                    ),
                progress,
            );
        } else {
            frame.render_widget(
                Paragraph::new(json_text(&maneuver.progress_detail)).wrap(Wrap { trim: true }),
                progress,
            );
        }
    } else {
        section(
            frame,
            maneuver_area,
            "Active Maneuver",
            vec![Line::from(Span::styled(
                "No persisted active maneuver",
                theme.dim(),
            ))],
            theme,
        );
    }

    let mut lines = Vec::new();
    if let Some(intent) = &context.latest_transition_intent {
        lines.push(Line::from(format!(
            "{} → {}",
            intent.status.as_deref().unwrap_or("status unavailable"),
            intent.target
        )));
        lines.push(Line::from(format!(
            "If {}",
            intent.condition.as_deref().unwrap_or("unconditional")
        )));
        if !compact {
            lines.push(Line::from(format!(
                "From {} · plan r{} · selected {}s",
                intent.source_state.as_deref().unwrap_or("unavailable"),
                revision(intent.plan_revision),
                number(intent.selected_at_seconds)
            )));
            if let Some(rationale) = &intent.rationale {
                lines.push(Line::from(vec![
                    theme.ai_badge(),
                    Span::styled(format!(" {rationale}"), theme.ai()),
                ]));
            }
        }
    } else {
        lines.push(Line::from(Span::styled(
            "No persisted Transition Intent",
            theme.dim(),
        )));
    }
    section(frame, intent_area, "Latest Transition Intent", lines, theme);

    let mut lines = Vec::new();
    if let Some(outcome) = &context.latest_hyper_outcome {
        lines.push(Line::from(format!(
            "{} · plan r{}",
            outcome.disposition,
            revision(outcome.plan_revision)
        )));
        if let Some(evidence) = &outcome.evidence_summary {
            lines.extend(wrapped(
                evidence,
                usize::from(hyper_area.width),
                0,
                theme.ai(),
            ));
        }
        if !compact {
            lines.push(Line::from(format!(
                "Triggers: {}",
                outcome.trigger_identities.join(", ")
            )));
            lines.push(Line::from(format!(
                "Requests: {}",
                outcome.request_identities.join(", ")
            )));
        }
    } else {
        lines.push(Line::from(Span::styled(
            "No persisted Hyper outcome",
            theme.dim(),
        )));
    }
    section(
        frame,
        hyper_area,
        "Latest Hyper outcome · AI non-authoritative",
        lines,
        theme,
    );
}

pub fn draw_belief_mini(
    frame: &mut Frame,
    area: Rect,
    beliefs: Option<&OperatorBeliefs>,
    theme: Theme,
) {
    let Some(beliefs) = beliefs else {
        absent(frame, area, " Belief ", theme);
        return;
    };
    let mut lines = Vec::new();
    if let Some(kind) = &beliefs.belief_kind {
        lines.push(Line::from(format!(
            "{kind} · r{}",
            revision(beliefs.revision)
        )));
        for entity in &beliefs.entities {
            lines.push(Line::from(format!(
                "{} {:.4} · Δ prior {} · prev {}",
                entity.label,
                entity.mean,
                delta(entity.delta_since_prior),
                delta(entity.delta_since_previous)
            )));
        }
    } else {
        lines.push(Line::from("No Bayesian belief"));
        lines.push(Line::from(
            beliefs
                .reason
                .as_deref()
                .unwrap_or("No reason supplied by the Host."),
        ));
    }
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: true })
            .block(block(" Belief ", theme)),
        area,
    );
}

pub fn draw_context_mini(
    frame: &mut Frame,
    area: Rect,
    context: Option<&OperatorContext>,
    theme: Theme,
) {
    let Some(context) = context else {
        absent(frame, area, " Context ", theme);
        return;
    };
    let mut lines = Vec::new();
    if let Some(snapshot) = &context.mission_snapshot {
        lines.push(Line::from(format!(
            "Snapshot v{} · plan r{}",
            snapshot.version,
            revision(snapshot.plan_revision)
        )));
        let stale: Vec<_> = snapshot
            .source_freshness
            .iter()
            .filter(|(_, fresh)| !**fresh)
            .map(|(name, _)| name.as_str())
            .collect();
        let unhealthy: Vec<_> = snapshot
            .source_health
            .iter()
            .filter(|(_, health)| !matches!(health.as_str(), "healthy" | "ok"))
            .map(|(name, _)| name.as_str())
            .collect();
        if !snapshot.missing_sources.is_empty() || !stale.is_empty() || !unhealthy.is_empty() {
            lines.push(Line::from(vec![
                theme.importance_mark(Importance::Warning),
                Span::styled(
                    format!(
                        " missing [{}] · stale [{}] · unhealthy [{}]",
                        snapshot.missing_sources.join(", "),
                        stale.join(", "),
                        unhealthy.join(", ")
                    ),
                    theme.importance(Importance::Warning),
                ),
            ]));
        } else {
            lines.push(Line::from("Sources: no reported health/freshness warnings"));
        }
    } else {
        lines.push(Line::from(Span::styled(
            "Snapshot unavailable",
            theme.dim(),
        )));
    }
    if let Some(fsm) = &context.fsm_status {
        lines.push(Line::from(format!(
            "FSM {} · candidates {}",
            fsm.active_state.as_deref().unwrap_or("unavailable"),
            fsm.transition_candidates.len()
        )));
    }
    if let Some(maneuver) = &context.active_maneuver {
        lines.push(Line::from(format!(
            "{} · {}",
            maneuver.action.as_deref().unwrap_or("action unavailable"),
            maneuver.status.as_deref().unwrap_or("status unavailable")
        )));
    }
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: true })
            .block(block(" Context ", theme)),
        area,
    );
}
