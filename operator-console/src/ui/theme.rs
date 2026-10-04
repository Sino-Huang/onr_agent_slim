//! Every console style lives here (§4.2). Each state pairs a glyph with a
//! colour so it stays readable without colour; `NO_COLOR` disables colour
//! but keeps glyphs and emphasis.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;

/// Importance Level (mapping v1, owned by the Runtime Host).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Importance {
    Critical,
    Warning,
    Notable,
    Routine,
    Debug,
}

impl Importance {
    pub const ALL: [Self; 5] = [
        Self::Critical,
        Self::Warning,
        Self::Notable,
        Self::Routine,
        Self::Debug,
    ];

    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "critical" => Self::Critical,
            "warning" => Self::Warning,
            "notable" => Self::Notable,
            "routine" => Self::Routine,
            "debug" => Self::Debug,
            _ => return None,
        })
    }

    pub fn glyph(self) -> &'static str {
        match self {
            Self::Critical => "✖",
            Self::Warning => "▲",
            Self::Notable => "●",
            Self::Routine => "·",
            Self::Debug => "∙",
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Critical => "critical",
            Self::Warning => "warning",
            Self::Notable => "notable",
            Self::Routine => "routine",
            Self::Debug => "debug",
        }
    }
}

/// Component badges shown next to evidence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Badge {
    Hyp,
    Man,
    Cc,
    Fsm,
    Bel,
    Env,
    Per,
    Stk,
}

impl Badge {
    pub fn label(self) -> &'static str {
        match self {
            Self::Hyp => "HYP",
            Self::Man => "MAN",
            Self::Cc => "CC",
            Self::Fsm => "FSM",
            Self::Bel => "BEL",
            Self::Env => "ENV",
            Self::Per => "PER",
            Self::Stk => "STK",
        }
    }

    fn color(self) -> Color {
        match self {
            Self::Hyp => Color::Magenta,
            Self::Man => Color::Cyan,
            Self::Cc => Color::Blue,
            Self::Fsm => Color::LightBlue,
            Self::Bel => Color::Yellow,
            Self::Env => Color::Green,
            Self::Per => Color::LightGreen,
            Self::Stk => Color::Gray,
        }
    }

    /// Badge for an evidence source, mirroring the Host's
    /// `importance.component_badge(source, event_kind)`.
    pub fn for_source(source: &str, event_kind: Option<&str>) -> Self {
        match source {
            "hyper-agent" | "planning-command-handler" => Self::Hyp,
            "maneuver-control" => Self::Man,
            "context-coordination" => Self::Cc,
            "fsm-runner" => Self::Fsm,
            "environment" | "physical-runtime" => Self::Env,
            "runtime-host" | "stack" => Self::Stk,
            "runtime" if event_kind == Some("planning-environment-data") => Self::Env,
            "runtime" => Self::Stk,
            other if other.contains("belief") => Self::Bel,
            other if other.contains("perception") => Self::Per,
            _ => Self::Stk,
        }
    }
}

/// Console palette, with colour optionally disabled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    color: bool,
}

impl Theme {
    /// Colour unless `NO_COLOR` is set to a non-empty value.
    pub fn from_env() -> Self {
        let no_color = std::env::var_os("NO_COLOR").is_some_and(|value| !value.is_empty());
        Self { color: !no_color }
    }

    pub fn new(color: bool) -> Self {
        Self { color }
    }

    pub fn has_color(self) -> bool {
        self.color
    }

    fn fg(self, color: Color) -> Style {
        if self.color {
            Style::default().fg(color)
        } else {
            Style::default()
        }
    }

    pub fn dim(self) -> Style {
        self.fg(Color::DarkGray)
    }

    pub fn hint(self) -> Style {
        self.fg(Color::Yellow)
    }

    pub fn good(self) -> Style {
        self.fg(Color::Green)
    }

    pub fn error(self) -> Style {
        self.fg(Color::Red).add_modifier(Modifier::BOLD)
    }

    pub fn title(self) -> Style {
        Style::default().add_modifier(Modifier::BOLD)
    }

    pub fn selected(self) -> Style {
        Style::default().add_modifier(Modifier::REVERSED)
    }

    pub fn active_tab(self) -> Style {
        if self.color {
            Style::default()
                .fg(Color::Black)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
        }
    }

    /// The `HISTORICAL` badge of a run opened read-only from the history.
    pub fn historical(self) -> Style {
        if self.color {
            Style::default()
                .fg(Color::Black)
                .bg(Color::Magenta)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
        }
    }

    /// Non-authoritative LLM text: italic.
    pub fn ai(self) -> Style {
        Style::default().add_modifier(Modifier::ITALIC)
    }

    pub fn importance(self, importance: Importance) -> Style {
        match importance {
            Importance::Critical => self.error(),
            Importance::Warning => self.hint(),
            Importance::Notable => self.good(),
            Importance::Routine => Style::default(),
            Importance::Debug => self.dim(),
        }
    }

    /// `glyph name` span pair for an importance level.
    pub fn importance_mark(self, importance: Importance) -> Span<'static> {
        Span::styled(importance.glyph(), self.importance(importance))
    }

    pub fn badge(self, badge: Badge) -> Span<'static> {
        Span::styled(
            badge.label(),
            self.fg(badge.color()).add_modifier(Modifier::BOLD),
        )
    }

    pub fn ai_badge(self) -> Span<'static> {
        Span::styled("AI", self.fg(Color::Magenta).add_modifier(Modifier::ITALIC))
    }

    /// Colour for a Mission Run lifecycle status.
    pub fn run_status(self, status: &str) -> Style {
        match status {
            "queued" => self.fg(Color::Cyan),
            "running" | "succeeded" => self.good(),
            "awaiting_human_decision" => self.fg(Color::Magenta),
            "failed" => self.fg(Color::Red),
            "cancelled" => self.hint(),
            _ => Style::default(),
        }
    }

    /// Preflight check status mark.
    pub fn check_mark(self, status: &str) -> Span<'static> {
        match status {
            "pass" => Span::styled("✔", self.good()),
            "warn" => Span::styled("▲", self.hint()),
            "fail" => Span::styled("✖", self.error()),
            _ => Span::styled("?", self.dim()),
        }
    }

    /// Stack service or prep-step state mark.
    pub fn service_mark(self, state: &str) -> Span<'static> {
        match state {
            "ready" => Span::styled("●", self.good()),
            "done" => Span::styled("✔", self.good()),
            "starting" | "running" => Span::styled("◐", self.hint()),
            "stopping" => Span::styled("◑", self.hint()),
            "pending" => Span::styled("○", self.dim()),
            "failed" => Span::styled("✖", self.error()),
            "exited" => Span::styled("■", Style::default()),
            "stopped" => Span::styled("■", self.dim()),
            _ => Span::styled("?", self.dim()),
        }
    }

    /// Phase stepper step mark.
    pub fn step_mark(self, status: &str) -> Span<'static> {
        match status {
            "done" => Span::styled("✔", self.good()),
            "active" => Span::styled("▶", self.fg(Color::Cyan).add_modifier(Modifier::BOLD)),
            "failed" => Span::styled("✖", self.error()),
            _ => Span::styled("○", self.dim()),
        }
    }

    /// Run status dot for the header strip.
    pub fn status_dot(self, status: &str) -> Span<'static> {
        Span::styled("●", self.run_status(status))
    }
}

#[cfg(test)]
mod tests {
    use super::{Badge, Importance, Theme};
    use ratatui::style::Style;

    #[test]
    fn no_color_keeps_glyphs_but_drops_colour() {
        let plain = Theme::new(false);
        for importance in Importance::ALL {
            assert_eq!(plain.importance(importance).fg, None);
            assert_eq!(
                plain.importance_mark(importance).content,
                importance.glyph()
            );
        }
        assert_eq!(plain.check_mark("fail").content, "✖");
        assert_eq!(plain.badge(Badge::Hyp).style.fg, None);
        assert_ne!(
            Theme::new(true).importance(Importance::Critical),
            Style::default()
        );
    }

    #[test]
    fn badges_follow_the_host_component_mapping() {
        assert_eq!(Badge::for_source("hyper-agent", None), Badge::Hyp);
        assert_eq!(
            Badge::for_source("planning-command-handler", None),
            Badge::Hyp
        );
        assert_eq!(
            Badge::for_source("bayesian-belief-service", None),
            Badge::Bel
        );
        assert_eq!(Badge::for_source("sukai-perception", None), Badge::Per);
        assert_eq!(
            Badge::for_source("runtime", Some("planning-environment-data")),
            Badge::Env
        );
        assert_eq!(Badge::for_source("runtime", Some("other")), Badge::Stk);
        assert_eq!(Badge::for_source("unknown", None), Badge::Stk);
    }
}
