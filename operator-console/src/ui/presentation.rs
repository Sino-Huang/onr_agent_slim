//! F4 presentation layout (issue #76 U11): the mission title and both
//! clocks, the phase stepper, live alerts, one large World source with its
//! AirSim disclosures, and one evidence card. Frozen, the clock row reads
//! `FROZEN AT <wall> · t=<mission>`; the alert rows are always live.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};

use super::layout::{Breakpoint, clock_duration, truncate, utc_time, wrapped};
use super::theme::{Badge, Importance, Theme};
use crate::app::presentation::{Alert, Evidence, Milestone};
use crate::app::run::run_elapsed_seconds;
use crate::app::{App, EvidenceCard, Liveness};

/// Draw the layout over `area` (the Run screen above the footer) and return
/// the body, where the Run screen's overlays (cancellation, rejection and
/// failure cards) go.
pub fn draw_presentation(
    frame: &mut Frame,
    area: Rect,
    app: &mut App,
    theme: Theme,
    breakpoint: Breakpoint,
) -> Rect {
    let frozen = app.view.presentation.is_frozen();
    // The waiting banner is live status; a frozen display holds still.
    let banner = (!frozen)
        .then(|| super::waiting::waiting_banner(app, theme, area.width))
        .flatten();
    let alerts: Vec<Line<'static>> = app
        .presentation_alerts()
        .iter()
        .map(|alert| alert_line(alert, theme, area.width))
        .collect();
    let [title, clocks, stepper, waiting, alert_rows, body] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(u16::from(banner.is_some())),
        Constraint::Length(u16::try_from(alerts.len()).unwrap_or(u16::MAX)),
        Constraint::Min(0),
    ])
    .areas(area);
    frame.render_widget(Paragraph::new(title_line(app, theme)), title);
    frame.render_widget(Paragraph::new(clock_line(app, theme)), clocks);
    frame.render_widget(
        Paragraph::new(super::overview::phase_line(
            app.presentation_evidence().phase,
            super::overview::mission_rejected(app),
            theme,
        )),
        stepper,
    );
    if let Some(banner) = banner {
        frame.render_widget(Paragraph::new(banner), waiting);
    }
    frame.render_widget(Paragraph::new(alerts), alert_rows);

    let [scene, card] = Layout::horizontal(match breakpoint {
        Breakpoint::Compact => [Constraint::Percentage(58), Constraint::Percentage(42)],
        _ => [Constraint::Percentage(64), Constraint::Percentage(36)],
    })
    .areas(body);
    draw_scene(frame, scene, app, theme);
    draw_card(frame, card, app, theme);
    body
}

/// ` PRESENTATION  Mission 1 · Harbor (simulated) · ● running │ host ● live`.
fn title_line(app: &App, theme: Theme) -> Line<'static> {
    let mut spans = Vec::new();
    if app.viewing_history() {
        spans.push(Span::styled(" HISTORICAL ", theme.historical()));
        spans.push(Span::raw(" "));
    }
    spans.push(Span::styled(" PRESENTATION ", theme.active_tab()));
    spans.push(Span::raw(" "));
    let Some(run) = app.run.as_ref() else {
        spans.push(Span::styled(
            "waiting for the current Mission Run…",
            theme.dim(),
        ));
        return Line::from(spans);
    };
    spans.push(Span::styled(
        app.mission_title().unwrap_or_default(),
        theme.title(),
    ));
    spans.push(Span::styled(" · ", theme.dim()));
    spans.push(theme.status_dot(&run.status));
    let mut status = format!(" {}", run.status);
    if let Some(classification) = run.terminal_classification.as_deref() {
        status.push_str(&format!(" · {classification}"));
    }
    spans.push(Span::styled(status, theme.run_status(&run.status)));
    spans.push(Span::styled(" │ host ", theme.dim()));
    spans.extend(match app.liveness() {
        Liveness::Live => [Span::styled("●", theme.good()), Span::raw(" live")],
        Liveness::Stale => [Span::styled("▲", theme.hint()), Span::raw(" stale")],
        Liveness::Offline => [Span::styled("✖", theme.error()), Span::raw(" offline")],
        Liveness::Idle => [Span::styled("○", theme.dim()), Span::raw(" idle")],
    });
    Line::from(spans)
}

/// Both clocks: wall time of day and the run's wall elapsed, then Mission
/// time; frozen, the moment the display holds.
fn clock_line(app: &App, theme: Theme) -> Line<'static> {
    let mission = app
        .presentation_evidence()
        .mission_time
        .map_or_else(|| "-".to_string(), |time| format!("{time} s"));
    if let Some(frozen) = app.view.presentation.frozen() {
        let mut spans = vec![Span::styled(
            format!(" FROZEN AT {} · t={mission}", utc_time(frozen.wall_unix)),
            theme.hint().patch(theme.title()),
        )];
        if let Some(elapsed) = frozen.run_elapsed {
            spans.push(Span::styled(
                format!(" · run {}", clock_duration(elapsed)),
                theme.dim(),
            ));
        }
        spans.push(Span::styled(
            " · display held; polling continues",
            theme.dim(),
        ));
        return Line::from(spans);
    }
    let now = app.unix_now();
    let mut spans = vec![
        Span::styled(" Wall ", theme.dim()),
        Span::styled(utc_time(now), theme.title()),
    ];
    if let Some(elapsed) = app
        .run
        .as_ref()
        .and_then(|run| run_elapsed_seconds(run, now))
    {
        spans.push(Span::styled(" · run ", theme.dim()));
        spans.push(Span::raw(clock_duration(elapsed)));
    }
    spans.push(Span::styled(" │ Mission ", theme.dim()));
    spans.push(Span::styled(format!("t={mission}"), theme.title()));
    Line::from(spans)
}

fn alert_line(alert: &Alert, theme: Theme, width: u16) -> Line<'static> {
    let style = match alert {
        Alert::Failure { .. } | Alert::HostOffline => theme.error(),
        Alert::HumanDecision { .. } => theme
            .run_status(crate::app::attention::AWAITING_HUMAN_DECISION)
            .patch(theme.title()),
        Alert::HostStale => theme.hint(),
    };
    Line::from(Span::styled(
        truncate(
            &format!(" {}", alert.text()),
            usize::from(width).saturating_sub(1),
        ),
        style,
    ))
}

/// The selected World source, large, with its AirSim disclosures below
/// (the follower and annotation disclosures of ADR 0016).
fn draw_scene(frame: &mut Frame, area: Rect, app: &mut App, theme: Theme) {
    let width = usize::from(area.width.saturating_sub(2));
    let disclosure = app
        .presentation_evidence()
        .world
        .map(|world| super::world::airsim_lines(world, app.view.frame_source, theme, width.max(1)))
        .unwrap_or_default();
    let height = if disclosure.is_empty() {
        0
    } else {
        u16::try_from(disclosure.len() + 2).unwrap_or(u16::MAX)
    };
    let [image, notes] =
        Layout::vertical([Constraint::Min(5), Constraint::Length(height)]).areas(area);
    super::world::draw_preview(frame, app, image, theme);
    if !disclosure.is_empty() {
        frame.render_widget(
            Paragraph::new(disclosure).block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(Span::styled(" What the scene shows ", theme.title())),
            ),
            notes,
        );
    }
}

fn draw_card(frame: &mut Frame, area: Rect, app: &App, theme: Theme) {
    let kind = app.view.presentation.card;
    let block = Block::default()
        .borders(Borders::ALL)
        .title(Line::from(vec![
            Span::styled(format!(" {} ", kind.label()), theme.title()),
            Span::styled(format!("· v {} ", kind.next().label()), theme.dim()),
        ]));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let width = usize::from(inner.width);
    let evidence = app.presentation_evidence();
    match kind {
        EvidenceCard::Milestone => frame.render_widget(
            Paragraph::new(milestone_lines(&evidence, theme, width)),
            inner,
        ),
        EvidenceCard::Belief => draw_belief(frame, inner, &evidence, theme),
        EvidenceCard::Invocation => frame.render_widget(
            Paragraph::new(invocation_card_lines(&evidence, theme, width)),
            inner,
        ),
    }
}

fn origin_line(text: &str, following: bool, width: usize, theme: Theme) -> Line<'static> {
    let mode = if following {
        "following newest"
    } else {
        "pinned"
    };
    Line::from(Span::styled(
        truncate(&format!(" {text} · {mode}"), width),
        theme.dim(),
    ))
}

/// The current phase step, then the Progress selection.
fn milestone_lines(evidence: &Evidence<'_>, theme: Theme, width: usize) -> Vec<Line<'static>> {
    let mut lines = vec![origin_line(
        "2 Progress selection",
        evidence.progress_following,
        width,
        theme,
    )];
    if let Some((phase, step)) = evidence.phase.and_then(|phase| {
        phase
            .steps
            .iter()
            .find(|step| step.id == phase.current)
            .map(|step| (phase, step))
    }) {
        let mut text = format!(
            "Phase {}/{} · {}",
            position(phase, step),
            phase.steps.len(),
            step.label
        );
        if let Some(detail) = step.detail.as_deref() {
            text.push_str(&format!(" ({detail})"));
        }
        lines.extend(wrapped(&text, width, 1, theme.title()));
    }
    lines.push(Line::from(""));
    match evidence.milestone {
        Milestone::Narrative(narrative) => {
            lines.push(Line::from(vec![
                Span::raw(" "),
                theme.ai_badge(),
                Span::styled(" Run Narrative · non-authoritative", theme.hint()),
            ]));
            lines.extend(wrapped(
                narrative
                    .and_then(|narrative| narrative.text.as_deref())
                    .unwrap_or("Narrative is not available yet."),
                width,
                1,
                theme.ai(),
            ));
        }
        Milestone::Node { node, parent } => {
            let importance = Importance::parse(&node.importance).unwrap_or(Importance::Routine);
            let mut head = vec![
                Span::raw(" "),
                theme.importance_mark(importance),
                Span::raw(" "),
            ];
            if node.level == "record" {
                head.push(theme.badge(Badge::for_source(&node.source, node.event_kind.as_deref())));
                head.push(Span::raw(" "));
            }
            if !node.authoritative {
                head.push(theme.ai_badge());
                head.push(Span::raw(" "));
            }
            lines.push(Line::from(head));
            let title = if node.level == "summary" {
                format!("{} ({} records)", node.title, node.child_count)
            } else {
                node.title.clone()
            };
            let style = if node.authoritative {
                theme.importance(importance).patch(theme.title())
            } else {
                theme.ai()
            };
            lines.extend(wrapped(&title, width, 1, style));
            let mut facts = format!("{} · {}", node.source, node.importance);
            if let Some(time) = node.mission_time_seconds {
                facts.push_str(&format!(" · t={time:.1} s"));
            }
            lines.extend(wrapped(&facts, width, 1, theme.dim()));
            if let Some(parent) = parent {
                lines.extend(wrapped(
                    &format!("in {}", parent.title),
                    width,
                    1,
                    theme.dim(),
                ));
            }
            if let Some(text) = node.text.as_deref().filter(|text| !text.is_empty()) {
                lines.push(Line::from(""));
                lines.extend(wrapped(
                    text,
                    width,
                    1,
                    if node.authoritative {
                        Style::default()
                    } else {
                        theme.ai()
                    },
                ));
            }
        }
        Milestone::None => lines.push(Line::from(Span::styled(
            " No Progress record selected yet.",
            theme.dim(),
        ))),
    }
    lines
}

fn position(phase: &crate::host::RunPhase, step: &crate::host::PhaseStep) -> usize {
    phase
        .steps
        .iter()
        .position(|candidate| candidate.id == step.id)
        .map_or(0, |index| index + 1)
}

/// The Belief/Context entity selection, drawn as on its tab.
fn draw_belief(frame: &mut Frame, area: Rect, evidence: &Evidence<'_>, theme: Theme) {
    let width = usize::from(area.width);
    let mut header = vec![Line::from(Span::styled(
        truncate(" 4 Belief/Context selection", width),
        theme.dim(),
    ))];
    let entity = evidence.beliefs.and_then(|beliefs| {
        let kind = beliefs.belief_kind.as_deref()?;
        let index = beliefs.selected_index(evidence.belief_entity)?;
        Some((beliefs, kind, index))
    });
    let Some((beliefs, kind, index)) = entity else {
        let reason = match evidence.beliefs {
            None => "Waiting for the Host; data not yet available.",
            Some(beliefs) if beliefs.belief_kind.is_none() => beliefs
                .reason
                .as_deref()
                .unwrap_or("No belief reason supplied by the Host."),
            Some(_) => "No entity marginals published.",
        };
        header.push(Line::from(""));
        header.extend(wrapped(reason, width, 1, theme.dim()));
        frame.render_widget(Paragraph::new(header), area);
        return;
    };
    header.push(Line::from(truncate(
        &format!(
            " {kind} · revision {}",
            beliefs
                .revision
                .map_or_else(|| "—".to_string(), |revision| revision.to_string())
        ),
        width,
    )));
    let [top, entity_area] =
        Layout::vertical([Constraint::Length(3), Constraint::Min(0)]).areas(area);
    frame.render_widget(Paragraph::new(header), top);
    super::belief_context::draw_entity(frame, entity_area, index, beliefs, theme);
}

/// The Agents invocation selection.
fn invocation_card_lines(
    evidence: &Evidence<'_>,
    theme: Theme,
    width: usize,
) -> Vec<Line<'static>> {
    let mut lines = vec![origin_line(
        "3 Agents selection",
        evidence.agents_following,
        width,
        theme,
    )];
    let Some(invocation) = evidence.invocation else {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            " No agent invocation recorded yet.",
            theme.dim(),
        )));
        return lines;
    };
    lines.push(Line::from(vec![
        Span::raw(" "),
        theme.badge(super::agents::role_badge(&invocation.role)),
        Span::styled(
            truncate(
                &format!(" {} · {}", invocation.name, invocation.completion_state),
                width.saturating_sub(4),
            ),
            theme.title(),
        ),
    ]));
    lines.extend(super::agents::invocation_lines(invocation, theme, width));
    lines
}
