//! Attention signals for long waits (issue #76 U3): pure edge detection on
//! the Mission Run status and the Host-derived phase, plus the window title.
//!
//! The app only produces [`AttentionEvent`] values; `main.rs` turns them into
//! terminal writes (bell, desktop notification) outside drawing.
//!
//! Each [`AttentionKind`] fires at most once per Mission Run (key: run id +
//! kind). A condition that is already true the first time this console sees
//! it on a run it did not activate in this process (owner recovery after a
//! restart) is history, not news: it is recorded as fired without an event,
//! so a restart never replays a bell.

use std::collections::HashSet;

use crate::host::{RunPhase, RunRecord};

/// Run status the Host reports while a Human Decision Request is open.
pub const AWAITING_HUMAN_DECISION: &str = "awaiting_human_decision";
/// Terminal classification and terminal-detail kind of a stack failure.
const STACK_FAILED: &str = "stack_failed";

/// One attention-worthy transition.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AttentionKind {
    /// The phase `stack` step turned `done`: services are ready and the
    /// agents start planning.
    StackReady,
    /// The phase `stack` step failed, or the run ended as `stack_failed`.
    StackFailed,
    /// Run status `awaiting_human_decision`.
    AwaitingHumanDecision,
    /// Terminal run status (`succeeded`, `failed`, `cancelled`).
    Terminal,
}

impl AttentionKind {
    /// Stable key, combined with the run id to fire once per run.
    pub fn key(self) -> &'static str {
        match self {
            Self::StackReady => "stack_ready",
            Self::StackFailed => "stack_failed",
            Self::AwaitingHumanDecision => "awaiting_human_decision",
            Self::Terminal => "terminal",
        }
    }
}

/// A fired attention signal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttentionEvent {
    pub mission_run_id: String,
    pub kind: AttentionKind,
    /// One-line, sanitized-by-caller notification body.
    pub message: String,
}

/// Fired keys and first-sight bookkeeping for the current Mission Run.
#[derive(Debug, Default)]
pub struct AttentionTracker {
    mission_run_id: Option<String>,
    /// `true` for a run this console did not activate in this process:
    /// conditions already true at first sight are suppressed.
    replay: bool,
    /// Kinds whose condition has been observed at least once.
    seen: HashSet<AttentionKind>,
    /// Kinds already fired (or suppressed as history) for this run.
    fired: HashSet<AttentionKind>,
}

impl AttentionTracker {
    /// Track a run this console just activated: every transition is new.
    pub fn activated(&mut self, mission_run_id: &str) {
        self.reset(mission_run_id, false);
    }

    fn reset(&mut self, mission_run_id: &str, replay: bool) {
        self.mission_run_id = Some(mission_run_id.to_string());
        self.replay = replay;
        self.seen.clear();
        self.fired.clear();
    }

    /// Observe the latest run record and overview phase; return the events
    /// this observation newly proves. A run id never seen before (owner
    /// recovery) starts in replay mode.
    pub fn observe(&mut self, run: &RunRecord, phase: Option<&RunPhase>) -> Vec<AttentionEvent> {
        if self.mission_run_id.as_deref() != Some(run.mission_run_id.as_str()) {
            self.reset(&run.mission_run_id, true);
        }
        let terminal = run.is_terminal();
        let stack_step = phase.and_then(|phase| phase.steps.iter().find(|step| step.id == "stack"));
        let ended_as_stack_failure = run.terminal_classification.as_deref() == Some(STACK_FAILED)
            || run
                .terminal_detail
                .as_ref()
                .is_some_and(|detail| detail.kind == STACK_FAILED);
        // `None`: the condition cannot be judged yet (no phase received).
        let signals = [
            (
                AttentionKind::StackReady,
                stack_step.map(|step| step.status == "done"),
            ),
            (
                AttentionKind::StackFailed,
                if ended_as_stack_failure {
                    Some(true)
                } else {
                    stack_step.map(|step| step.status == "failed")
                },
            ),
            (
                AttentionKind::AwaitingHumanDecision,
                Some(run.status == AWAITING_HUMAN_DECISION),
            ),
            (AttentionKind::Terminal, Some(terminal)),
        ];
        let mut fired = Vec::new();
        for (kind, signal) in signals {
            let Some(active) = signal else { continue };
            let first_sight = self.seen.insert(kind);
            if active && self.fired.insert(kind) && !(self.replay && first_sight) {
                fired.push(kind);
            }
        }
        // Once the run is terminal only the terminal event is news; it also
        // carries a stack failure (`ONR ✖ stack_failed`), so no second event.
        if terminal {
            fired.retain(|kind| *kind == AttentionKind::Terminal);
        }
        fired
            .into_iter()
            .map(|kind| AttentionEvent {
                mission_run_id: run.mission_run_id.clone(),
                kind,
                message: event_message(kind, run, stack_step.and_then(|s| s.detail.as_deref())),
            })
            .collect()
    }
}

fn event_message(kind: AttentionKind, run: &RunRecord, stack_detail: Option<&str>) -> String {
    match kind {
        AttentionKind::StackReady => "Stack ready · planning started".to_string(),
        AttentionKind::StackFailed => match stack_detail {
            Some(detail) => format!("Stack failed · {detail}"),
            None => "Stack failed".to_string(),
        },
        AttentionKind::AwaitingHumanDecision => "Awaiting a Human Decision".to_string(),
        AttentionKind::Terminal => terminal_label(run),
    }
}

/// `✔ succeeded`, `✖ stack_failed`, `✖ cancelled_by_owner`: the terminal
/// mark plus the classification when the Host recorded one.
pub fn terminal_label(run: &RunRecord) -> String {
    let mark = if run.status == "succeeded" {
        "✔"
    } else {
        "✖"
    };
    let label = match run.terminal_classification.as_deref() {
        Some(classification) if run.status != "succeeded" => classification,
        _ => run.status.as_str(),
    };
    format!("{mark} {label}")
}

/// Terminal/tmux window title: a status display that tracks the run.
///
/// - no run: `ONR`
/// - active: `ONR ● running 00:12:04 · Planning` (elapsed and the current
///   phase label when known)
/// - terminal: `ONR ✔ succeeded` / `ONR ✖ stack_failed`
pub fn window_title(run: Option<&RunRecord>, phase: Option<&RunPhase>, now_unix: i64) -> String {
    let Some(run) = run else {
        return "ONR".to_string();
    };
    if run.is_terminal() {
        return format!("ONR {}", terminal_label(run));
    }
    let mut title = format!("ONR ● {}", run.status);
    if let Some(elapsed) = super::run::run_elapsed_seconds(run, now_unix) {
        title.push(' ');
        title.push_str(&crate::ui::layout::clock_duration(elapsed));
    }
    if let Some(step) =
        phase.and_then(|phase| phase.steps.iter().find(|step| step.id == phase.current))
    {
        title.push_str(" · ");
        title.push_str(&step.label);
    }
    title
}
