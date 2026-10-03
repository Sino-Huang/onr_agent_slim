//! Responsive breakpoints and small text/layout helpers shared by screens.

use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};

use super::theme::Theme;
pub use crate::app::launch::wrap_line;

/// Responsive layout class derived from the terminal size.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Breakpoint {
    /// From the 100x30 minimum: side panes shrink or stack, no image.
    Compact,
    /// From 140x40.
    Standard,
    /// From 180x50: extra panes appear.
    Wide,
}

impl Breakpoint {
    pub fn of(area: Rect) -> Self {
        if area.width >= 180 && area.height >= 50 {
            Self::Wide
        } else if area.width >= 140 && area.height >= 40 {
            Self::Standard
        } else {
            Self::Compact
        }
    }

    /// Pick a value per breakpoint.
    pub fn pick<T>(self, compact: T, standard: T, wide: T) -> T {
        match self {
            Self::Compact => compact,
            Self::Standard => standard,
            Self::Wide => wide,
        }
    }
}

/// Rectangle of at most `width`x`height` centred in `area`.
pub fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}

/// Truncate to `width` characters with a trailing ellipsis.
pub fn truncate(value: &str, width: usize) -> String {
    if value.chars().count() <= width {
        return value.to_string();
    }
    if width <= 1 {
        return "…".chars().take(width).collect();
    }
    let mut result: String = value.chars().take(width - 1).collect();
    result.push('…');
    result
}

/// Label column width (including the leading space) for `label value` rows.
pub const LABEL_WIDTH: usize = 12;

/// `label value` row with a dim fixed-width label.
pub fn field(theme: Theme, label: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!(" {label:<11}"), theme.dim()),
        Span::raw(value.to_string()),
    ])
}

/// `label value` row truncated to `width` columns.
pub fn short_field(theme: Theme, label: &str, value: &str, width: usize) -> Line<'static> {
    field(
        theme,
        label,
        &truncate(value, width.saturating_sub(LABEL_WIDTH)),
    )
}

/// `label value` rows wrapped to `width` with continuation rows aligned
/// under the value.
pub fn wrapped_field(theme: Theme, label: &str, value: &str, width: usize) -> Vec<Line<'static>> {
    let rows = wrap_line(value, width.saturating_sub(LABEL_WIDTH));
    rows.into_iter()
        .enumerate()
        .map(|(index, row)| {
            let label = if index == 0 { label } else { "" };
            field(theme, label, &row)
        })
        .collect()
}

/// Text wrapped to `width` with every row indented by `indent` spaces.
pub fn wrapped(text: &str, width: usize, indent: usize, style: Style) -> Vec<Line<'static>> {
    wrap_line(text, width.saturating_sub(indent))
        .into_iter()
        .map(|row| Line::from(Span::styled(format!("{:indent$}{row}", ""), style)))
        .collect()
}

pub fn human_bytes(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    if bytes < 1024 {
        return format!("{bytes} B");
    }
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit + 1 < UNITS.len() {
        value /= 1024.0;
        unit += 1;
    }
    format!("{value:.1} {}", UNITS[unit])
}

/// Compact JSON, or `-` for null.
pub fn json_text(value: &serde_json::Value) -> String {
    if value.is_null() {
        "-".to_string()
    } else {
        serde_json::to_string(value).unwrap_or_else(|_| "-".to_string())
    }
}

/// `HH:MM:SS` for a non-negative second count.
pub fn clock_duration(seconds: i64) -> String {
    let seconds = seconds.max(0);
    format!(
        "{:02}:{:02}:{:02}",
        seconds / 3600,
        (seconds / 60) % 60,
        seconds % 60
    )
}

/// First `n` characters of an identifier, with an ellipsis if cut.
pub fn short_id(value: &str, n: usize) -> String {
    truncate(value, n + 1)
}
