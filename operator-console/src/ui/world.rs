//! Real image surface and Host-projected world state.

use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Wrap},
};

use super::{
    layout::{field, json_text, wrapped_field},
    theme::Theme,
};
use crate::app::App;
use crate::host::{AirSimAnnotation, AirSimStatus, FrameSource, OperatorWorld};

pub fn draw_world(frame: &mut Frame, app: &mut App, area: Rect, theme: Theme) {
    let width = usize::from(area.width.saturating_sub(2));
    let mut lines = Vec::new();
    if let Some(world) = app.view.world.as_ref() {
        lines.push(field(
            theme,
            "Viewer:",
            if world.viewer.available {
                "available"
            } else {
                world.viewer.reason.as_deref().unwrap_or("unavailable")
            },
        ));
        if let Some(state) = world.state.as_ref() {
            lines.push(field(
                theme,
                "Mission t:",
                &state
                    .mission_time_seconds
                    .map_or_else(|| "-".into(), |time| format!("{time:.1} s")),
            ));
            lines.push(field(
                theme,
                "Flight:",
                state.flight_state.as_deref().unwrap_or("-"),
            ));
            lines.push(field(
                theme,
                "Maneuver:",
                state.active_maneuver.as_deref().unwrap_or("-"),
            ));
            lines.push(field(
                theme,
                "Version:",
                &state
                    .state_version
                    .map_or_else(|| "-".into(), |version| version.to_string()),
            ));
        } else {
            lines.push(Line::from(Span::styled(
                " No world state available",
                theme.dim(),
            )));
        }
        lines.extend(airsim_lines(world, app.view.frame_source, theme, width));
        let sources = world
            .frames
            .iter()
            .map(|frame| frame.source.as_str())
            .collect::<Vec<_>>()
            .join(" / ");
        lines.push(field(
            theme,
            "Sources:",
            if sources.is_empty() {
                "none available"
            } else {
                &sources
            },
        ));
    } else {
        lines.push(Line::from(Span::styled(
            " Waiting for world state from the Host…",
            theme.dim(),
        )));
    }
    if let Some(environment) = app.view.environment.as_ref() {
        lines.push(field(theme, "Position:", &json_text(&environment.position)));
        lines.push(field(theme, "Velocity:", &json_text(&environment.velocity)));
    }
    let rows: usize = lines
        .iter()
        .map(|line| line.width().div_ceil(width.max(1)).max(1))
        .sum();
    let height = u16::try_from(rows + 2).unwrap_or(u16::MAX);
    let [image_area, state_area] =
        Layout::vertical([Constraint::Min(5), Constraint::Length(height)]).areas(area);
    draw_preview(frame, app, image_area, theme);
    frame.render_widget(
        Paragraph::new(lines).wrap(Wrap { trim: false }).block(
            Block::default()
                .borders(Borders::ALL)
                .title(Span::styled(" Authoritative World State ", theme.title())),
        ),
        state_area,
    );
}

/// The AirSim disclosure rows (ADR 0016): what AirSim shows (follower or
/// scene clock, lag), what the overlay boxes are, and, on the annotated
/// front camera, the Host's provenance sentence. None when AirSim is off.
pub(crate) fn airsim_lines(
    world: &OperatorWorld,
    source: FrameSource,
    theme: Theme,
    width: usize,
) -> Vec<Line<'static>> {
    let Some(airsim) = world.airsim.as_ref() else {
        return Vec::new();
    };
    let mut lines = wrapped_field(theme, "AirSim:", &airsim_text(airsim), width);
    if let Some(annotation) = airsim.annotation.as_ref() {
        lines.extend(wrapped_field(
            theme,
            "Overlay:",
            &overlay_text(annotation),
            width,
        ));
        if source == FrameSource::CameraFrontAnnotated {
            lines.extend(wrapped_field(
                theme,
                "Boxes:",
                &annotation.disclosure,
                width,
            ));
        }
    }
    lines
}

fn seconds(value: Option<&serde_json::Number>) -> Option<f64> {
    value.and_then(serde_json::Number::as_f64)
}

/// `world-model follower · capturing · lag 0.5 s · ship phase ±0.03 s`.
fn airsim_text(airsim: &AirSimStatus) -> String {
    let mut parts = vec![match airsim.mode.as_str() {
        "world_model_follower" => "world-model follower".to_string(),
        "scene_clock" => "scene clock".to_string(),
        other => other.to_string(),
    }];
    if airsim.mode != "world_model_follower" || airsim.perception != "off" {
        parts.push(format!("perception {}", airsim.perception));
    }
    parts.push(airsim.state.clone());
    if let Some(lag) = seconds(airsim.lag_seconds.as_ref()) {
        parts.push(format!("lag {lag:.1} s"));
    }
    if let Some(error) = seconds(airsim.ship_phase_error_seconds.as_ref()) {
        parts.push(format!("ship phase ±{:.2} s", error.abs()));
    }
    if let Some(reason) = airsim.reason.as_deref() {
        parts.push(reason.to_string());
    }
    parts.join(" · ")
}

/// What the boxes on the annotated front camera show.
fn overlay_text(annotation: &AirSimAnnotation) -> String {
    if annotation.match_ == "none" {
        let last = seconds(annotation.perception_mission_time_seconds.as_ref());
        // Ideal perception publishes only the ships it saw, so a frame without
        // a report cannot be told apart from a frame between samples.
        return match (annotation.kind.as_str(), last) {
            ("perception_ideal", Some(time)) => {
                format!(
                    "ideal perception reported no ships for this frame (last report {time:.1} s)"
                )
            }
            ("perception_ideal", None) => "ideal perception has reported no ships yet".to_string(),
            (_, Some(time)) => format!("no perception sample for this frame (last {time:.1} s)"),
            (_, None) => "no perception sample yet".to_string(),
        };
    }
    let count = |singular: &str, plural: &str| {
        let noun = if annotation.objects == 1 {
            singular
        } else {
            plural
        };
        format!("{} {noun}", annotation.objects)
    };
    let (what, objects) = match annotation.kind.as_str() {
        "ideal_segmentation" => ("ideal segmentation", count("object", "objects")),
        "perception_yolo" => ("YOLO detections", count("box", "boxes")),
        "perception_ideal" => ("ideal perception", count("ship", "ships")),
        other => (other, count("object", "objects")),
    };
    let mut text = format!("{what} · {objects}");
    match annotation.match_.as_str() {
        "exact" if annotation.kind != "ideal_segmentation" => text.push_str(" · same frame"),
        "exact" => {}
        other => text.push_str(&format!(" · match {other}")),
    }
    text
}

/// Overview, World and the presentation layout share one persistent
/// protocol. Only the currently visible surface requests an encode when its
/// actual cell dimensions change.
pub fn draw_preview(frame: &mut Frame, app: &mut App, area: Rect, theme: Theme) {
    let view = &mut app.view;
    let status = if view.presentation.is_frozen() {
        "frozen"
    } else if view.media.paused {
        "paused"
    } else if app.run.as_ref().is_some_and(|run| run.is_terminal()) {
        "final"
    } else {
        "live"
    };
    let sequence = view
        .frame
        .as_ref()
        .and_then(|frame| frame.sequence)
        .map_or_else(|| "-".into(), |sequence| sequence.to_string());
    let block = Block::default().borders(Borders::ALL).title(Span::styled(
        format!(
            " {} · seq {sequence} · {status} ",
            view.frame_source.label()
        ),
        theme.title(),
    ));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.is_empty() {
        return;
    }
    if !view.media.is_enabled() {
        frame.render_widget(
            Paragraph::new(" Images disabled (--image-protocol off)").style(theme.dim()),
            inner,
        );
        return;
    }
    if let Some(error) = view.frame_error.as_deref()
        && !view.media.is_ready()
    {
        frame.render_widget(
            Paragraph::new(error)
                .style(theme.error())
                .wrap(Wrap { trim: false }),
            inner,
        );
        return;
    }
    if view.frame.is_none() {
        let reason = view
            .world
            .as_ref()
            .and_then(|world| world.viewer.reason.as_deref())
            .unwrap_or("Waiting for a world frame…");
        frame.render_widget(
            Paragraph::new(reason)
                .style(theme.dim())
                .wrap(Wrap { trim: false }),
            inner,
        );
        return;
    }
    // The presentation layout fills its large surface, also upscaling.
    if view.presentation.active {
        view.media.render_scaled(frame, inner);
    } else {
        view.media.render(frame, inner);
    }
    // Keep a failure visible without discarding the last good frame.
    if let Some(error) = view.frame_error.as_deref() {
        let warning = Rect::new(inner.x, inner.bottom().saturating_sub(1), inner.width, 1);
        frame.render_widget(
            Paragraph::new(format!("▲ {error}")).style(theme.hint()),
            warning,
        );
    }
}
