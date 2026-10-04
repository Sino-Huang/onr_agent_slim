//! Failure landing card for infrastructure failures (issue #76 U7).
//!
//! A run that ended `stack_failed`, `worker_failed` or `host_interrupted`
//! gets a card with what the Host recorded: stage, sanitized reason, failed
//! service, last completed phase step and cleanup state, plus the log the
//! operator should read first. Everything here is derived from Host data
//! already on screen (run record, overview, stack section); nothing is
//! guessed. The last log line is context, never presented as the cause.
//!
//! `y` copies the run id and Run Root through OSC 52; the app only queues the
//! text, `main.rs` writes [`osc52_sequence`] to the terminal outside drawing.

use super::App;
use super::run::{ArtifactInspector, CONTENT_PAGE_BYTES};
use crate::host::{
    ContentPurpose, HostCommand, OperatorOverview, OperatorStack, RunRecord, TerminalDetail,
};

/// Terminal classifications that get the failure card. Mission rejection
/// has its own card.
pub const INFRASTRUCTURE_FAILURES: [&str; 3] =
    ["stack_failed", "worker_failed", "host_interrupted"];

/// The Run Worker log Artifact: every Host serves it for a run.
pub const WORKER_LOG: &str = "worker-log";

/// Service states that mean a process may still be running.
const RUNNING_STATES: [&str; 3] = ["starting", "ready", "stopping"];

/// Whether the Environment Stack is torn down, as far as the Host's stack
/// status shows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Cleanup {
    /// The stack section has not arrived yet.
    Checking,
    /// The stack section lists no services.
    NoServices,
    /// No service is `starting`, `ready` or `stopping`.
    Complete,
    /// These services are still recorded as running.
    Running(Vec<String>),
}

impl Cleanup {
    /// `e` may start a new Mission Intent: nothing is recorded as running.
    pub fn allows_new_intent(&self) -> bool {
        matches!(self, Self::Complete | Self::NoServices)
    }

    pub fn describe(&self) -> String {
        match self {
            Self::Checking => "checking the stack status…".to_string(),
            Self::NoServices => "no stack services recorded".to_string(),
            Self::Complete => "✔ every stack service stopped".to_string(),
            Self::Running(names) => format!(
                "▲ not confirmed: {} still recorded as running",
                names.join(", ")
            ),
        }
    }
}

/// Which log `l` opens and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailureLog {
    pub artifact_id: String,
    /// `true` when the Host named it (`terminal_detail.log_artifact_id`,
    /// v1.5); otherwise it comes from the stack status or is the worker log.
    pub named_by_host: bool,
}

/// Everything the failure card shows, derived from Host data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailureCard {
    pub classification: String,
    pub headline: String,
    pub stage: String,
    pub reason: String,
    /// Failed service and where the name came from.
    pub failed_service: Option<(String, &'static str)>,
    pub last_done_step: String,
    pub cleanup: Cleanup,
    pub log: FailureLog,
    /// Last line of that log as the stack status recorded it: context only.
    pub last_line: Option<String>,
    pub mission_run_id: String,
    /// Absent from Hosts older than API v1.5.
    pub run_root: Option<String>,
}

impl FailureCard {
    /// The card for a terminal infrastructure failure, or `None`.
    pub fn derive(
        run: &RunRecord,
        overview: Option<&OperatorOverview>,
        stack: Option<&OperatorStack>,
    ) -> Option<Self> {
        if !run.is_terminal() {
            return None;
        }
        let classification = run.terminal_classification.as_deref()?;
        if !INFRASTRUCTURE_FAILURES.contains(&classification) {
            return None;
        }
        // A detail recorded for another outcome (a rejection published before
        // a Host restart) does not describe this failure.
        let detail = run
            .terminal_detail
            .as_ref()
            .filter(|detail| detail.kind == classification);
        let phase = overview.and_then(|overview| overview.phase.as_ref());
        let failed_step = phase.and_then(|phase| {
            phase
                .steps
                .iter()
                .find(|step| step.status == "failed" && step.id != "terminal")
        });
        let detail_stage = detail.and_then(|detail| detail.stage.as_deref());
        let stage = match (failed_step, detail_stage) {
            (Some(step), Some(stage)) => format!("{} ({stage})", step.label),
            (Some(step), None) => step.label.clone(),
            (None, Some(stage)) => stage.to_string(),
            (None, None) if phase.is_none() => "phase not loaded yet".to_string(),
            (None, None) => "not recorded".to_string(),
        };
        let last_done_step = match phase {
            None => "phase not loaded yet".to_string(),
            Some(phase) => phase
                .steps
                .iter()
                .rev()
                .find(|step| step.status == "done")
                .map_or_else(|| "none".to_string(), |step| step.label.clone()),
        };
        let failed_service = detail
            .and_then(|detail| detail.service.clone())
            .map(|name| (name, "terminal detail"))
            .or_else(|| {
                let names: Vec<&str> = stack?
                    .services
                    .iter()
                    .filter(|service| service.state == "failed")
                    .map(|service| service.name.as_str())
                    .collect();
                (!names.is_empty()).then(|| (names.join(", "), "stack status"))
            });
        let log = failure_log(classification, detail, stack);
        let last_line = stack.and_then(|stack| {
            stack
                .services
                .iter()
                .find(|service| {
                    service.log_artifact_id.as_deref() == Some(log.artifact_id.as_str())
                })
                .and_then(|service| service.last_line.clone())
        });
        let cleanup = match stack {
            None => Cleanup::Checking,
            Some(stack) if stack.services.is_empty() => Cleanup::NoServices,
            Some(stack) => {
                let running: Vec<String> = stack
                    .services
                    .iter()
                    .filter(|service| RUNNING_STATES.contains(&service.state.as_str()))
                    .map(|service| service.name.clone())
                    .collect();
                if running.is_empty() {
                    Cleanup::Complete
                } else {
                    Cleanup::Running(running)
                }
            }
        };
        Some(Self {
            classification: classification.to_string(),
            headline: headline(classification, detail),
            stage,
            reason: reason(classification, detail),
            failed_service,
            last_done_step,
            cleanup,
            log,
            last_line,
            mission_run_id: run.mission_run_id.clone(),
            run_root: overview.and_then(|overview| overview.run_root.clone()),
        })
    }

    /// The text `y` copies: run id, then the Run Root when the Host reports it.
    pub fn clipboard_text(&self) -> String {
        match self.run_root.as_deref() {
            Some(root) => format!("{} {root}", self.mission_run_id),
            None => self.mission_run_id.clone(),
        }
    }
}

fn headline(classification: &str, detail: Option<&TerminalDetail>) -> String {
    match classification {
        "stack_failed" => match detail.and_then(|detail| detail.service.as_deref()) {
            Some(service) => format!("Environment Stack failed: {service}"),
            None => "Environment Stack failed".to_string(),
        },
        "worker_failed" => "Run Worker failed".to_string(),
        _ => "Runtime Host interrupted the run".to_string(),
    }
}

fn reason(classification: &str, detail: Option<&TerminalDetail>) -> String {
    let message = detail.and_then(|detail| detail.message.as_deref());
    match (classification, message) {
        ("worker_failed", Some(message)) => {
            match detail.and_then(|detail| detail.error_type.as_deref()) {
                Some(kind) => format!("{kind}: {message}"),
                None => message.to_string(),
            }
        }
        (_, Some(message)) => message.to_string(),
        ("host_interrupted", None) => {
            "The Runtime Host was interrupted while this run was active; it recorded no failure message"
                .to_string()
        }
        (_, None) => "The Host recorded no failure message".to_string(),
    }
}

/// The Host-named log; for an older Host, the failed service's or prep
/// step's log from the stack status, else the Run Worker log.
fn failure_log(
    classification: &str,
    detail: Option<&TerminalDetail>,
    stack: Option<&OperatorStack>,
) -> FailureLog {
    if let Some(artifact_id) = detail.and_then(|detail| detail.log_artifact_id.clone()) {
        return FailureLog {
            artifact_id,
            named_by_host: true,
        };
    }
    let from_stack = (classification == "stack_failed")
        .then(|| detail.and_then(|detail| detail.service.as_deref()))
        .flatten()
        .zip(stack)
        .and_then(|(name, stack)| {
            stack
                .services
                .iter()
                .find(|service| service.name == name)
                .and_then(|service| service.log_artifact_id.clone())
                .or_else(|| {
                    stack
                        .steps
                        .iter()
                        .find(|step| step.name == name)
                        .map(|step| step.log_artifact_id.clone())
                })
        });
    FailureLog {
        artifact_id: from_stack.unwrap_or_else(|| WORKER_LOG.to_string()),
        named_by_host: false,
    }
}

impl App {
    /// Whether the current run ended in an infrastructure failure.
    pub fn failure_classified(&self) -> bool {
        self.run.as_ref().is_some_and(|run| {
            run.is_terminal()
                && run
                    .terminal_classification
                    .as_deref()
                    .is_some_and(|kind| INFRASTRUCTURE_FAILURES.contains(&kind))
        })
    }

    /// The failure card's content, while the run is an infrastructure failure.
    pub fn failure_card(&self) -> Option<FailureCard> {
        FailureCard::derive(
            self.run.as_ref()?,
            self.view.overview.as_ref(),
            self.view.stack.stack.as_ref(),
        )
    }

    /// Whether the failure card is shown (until Enter or a tab switch).
    pub fn failure_open(&self) -> bool {
        !self.view.failure_dismissed && self.failure_classified()
    }

    /// Why `e` may not start a new Mission Intent yet: an infrastructure
    /// failure whose stack status still shows services running, or has not
    /// arrived.
    pub(crate) fn new_intent_blocked(&self) -> Option<String> {
        let card = self.failure_card()?;
        match card.cleanup {
            Cleanup::Checking => Some(
                "New intent after cleanup: waiting for the stack status from the Host".to_string(),
            ),
            Cleanup::Running(names) => Some(format!(
                "New intent after cleanup: {} still recorded as running (6 Stack)",
                names.join(", ")
            )),
            Cleanup::Complete | Cleanup::NoServices => None,
        }
    }

    /// `l`: open the failure's log in the Artifact inspector at its tail.
    pub(crate) fn open_failure_log(&mut self) {
        let Some(card) = self.failure_card() else {
            return;
        };
        let artifact_id = card.log.artifact_id;
        if !card.log.named_by_host {
            self.hint = Some(format!(
                "This Host names no failure log (API < 1.5): showing {artifact_id}"
            ));
        }
        let classification = self
            .view
            .artifacts
            .iter()
            .find(|artifact| artifact.artifact_id == artifact_id)
            .map_or_else(
                || "service_log".to_string(),
                |artifact| artifact.classification.clone(),
            );
        self.view.inspector = Some(ArtifactInspector {
            artifact_id: artifact_id.clone(),
            classification,
            offset: 0,
            previous_offsets: Vec::new(),
            page: None,
            scroll: 0,
            rows: 0,
            max_scroll: 0,
            tail: true,
        });
        self.outbox.push(HostCommand::FetchArtifactContent {
            purpose: ContentPurpose::Inspector,
            mission_run_id: card.mission_run_id,
            artifact_id,
            offset: 0,
            limit: CONTENT_PAGE_BYTES,
        });
    }

    /// `y`: queue the run id and Run Root for an OSC 52 clipboard write.
    pub(crate) fn copy_run_identity(&mut self) {
        let Some(card) = self.failure_card() else {
            return;
        };
        let text = card.clipboard_text();
        let missing_root = if card.run_root.is_none() {
            " · this Host reports no run root (API < 1.5)"
        } else {
            ""
        };
        self.hint = Some(format!(
            "Sent to the terminal clipboard (OSC 52; the terminal decides): {text}{missing_root}"
        ));
        self.clipboard = Some(text);
    }
}

/// OSC 52 "set clipboard" for `text`. Inside tmux the plain sequence reaches
/// tmux itself (stored as a paste buffer with `set-clipboard on`) and a DCS
/// passthrough copy reaches the outer terminal (tmux 3.3+ only with
/// `allow-passthrough on`). Whether the terminal accepts it is its decision.
pub fn osc52_sequence(text: &str, tmux: bool) -> String {
    let sequence = format!("\x1b]52;c;{}\x07", base64(text.as_bytes()));
    if tmux {
        format!(
            "{sequence}\x1bPtmux;{}\x1b\\",
            sequence.replace('\x1b', "\x1b\x1b")
        )
    } else {
        sequence
    }
}

fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let triple = chunk.iter().enumerate().fold(0u32, |acc, (index, byte)| {
            acc | u32::from(*byte) << (16 - 8 * index)
        });
        for index in 0..4 {
            if index <= chunk.len() {
                out.push(char::from(
                    ALPHABET[(triple >> (18 - 6 * index) & 0x3f) as usize],
                ));
            } else {
                out.push('=');
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::{base64, osc52_sequence};

    #[test]
    fn base64_matches_rfc_4648_vectors() {
        for (input, expected) in [
            ("", ""),
            ("f", "Zg=="),
            ("fo", "Zm8="),
            ("foo", "Zm9v"),
            ("foob", "Zm9vYg=="),
            ("fooba", "Zm9vYmE="),
            ("foobar", "Zm9vYmFy"),
        ] {
            assert_eq!(base64(input.as_bytes()), expected);
        }
    }

    #[test]
    fn tmux_gets_the_plain_sequence_and_a_passthrough_copy() {
        assert_eq!(osc52_sequence("foo", false), "\x1b]52;c;Zm9v\x07");
        assert_eq!(
            osc52_sequence("foo", true),
            "\x1b]52;c;Zm9v\x07\x1bPtmux;\x1b\x1b]52;c;Zm9v\x07\x1b\\"
        );
    }
}
