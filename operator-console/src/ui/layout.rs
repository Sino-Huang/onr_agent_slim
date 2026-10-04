//! Responsive breakpoints and small text/layout helpers shared by screens.

use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};

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

/// `HH:MM:SS UTC` wall-clock time of day for Unix seconds (the console
/// carries no time-zone database, so wall time is shown in UTC).
pub fn utc_time(unix: i64) -> String {
    format!("{} UTC", clock_duration(unix.rem_euclid(86_400)))
}

/// `M:SS` for a non-negative second count (minutes are not wrapped).
pub fn short_duration(seconds: i64) -> String {
    let seconds = seconds.max(0);
    format!("{}:{:02}", seconds / 60, seconds % 60)
}

/// Whole seconds from an RFC 3339 `start` to `end` (Unix seconds), at least 0.
pub fn seconds_between(start: &str, end: i64) -> Option<i64> {
    Some((end - crate::app::run::parse_rfc3339(start)?).max(0))
}

/// Braille spinner frames; one frame per 100 ms of app-clock time.
pub const SPINNER: [char; 10] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

/// First `n` characters of an identifier, with an ellipsis if cut.
pub fn short_id(value: &str, n: usize) -> String {
    truncate(value, n + 1)
}

/// First visible row of a `height`-row window over `len` rows that keeps
/// `selected` visible. It moves the previous `offset` only as far as the
/// selection needs and never past the last full window, so a kept offset
/// survives evidence updates and resizes.
pub fn scroll_into_view(
    offset: usize,
    selected: Option<usize>,
    len: usize,
    height: usize,
) -> usize {
    let offset = offset.min(len.saturating_sub(height));
    match selected.filter(|_| height > 0) {
        Some(selected) => offset
            .min(selected)
            .max((selected + 1).saturating_sub(height)),
        None => offset,
    }
}

/// A one-row-per-item list in `block` (drawn over `area`) whose viewport keeps
/// `selected` visible. `offset` is the first visible row, kept across draws
/// (see [`scroll_into_view`]). The bottom border shows the `37/142` position;
/// `▲` on the top border and `▼` on the bottom border mark hidden rows.
pub fn scrolling_list(
    block: Block<'static>,
    area: Rect,
    theme: Theme,
    len: usize,
    selected: Option<usize>,
    offset: &mut usize,
    row: impl Fn(usize) -> Line<'static>,
) -> Paragraph<'static> {
    let height = usize::from(block.inner(area).height);
    *offset = scroll_into_view(*offset, selected, len, height);
    let end = (*offset + height).min(len);
    let mut block = block;
    if *offset > 0 {
        block = block.title_top(Line::from(Span::styled(" ▲ ", theme.dim())).right_aligned());
    }
    let mut position = selected.map_or_else(String::new, |index| format!(" {}/{len}", index + 1));
    if end < len {
        position.push_str(" ▼");
    }
    if !position.is_empty() {
        position.push(' ');
        block = block.title_bottom(Line::from(Span::styled(position, theme.dim())).right_aligned());
    }
    Paragraph::new((*offset..end).map(row).collect::<Vec<_>>()).block(block)
}
