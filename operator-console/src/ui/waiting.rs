//! Waiting banner: one row under the phase stepper while a running Mission
//! Run waits on something that otherwise looks frozen - a prep step, a stack
//! service that has not passed readiness, a live agent call, or (v1.5)
//! teardown: `Stopping airsim-engine (3/3) · 0:06 / grace 0:30`, where the
//! grace is the Stack Supervisor's SIGTERM-to-SIGKILL period for that
//! service.
//!
//! The spinner advances with the console clock, not with Host data, so a
//! moving spinner means the console is alive; Host liveness stays in the
//! header. Elapsed time is measured from the Host's `started_at` (or
//! `stop_requested_at`). A service's readiness time in the previous run of
//! the same preset is shown as history (`last run 0:27`), never as an
//! estimate. The tail names the `w` key, which opens what the banner names
//! ([`App::current_wait`]).

use ratatui::style::Style;
use ratatui::text::{Line, Span};

use super::layout::{SPINNER, seconds_between, short_duration, truncate};
use super::theme::{Importance, Theme};
use crate::app::{App, CurrentWait, RunTab};
use crate::host::{OperatorAgentInvocation, OperatorStack};

/// Share of the readiness budget after which the timer turns to warning.
const WARN_FRACTION: f64 = 0.8;

/// What the run is waiting for, as rendered.
struct Wait {
    text: String,
    elapsed: Option<i64>,
    budget: Option<i64>,
    /// Prefix of the budget (`grace ` for a stopping service).
    budget_label: &'static str,
    /// Measured readiness in the previous run of the same preset.
    last_run: Option<i64>,
}

fn seconds(number: Option<&serde_json::Number>) -> Option<i64> {
    number
        .and_then(serde_json::Number::as_f64)
        .map(|seconds| seconds.round() as i64)
}

/// The banner, or `None` when the run is not waiting on anything visible.
pub fn waiting_banner(app: &App, theme: Theme, width: u16) -> Option<Line<'static>> {
    let current = app.current_wait()?;
    let wait = describe(current, app.unix_now());

    let late = matches!(
        (wait.elapsed, wait.budget),
        (Some(elapsed), Some(budget)) if budget > 0 && elapsed as f64 >= budget as f64 * WARN_FRACTION
    );
    // A late timer carries the warning glyph so it reads without colour.
    let timer = wait.elapsed.map(|elapsed| {
        let timer = match wait.budget {
            Some(budget) => format!(
                "{} / {}{}",
                short_duration(elapsed),
                wait.budget_label,
                short_duration(budget)
            ),
            None => short_duration(elapsed),
        };
        if late {
            format!("{} {timer}", Importance::Warning.glyph())
        } else {
            timer
        }
    });
    let history = wait
        .last_run
        .map(|seconds| format!(" · last run {}", short_duration(seconds)));
    // From the presentation layout `w` always opens the wait's tab.
    let shown_tab = (!app.presenting()).then_some(app.view.tab);
    let tail =
        inspect_hint(current, shown_tab).map_or_else(String::new, |hint| format!(" · {hint}"));
    let spinner = SPINNER[app.spinner_frame() % SPINNER.len()];
    let fixed = 3
        + timer.as_ref().map_or(0, |timer| timer.chars().count() + 3)
        + history
            .as_ref()
            .map_or(0, |history| history.chars().count())
        + tail.chars().count();
    let text = truncate(&wait.text, usize::from(width).saturating_sub(fixed));

    let mut spans = vec![
        Span::styled(format!(" {spinner} "), theme.hint()),
        Span::styled(text, theme.title()),
    ];
    if let Some(timer) = timer {
        spans.push(Span::styled(" · ", theme.dim()));
        spans.push(Span::styled(
            timer,
            if late {
                theme.importance(Importance::Warning)
            } else {
                Style::default()
            },
        ));
    }
    if let Some(history) = history {
        spans.push(Span::styled(history, theme.dim()));
    }
    spans.push(Span::styled(tail, theme.dim()));
    Some(Line::from(spans))
}

/// What `w` does from the shown tab (`None`: the presentation layout): open
/// the wait's tab, or select its row there.
fn inspect_hint(wait: CurrentWait<'_>, tab: Option<RunTab>) -> Option<&'static str> {
    let on_tab = tab == Some(wait.tab());
    Some(match wait {
        CurrentWait::Stack(_) | CurrentWait::Teardown(_) | CurrentWait::Cancelling if on_tab => {
            return None;
        }
        CurrentWait::Stack(_) | CurrentWait::Teardown(_) | CurrentWait::Cancelling => "w: 6 Stack",
        _ if on_tab => "w: select",
        CurrentWait::Invocation(_) => "w: 3 Agents",
        _ => "w: 6 Stack log",
    })
}

fn describe(wait: CurrentWait<'_>, now: i64) -> Wait {
    let plain = |text: String| Wait {
        text,
        elapsed: None,
        budget: None,
        budget_label: "",
        last_run: None,
    };
    match wait {
        CurrentWait::Stopping { service, position } => {
            let mut text = format!("Stopping {}", service.name);
            if let Some((position, total)) = position {
                text.push_str(&format!(" ({position}/{total})"));
            }
            Wait {
                text,
                elapsed: service
                    .stop_requested_at
                    .as_deref()
                    .and_then(|requested| seconds_between(requested, now)),
                budget: seconds(service.stop_grace_seconds.as_ref()),
                budget_label: "grace ",
                last_run: None,
            }
        }
        CurrentWait::Teardown(stack) => plain(teardown_text(stack)),
        CurrentWait::Cancelling => plain(
            "Cancellation requested · waiting for the Run Worker to begin teardown".to_string(),
        ),
        CurrentWait::Step(step) => Wait {
            text: format!("Running {} (stack preparation)", step.name),
            elapsed: seconds_between(&step.started_at, now),
            budget: seconds(Some(&step.timeout_seconds)),
            budget_label: "",
            last_run: None,
        },
        CurrentWait::Service {
            service,
            position,
            total,
        } => {
            let mut text = format!("Starting {} ({position}/{total})", service.name);
            if let Some(waiting_for) = service.waiting_for.as_deref() {
                text.push_str(&format!(" · waiting for {waiting_for}"));
            }
            Wait {
                text,
                elapsed: service
                    .started_at
                    .as_deref()
                    .and_then(|started| seconds_between(started, now)),
                budget: seconds(service.ready_timeout_seconds.as_ref()),
                budget_label: "",
                last_run: seconds(service.previous_ready_seconds.as_ref()),
            }
        }
        CurrentWait::Stack(stack) => plain(format!(
            "Starting the Environment Stack ({})",
            ready_count(stack)
        )),
        CurrentWait::Invocation(invocation) => Wait {
            text: agent_text(invocation),
            elapsed: invocation
                .started_at
                .as_deref()
                .and_then(|started| seconds_between(started, now)),
            budget: None,
            budget_label: "",
            last_run: None,
        },
    }
}

/// Between services, or once every service stopped while the Host has not
/// recorded the end yet.
fn teardown_text(stack: &OperatorStack) -> String {
    let Some(teardown) = stack.teardown.as_ref() else {
        return "Stopping the Environment Stack".to_string();
    };
    if teardown.finished() {
        return "Environment Stack stopped · waiting for the Host to record the end".to_string();
    }
    let order = &teardown.stop_order;
    let stopped = stack
        .services
        .iter()
        .filter(|service| {
            order.contains(&service.name)
                && matches!(service.state.as_str(), "stopped" | "exited" | "failed")
        })
        .count();
    format!(
        "Stopping the Environment Stack ({stopped}/{} stopped)",
        order.len()
    )
}

fn ready_count(stack: &OperatorStack) -> String {
    let ready = stack
        .services
        .iter()
        .filter(|service| service.state == "ready")
        .count();
    format!("{ready}/{} ready", stack.services.len())
}

fn agent_text(invocation: &OperatorAgentInvocation) -> String {
    match invocation.kind.as_str() {
        "llm" => format!(
            "Waiting for the LLM · {} · {}",
            invocation.role, invocation.name
        ),
        "tool" => format!("{} running tool {}", invocation.role, invocation.name),
        _ => format!("{} running {}", invocation.role, invocation.name),
    }
}
