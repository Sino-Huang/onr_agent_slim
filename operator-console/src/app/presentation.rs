//! F4 presentation layout (issue #76 U11): one large World source, one
//! evidence card, the mission title and both clocks, for a presenter who
//! keeps the audience on the scene.
//!
//! - The card shows the existing selections: the Progress selection (with
//!   the current phase step) as the milestone, the Belief/Context entity, or
//!   the Agents invocation. `v` cycles the kind; tabs never rotate on their own.
//! - `p` freezes the display: the frame, card, phase and clocks are held
//!   (`FROZEN AT <wall> · t=<mission>`) while every operator-view section keeps
//!   polling. The frame is held by the World tab's `p` pause (no frame fetch
//!   while frozen, and a frame already in flight is dropped); resuming
//!   restores the World pause state and shows the latest evidence.
//! - Failure, Human Decision and Host-offline alerts are derived live, never
//!   from the frozen copy, so they break through a frozen view.
//! - A historical run (F3) can be presented read-only; its alerts still come
//!   from the current run and the Host connection.

use crossterm::event::{KeyCode, KeyEvent};

use super::attention::{AWAITING_HUMAN_DECISION, terminal_label};
use super::run::run_elapsed_seconds;
use super::{App, Liveness, RunView};
use crate::host::{
    OperatorAgentInvocation, OperatorBeliefs, OperatorSection, OperatorWorld, ProgressNarrative,
    ProgressNode, RunPhase,
};

/// Sections the presentation layout reads; polled in place of the hidden
/// tab's sections while it is shown.
pub const SECTIONS: [OperatorSection; 5] = [
    OperatorSection::Overview,
    OperatorSection::Progress,
    OperatorSection::Beliefs,
    OperatorSection::Agents,
    OperatorSection::World,
];

/// Which evidence card the presentation shows; `v` cycles.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum EvidenceCard {
    /// The current phase step and the Progress selection.
    #[default]
    Milestone,
    /// The Belief/Context entity selection.
    Belief,
    /// The Agents invocation selection.
    Invocation,
}

impl EvidenceCard {
    pub fn label(self) -> &'static str {
        match self {
            Self::Milestone => "Milestone",
            Self::Belief => "Belief entity",
            Self::Invocation => "Agent invocation",
        }
    }

    pub fn next(self) -> Self {
        match self {
            Self::Milestone => Self::Belief,
            Self::Belief => Self::Invocation,
            Self::Invocation => Self::Milestone,
        }
    }
}

/// The Progress selection a milestone card shows.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Milestone<'a> {
    /// The Run Narrative root (AI, non-authoritative).
    Narrative(Option<&'a ProgressNarrative>),
    /// A summary or record, with its parent summary when it has one.
    Node {
        node: &'a ProgressNode,
        parent: Option<&'a ProgressNode>,
    },
    /// Nothing selected, or the live container.
    None,
}

/// What the presentation layout draws, borrowed from the live view or from
/// the frozen copy.
#[derive(Debug, Clone, Copy)]
pub struct Evidence<'a> {
    pub mission_time: Option<&'a serde_json::Number>,
    pub phase: Option<&'a RunPhase>,
    pub milestone: Milestone<'a>,
    /// Progress follows the newest record (else the operator pinned one).
    pub progress_following: bool,
    pub beliefs: Option<&'a OperatorBeliefs>,
    pub belief_entity: Option<&'a str>,
    pub invocation: Option<&'a OperatorAgentInvocation>,
    /// Agents follows the newest invocation.
    pub agents_following: bool,
    pub world: Option<&'a OperatorWorld>,
}

impl<'a> Evidence<'a> {
    fn live(view: &'a RunView) -> Self {
        let overview = view.overview.as_ref();
        let progress = &view.progress;
        let milestone = match progress.selected_id() {
            Some("root") => Milestone::Narrative(progress.narrative.as_ref()),
            Some(_) => match progress.selected_node() {
                Some(node) if node.level != "live" => Milestone::Node {
                    node,
                    parent: node
                        .parent_id
                        .as_deref()
                        .and_then(|parent| progress.nodes.get(parent))
                        .filter(|parent| parent.level == "summary"),
                },
                _ => Milestone::None,
            },
            None => Milestone::None,
        };
        Self {
            mission_time: overview
                .and_then(|overview| overview.environment.mission_time_seconds.as_ref()),
            phase: overview.and_then(|overview| overview.phase.as_ref()),
            milestone,
            progress_following: progress.follow_newest,
            beliefs: view.beliefs.as_ref(),
            belief_entity: view.selected_belief_entity.as_deref(),
            invocation: view.selected_invocation().map(|(_, invocation)| invocation),
            agents_following: view.agent_following,
            world: view.world.as_ref(),
        }
    }

    fn to_owned(self) -> OwnedEvidence {
        OwnedEvidence {
            mission_time: self.mission_time.cloned(),
            phase: self.phase.cloned(),
            milestone: match self.milestone {
                Milestone::Narrative(narrative) => OwnedMilestone::Narrative(narrative.cloned()),
                Milestone::Node { node, parent } => {
                    OwnedMilestone::Node(Box::new((node.clone(), parent.cloned())))
                }
                Milestone::None => OwnedMilestone::None,
            },
            progress_following: self.progress_following,
            beliefs: self.beliefs.cloned(),
            belief_entity: self.belief_entity.map(str::to_string),
            invocation: self.invocation.cloned(),
            agents_following: self.agents_following,
            world: self.world.cloned(),
        }
    }
}

#[derive(Debug)]
enum OwnedMilestone {
    Narrative(Option<ProgressNarrative>),
    /// The node and its parent summary.
    Node(Box<(ProgressNode, Option<ProgressNode>)>),
    None,
}

#[derive(Debug)]
struct OwnedEvidence {
    mission_time: Option<serde_json::Number>,
    phase: Option<RunPhase>,
    milestone: OwnedMilestone,
    progress_following: bool,
    beliefs: Option<OperatorBeliefs>,
    belief_entity: Option<String>,
    invocation: Option<OperatorAgentInvocation>,
    agents_following: bool,
    world: Option<OperatorWorld>,
}

impl OwnedEvidence {
    fn borrow(&self) -> Evidence<'_> {
        Evidence {
            mission_time: self.mission_time.as_ref(),
            phase: self.phase.as_ref(),
            milestone: match &self.milestone {
                OwnedMilestone::Narrative(narrative) => Milestone::Narrative(narrative.as_ref()),
                OwnedMilestone::Node(nodes) => Milestone::Node {
                    node: &nodes.0,
                    parent: nodes.1.as_ref(),
                },
                OwnedMilestone::None => Milestone::None,
            },
            progress_following: self.progress_following,
            beliefs: self.beliefs.as_ref(),
            belief_entity: self.belief_entity.as_deref(),
            invocation: self.invocation.as_ref(),
            agents_following: self.agents_following,
            world: self.world.as_ref(),
        }
    }
}

/// A frozen display: the wall clock and run elapsed at the freeze, and a
/// copy of the evidence then on screen.
#[derive(Debug)]
pub struct Frozen {
    /// Wall clock (Unix seconds) at the freeze.
    pub wall_unix: i64,
    /// Run wall time at the freeze.
    pub run_elapsed: Option<i64>,
    /// The World `p` pause state the freeze found; restored on resume.
    media_paused: bool,
    evidence: OwnedEvidence,
}

/// Presentation layout state of one run view.
#[derive(Debug, Default)]
pub struct Presentation {
    /// F4: the layout replaces the tabs (the tab stays selected behind it).
    pub active: bool,
    pub card: EvidenceCard,
    frozen: Option<Box<Frozen>>,
}

impl Presentation {
    pub fn frozen(&self) -> Option<&Frozen> {
        self.frozen.as_deref()
    }

    pub fn is_frozen(&self) -> bool {
        self.frozen.is_some()
    }
}

/// A live alert that breaks through the presentation layout, frozen or not.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Alert {
    /// The current run failed (terminal `failed`, the U7 failure card's and
    /// the U3 terminal event's source) or its stack step failed (U3
    /// `stack_failed`). `historical`: a historical run is on display.
    Failure {
        label: String,
        detail: Option<String>,
        historical: bool,
    },
    /// The current run awaits a Human Decision (U3 source: run status).
    HumanDecision { historical: bool },
    /// No successful Host response within the offline threshold.
    HostOffline,
    /// No successful Host response within the stale threshold.
    HostStale,
}

impl Alert {
    /// One-line alert text.
    pub fn text(&self) -> String {
        let subject = |historical: bool| {
            if historical { "CURRENT RUN" } else { "RUN" }
        };
        match self {
            Self::Failure {
                label,
                detail,
                historical,
            } => {
                let mut text = format!("✖ {} FAILED · {label}", subject(*historical));
                if let Some(detail) = detail {
                    text.push_str(" · ");
                    text.push_str(detail);
                }
                text
            }
            Self::HumanDecision { historical } => format!(
                "◆ HUMAN DECISION REQUIRED · {} paused, awaiting a Human Decision (status only)",
                if *historical {
                    "the current Mission Run is"
                } else {
                    "the Mission Run is"
                }
            ),
            Self::HostOffline => {
                "✖ HOST OFFLINE · no Host response; showing the last received evidence".to_string()
            }
            Self::HostStale => {
                "▲ HOST STALE · Host responses are late; showing the last received evidence"
                    .to_string()
            }
        }
    }
}

impl App {
    /// Whether the F4 presentation layout replaces the Run tabs.
    pub fn presenting(&self) -> bool {
        self.view.presentation.active
    }

    /// The evidence the presentation layout draws: the frozen copy while
    /// frozen, else the live view.
    pub fn presentation_evidence(&self) -> Evidence<'_> {
        match self.view.presentation.frozen() {
            Some(frozen) => frozen.evidence.borrow(),
            None => Evidence::live(&self.view),
        }
    }

    /// Alerts that break through the presentation layout: a failure or
    /// Human Decision of the current run (parked behind a historical one or
    /// displayed), then the Host connection. Always live.
    pub fn presentation_alerts(&self) -> Vec<Alert> {
        let historical = self.viewing_history();
        let mut alerts = Vec::new();
        if let Some(run) = self.current_run() {
            let stack_failed = self
                .current_view()
                .overview
                .as_ref()
                .and_then(|overview| overview.phase.as_ref())
                .is_some_and(|phase| {
                    phase
                        .steps
                        .iter()
                        .any(|step| step.id == "stack" && step.status == "failed")
                });
            if run.status == "failed" || stack_failed {
                let label = if run.is_terminal() {
                    terminal_label(run)
                        .trim_start_matches('✖')
                        .trim()
                        .to_string()
                } else {
                    "stack_failed".to_string()
                };
                alerts.push(Alert::Failure {
                    label,
                    detail: run.terminal_detail.as_ref().map(|detail| detail.summary()),
                    historical,
                });
            }
            if run.status == AWAITING_HUMAN_DECISION {
                alerts.push(Alert::HumanDecision { historical });
            }
        }
        match self.liveness() {
            Liveness::Offline => alerts.push(Alert::HostOffline),
            Liveness::Stale => alerts.push(Alert::HostStale),
            Liveness::Live | Liveness::Idle => {}
        }
        alerts
    }

    /// The displayed run's title: its preset's catalog title when the
    /// presets are loaded, else the preset id, else the mission id.
    pub fn mission_title(&self) -> Option<String> {
        let run = self.run.as_ref()?;
        let Some(stack) = run.stack.as_ref() else {
            return Some(run.mission_id.clone());
        };
        let title = self
            .launch
            .presets
            .as_ref()
            .and_then(|presets| {
                presets
                    .presets
                    .iter()
                    .find(|preset| preset.preset_id == stack.preset_id)
            })
            .map_or_else(|| stack.preset_id.clone(), |preset| preset.title.clone());
        Some(title)
    }

    /// F4 on the Run screen: show the presentation layout over the current
    /// tab, or end it and return to that tab.
    pub(crate) fn toggle_presentation(&mut self) {
        if self.presenting() {
            let tab = self.view.tab;
            // `select_tab` ends the presentation and re-requests the tab.
            self.select_tab(tab);
            return;
        }
        self.view.presentation.active = true;
        for section in SECTIONS {
            self.request_section(section);
        }
        self.request_visible_frame();
    }

    /// End the presentation (and any freeze); the selected tab shows again.
    pub(crate) fn end_presentation(&mut self) {
        self.unfreeze();
        self.view.presentation.active = false;
    }

    /// Keys of the presentation layout; returns whether the key was taken.
    pub(crate) fn handle_presentation_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::F(4) => self.toggle_presentation(),
            KeyCode::Char('v') => {
                let presentation = &mut self.view.presentation;
                presentation.card = presentation.card.next();
            }
            KeyCode::Char('p') => {
                if self.view.presentation.is_frozen() {
                    self.unfreeze();
                } else {
                    self.freeze();
                }
            }
            KeyCode::Char('s') => {
                if self.view.presentation.is_frozen() {
                    self.hint = Some("Frozen: p resumes before s changes the source".to_string());
                } else {
                    self.cycle_frame_source();
                }
            }
            _ => return false,
        }
        true
    }

    /// Hold the displayed evidence; the World `p` pause holds the frame.
    fn freeze(&mut self) {
        let wall_unix = self.unix_now();
        let run_elapsed = self
            .run
            .as_ref()
            .and_then(|run| run_elapsed_seconds(run, wall_unix));
        let evidence = Evidence::live(&self.view).to_owned();
        let view = &mut self.view;
        view.presentation.frozen = Some(Box::new(Frozen {
            wall_unix,
            run_elapsed,
            media_paused: view.media.paused,
            evidence,
        }));
        view.media.paused = true;
    }

    /// Show the latest evidence again and restore the World pause state.
    fn unfreeze(&mut self) {
        let Some(frozen) = self.view.presentation.frozen.take() else {
            return;
        };
        self.view.media.paused = frozen.media_paused;
        self.request_visible_frame();
    }
}
