//! Run screen: tabs, operator-view polling with per-section cursors and
//! ETags, section reducers, the Artifact inspector, and cancellation keys.

use crossterm::event::{KeyCode, KeyEvent};

use super::history::PollScope;
use super::presentation::{self, Presentation};
use super::progress::ProgressView;
use super::world::WorldMedia;
use super::{
    App, CancellationOrigin, CancellationState, ReceiptExportState, RefreshKey, StackView,
};
use crate::host::{
    ArtifactContentPage, ArtifactDescriptor, CancellationRequest, ContentPurpose,
    ConversationEntry, EvidencePage, Fetched, FrameSource, HostCommand, HostError,
    OperatorAgentInvocation, OperatorBeliefs, OperatorContext, OperatorEnvironment,
    OperatorOverview, OperatorSection, OperatorStack, OperatorTimelineEntry, OperatorViewPage,
    OperatorWorld, RunRecord, StackService, StackStep, WorldFrame,
};

/// Bytes per Artifact inspector page and service-log tail window.
pub const CONTENT_PAGE_BYTES: u64 = 4096;

/// Run screen tabs `1`-`7`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum RunTab {
    #[default]
    Overview,
    Progress,
    Agents,
    BeliefContext,
    World,
    Stack,
    Artifacts,
}

impl RunTab {
    pub const ALL: [Self; 7] = [
        Self::Overview,
        Self::Progress,
        Self::Agents,
        Self::BeliefContext,
        Self::World,
        Self::Stack,
        Self::Artifacts,
    ];

    pub fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|tab| *tab == self)
            .expect("ALL lists every tab")
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Overview => "Overview",
            Self::Progress => "Progress",
            Self::Agents => "Agents",
            Self::BeliefContext => "Belief/Context",
            Self::World => "World",
            Self::Stack => "Stack",
            Self::Artifacts => "Artifacts",
        }
    }

    /// Primary operator-view section for this tab. Compound tabs poll their
    /// additional sections through `sections`.
    pub fn section(self) -> Option<OperatorSection> {
        match self {
            Self::Overview => Some(OperatorSection::Overview),
            Self::Agents => Some(OperatorSection::Agents),
            Self::World => Some(OperatorSection::World),
            Self::Stack => Some(OperatorSection::Stack),
            Self::Artifacts => Some(OperatorSection::Artifacts),
            Self::Progress => Some(OperatorSection::Progress),
            Self::BeliefContext => Some(OperatorSection::Beliefs),
        }
    }

    pub fn sections(self) -> &'static [OperatorSection] {
        match self {
            Self::Overview => &[
                OperatorSection::Overview,
                OperatorSection::Progress,
                OperatorSection::Beliefs,
                OperatorSection::Context,
                OperatorSection::World,
            ],
            Self::Progress => &[OperatorSection::Progress],
            Self::Agents => &[OperatorSection::Agents],
            Self::BeliefContext => &[OperatorSection::Beliefs, OperatorSection::Context],
            Self::World => &[OperatorSection::World, OperatorSection::Environment],
            Self::Stack => &[OperatorSection::Stack],
            Self::Artifacts => &[OperatorSection::Artifacts],
        }
    }

    fn from_digit(c: char) -> Option<Self> {
        let index = c.to_digit(10)? as usize;
        index
            .checked_sub(1)
            .and_then(|index| Self::ALL.get(index).copied())
    }

    fn offset(self, delta: isize) -> Self {
        let len = Self::ALL.len() as isize;
        Self::ALL[(self.index() as isize + delta).rem_euclid(len) as usize]
    }
}

/// What a non-terminal Mission Run visibly waits on: the waiting banner's
/// subject and the `w` ("inspect current wait") target.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum CurrentWait<'a> {
    /// Teardown signalled this service (`stopping`, v1.5): 1-based
    /// `position` in `teardown.stop_order` of `total`, when listed there.
    Stopping {
        service: &'a StackService,
        position: Option<(usize, usize)>,
    },
    /// Teardown started and no service is `stopping` right now.
    Teardown(&'a OperatorStack),
    /// Cancellation was requested and the stack has not reported teardown.
    Cancelling,
    /// A prep step is running (`stack.step`).
    Step(&'a StackStep),
    /// The first `starting` service, at 1-based `position` of `total`.
    Service {
        service: &'a StackService,
        position: usize,
        total: usize,
    },
    /// The Stack phase is active but no prep step or service is starting.
    Stack(&'a OperatorStack),
    /// A live Hyper Agent or Maneuver Control call.
    Invocation(&'a OperatorAgentInvocation),
}

/// Where `w` navigates, captured at keypress so later Host updates cannot
/// redirect it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WaitTarget {
    /// A Stack tab row (prep step or service), by name.
    StackRow(String),
    /// The Stack tab, without a specific row.
    Stack,
    /// An Agents tab invocation, by stable ID.
    Invocation(String),
}

impl CurrentWait<'_> {
    pub fn target(self) -> WaitTarget {
        match self {
            Self::Step(step) => WaitTarget::StackRow(step.name.clone()),
            Self::Service { service, .. } | Self::Stopping { service, .. } => {
                WaitTarget::StackRow(service.name.clone())
            }
            Self::Stack(_) | Self::Teardown(_) | Self::Cancelling => WaitTarget::Stack,
            Self::Invocation(invocation) => WaitTarget::Invocation(invocation.stable_id.clone()),
        }
    }

    /// The tab `w` opens.
    pub fn tab(self) -> RunTab {
        match self {
            Self::Invocation(_) => RunTab::Agents,
            _ => RunTab::Stack,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactInspector {
    pub artifact_id: String,
    pub classification: String,
    pub offset: u64,
    pub previous_offsets: Vec<u64>,
    pub page: Option<ArtifactContentPage>,
    /// First visible wrapped line of the current byte page.
    pub scroll: usize,
    /// Wrapped-line viewport height at the last draw (PgUp/PgDn step).
    pub rows: usize,
    /// Largest useful `scroll` at the last draw (End target).
    pub max_scroll: usize,
    /// Opened at the log tail (failure card `l`): the first page reply moves
    /// to the last byte page, which then shows its last line.
    pub tail: bool,
}

impl ArtifactInspector {
    /// Scroll to `line`, within the bounds of the last draw.
    fn scroll_to(&mut self, line: usize) {
        self.scroll = line.min(self.max_scroll);
    }

    /// Scroll by `delta` wrapped lines from the visible position.
    fn scroll_by(&mut self, delta: isize) {
        let current = self.scroll.min(self.max_scroll);
        self.scroll_to(current.saturating_add_signed(delta));
    }
}

const SECTION_COUNT: usize = OperatorSection::ALL.len();

/// Run screen state for one Mission Run; replaced when the run changes.
#[derive(Debug)]
pub struct RunView {
    pub tab: RunTab,
    pub overview: Option<OperatorOverview>,
    pub progress: ProgressView,
    pub beliefs: Option<OperatorBeliefs>,
    /// Belief/Context selection, by stable entity ID across revisions.
    pub selected_belief_entity: Option<String>,
    pub context: Option<OperatorContext>,
    pub world: Option<OperatorWorld>,
    pub media: WorldMedia,
    pub rejection_dismissed: bool,
    /// Enter (or a tab switch) closed the infrastructure failure card.
    pub failure_dismissed: bool,
    pub agents: Vec<OperatorAgentInvocation>,
    pub selected_invocation: Option<String>,
    pub agent_following: bool,
    pub newer_invocations: usize,
    /// First visible row of the invocation list, kept across draws.
    pub agent_list_offset: usize,
    pub agent_detail_scroll: u16,
    pub environment: Option<OperatorEnvironment>,
    pub artifacts: Vec<ArtifactDescriptor>,
    pub selected_artifact: Option<String>,
    /// First visible row of the Artifact list, kept across draws.
    pub artifact_list_offset: usize,
    pub conversation_entries: Vec<ConversationEntry>,
    pub conversation_entries_truncated: bool,
    pub inspector: Option<ArtifactInspector>,
    pub stack: StackView,
    pub frame_source: FrameSource,
    pub frame: Option<WorldFrame>,
    pub frame_error: Option<String>,
    /// `x`: the owner's receipt export for this run.
    pub receipt_export: ReceiptExportState,
    /// F4: the presentation layout over the selected tab.
    pub presentation: Presentation,
    cursors: [Option<String>; SECTION_COUNT],
    before_cursors: [Option<String>; SECTION_COUNT],
    etags: [Option<String>; SECTION_COUNT],
    pub(super) latest_requests: [u64; SECTION_COUNT],
    pub(super) pending_requests: [bool; SECTION_COUNT],
    request_sequence: u64,
}

impl Default for RunView {
    fn default() -> Self {
        Self {
            tab: RunTab::default(),
            overview: None,
            progress: ProgressView::default(),
            beliefs: None,
            selected_belief_entity: None,
            context: None,
            world: None,
            media: WorldMedia::default(),
            rejection_dismissed: false,
            failure_dismissed: false,
            agents: Vec::new(),
            selected_invocation: None,
            agent_following: true,
            newer_invocations: 0,
            agent_list_offset: 0,
            agent_detail_scroll: 0,
            environment: None,
            artifacts: Vec::new(),
            selected_artifact: None,
            artifact_list_offset: 0,
            conversation_entries: Vec::new(),
            conversation_entries_truncated: false,
            inspector: None,
            stack: StackView::default(),
            frame_source: FrameSource::World,
            frame: None,
            frame_error: None,
            receipt_export: ReceiptExportState::Idle,
            presentation: Presentation::default(),
            cursors: Default::default(),
            before_cursors: Default::default(),
            etags: Default::default(),
            latest_requests: [0; SECTION_COUNT],
            pending_requests: [false; SECTION_COUNT],
            request_sequence: 0,
        }
    }
}

impl RunView {
    /// Resolve the stable Artifact ID selection to its current index and item.
    pub fn selected_artifact(&self) -> Option<(usize, &ArtifactDescriptor)> {
        let selected = self.selected_artifact.as_deref()?;
        self.artifacts
            .iter()
            .enumerate()
            .find(|(_, artifact)| artifact.artifact_id == selected)
    }

    pub fn selected_invocation(&self) -> Option<(usize, &OperatorAgentInvocation)> {
        let selected = self.selected_invocation.as_deref()?;
        self.agents
            .iter()
            .enumerate()
            .find(|(_, invocation)| invocation.stable_id == selected)
    }

    fn move_belief_selection(&mut self, delta: isize) {
        let Some(beliefs) = self.beliefs.as_ref() else {
            return;
        };
        let Some(current) = beliefs.selected_index(self.selected_belief_entity.as_deref()) else {
            return;
        };
        let next = step_index(current, delta, beliefs.entities.len());
        self.selected_belief_entity = Some(beliefs.entities[next].entity_id.clone());
    }

    /// Cursor the next poll of `section` resumes from.
    pub fn cursor(&self, section: OperatorSection) -> Option<&str> {
        self.cursors[section.index()].as_deref()
    }

    fn move_invocation_selection(&mut self, delta: isize) {
        if self.agents.is_empty() {
            return;
        }
        self.agent_following = false;
        let current = self.selected_invocation().map_or(0, |(index, _)| index);
        let next = step_index(current, delta, self.agents.len());
        self.selected_invocation = Some(self.agents[next].stable_id.clone());
        self.agent_detail_scroll = 0;
    }

    fn merge_agent_invocations(&mut self, incoming: Vec<OperatorAgentInvocation>, backfill: bool) {
        let mut added: Vec<String> = Vec::new();
        for invocation in incoming {
            if let Some(existing) = self
                .agents
                .iter_mut()
                .find(|item| item.stable_id == invocation.stable_id)
            {
                *existing = invocation;
            } else {
                if !backfill && !self.agent_following {
                    added.push(invocation.stable_id.clone());
                }
                self.agents.push(invocation);
            }
        }
        self.agents.sort_by(|left, right| {
            left.started_at
                .cmp(&right.started_at)
                .then_with(|| left.updated_at.cmp(&right.updated_at))
                .then_with(|| left.stable_id.cmp(&right.stable_id))
        });
        if self.agent_following {
            self.selected_invocation = self.agents.last().map(|item| item.stable_id.clone());
            self.newer_invocations = 0;
        } else {
            // Only arrivals after the selection are newer; `w` can pin a
            // call before its page arrives together with older ones.
            let newer = match self.selected_invocation() {
                Some((selected, _)) => self.agents[selected + 1..]
                    .iter()
                    .filter(|item| added.contains(&item.stable_id))
                    .count(),
                None => added.len(),
            };
            self.newer_invocations = self.newer_invocations.saturating_add(newer);
            // Only an unset selection falls back: `w` may select an
            // invocation before its Agents page arrives.
            if self.selected_invocation.is_none() {
                self.selected_invocation = self.agents.first().map(|item| item.stable_id.clone());
            }
        }
    }

    /// Merge Artifact descriptors; returns whether the selection changed.
    fn merge_artifacts(&mut self, incoming: Vec<ArtifactDescriptor>) -> bool {
        let selected = self.selected_artifact.clone();
        for artifact in incoming {
            if let Some(existing) = self
                .artifacts
                .iter_mut()
                .find(|item| item.artifact_id == artifact.artifact_id)
            {
                *existing = artifact;
            } else {
                self.artifacts.push(artifact);
            }
        }
        self.artifacts
            .sort_by(|left, right| left.artifact_id.cmp(&right.artifact_id));
        self.selected_artifact = selected
            .clone()
            .filter(|id| self.artifacts.iter().any(|item| &item.artifact_id == id))
            .or_else(|| self.artifacts.first().map(|item| item.artifact_id.clone()));
        self.selected_artifact != selected
    }
}

fn step_index(current: usize, delta: isize, len: usize) -> usize {
    if delta < 0 {
        current.saturating_sub(delta.unsigned_abs())
    } else {
        current.saturating_add(delta as usize).min(len - 1)
    }
}

fn merge_timeline(retained: &mut Vec<OperatorTimelineEntry>, incoming: Vec<OperatorTimelineEntry>) {
    for entry in incoming {
        if let Some(existing) = retained
            .iter_mut()
            .find(|item| item.stable_id == entry.stable_id)
        {
            *existing = entry;
        } else {
            retained.push(entry);
        }
    }
    retained.sort_by(|left, right| {
        left.observation_sequence
            .cmp(&right.observation_sequence)
            .then_with(|| left.stable_id.cmp(&right.stable_id))
    });
}

/// Parse an RFC 3339 timestamp (`YYYY-MM-DDTHH:MM:SS[.frac](Z|±HH:MM)`) to
/// Unix seconds.
pub fn parse_rfc3339(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() < 20 || bytes[4] != b'-' || bytes[7] != b'-' || bytes[13] != b':' {
        return None;
    }
    let number = |range: std::ops::Range<usize>| -> Option<i64> { value.get(range)?.parse().ok() };
    let (year, month, day) = (number(0..4)?, number(5..7)?, number(8..10)?);
    let (hour, minute, second) = (number(11..13)?, number(14..16)?, number(17..19)?);
    let mut rest = &value[19..];
    if let Some(fraction) = rest.strip_prefix('.') {
        let digits = fraction.bytes().take_while(u8::is_ascii_digit).count();
        rest = &fraction[digits..];
    }
    let offset = match rest {
        "Z" | "z" => 0,
        _ if rest.len() == 6 && matches!(&rest[..1], "+" | "-") => {
            let sign = if rest.starts_with('-') { -1 } else { 1 };
            let hours: i64 = rest[1..3].parse().ok()?;
            let minutes: i64 = rest[4..6].parse().ok()?;
            sign * (hours * 3600 + minutes * 60)
        }
        _ => return None,
    };
    // Days from civil (Howard Hinnant).
    let shifted_year = if month <= 2 { year - 1 } else { year };
    let era = shifted_year.div_euclid(400);
    let year_of_era = shifted_year - era * 400;
    let month_index = (month + 9) % 12;
    let day_of_year = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    Some(days * 86_400 + hour * 3600 + minute * 60 + second - offset)
}

/// Seconds the run has been running: start to finish, or start to `now`.
pub fn run_elapsed_seconds(run: &RunRecord, now_unix: i64) -> Option<i64> {
    let start = parse_rfc3339(run.started_at.as_deref().or(run.created_at.as_deref())?)?;
    let end = match run.finished_at.as_deref() {
        Some(finished) => parse_rfc3339(finished)?,
        None => now_unix,
    };
    Some((end - start).max(0))
}

impl App {
    pub fn configure_images(&mut self, picker: Option<ratatui_image::picker::Picker>) {
        self.image_picker = picker.clone();
        self.view.media.configure(picker);
    }

    pub fn request_visible_frame(&mut self) {
        let visible = self.logical_state_name() == "Run"
            && (self.presenting()
                || self.view.tab == RunTab::World
                || (self.view.tab == RunTab::Overview
                    && self.last_size.0 >= 140
                    && self.last_size.1 >= 40));
        if self.view.world.as_ref().is_some_and(|world| {
            !world
                .frames
                .iter()
                .any(|frame| frame.source == self.view.frame_source.as_str())
        }) {
            return;
        }
        let now = self
            .clock
            .now()
            .saturating_duration_since(self.clock_origin);
        if self
            .view
            .media
            .should_request(visible, self.view.frame_source, now)
        {
            self.request_frame();
        }
    }

    pub fn rejection_open(&self) -> bool {
        !self.view.rejection_dismissed
            && !self.failure_classified()
            && self
                .run
                .as_ref()
                .and_then(|run| run.terminal_detail.as_ref())
                .is_some_and(|detail| detail.kind == "mission_rejected")
    }

    /// Cancellation was confirmed: the request is in flight (the Host
    /// answers it only after the Run Worker's teardown) or was accepted.
    pub fn cancellation_in_progress(&self) -> bool {
        matches!(self.cancellation, CancellationState::Requested { .. })
            || (self.cancellation == CancellationState::Confirming && self.cancellation_submitting)
    }

    /// What the running Mission Run visibly waits on, if anything: teardown
    /// (a `stopping` service, a started teardown, or a requested
    /// cancellation), then a prep step or starting service while the Stack
    /// phase is active, then a live agent call.
    pub fn current_wait(&self) -> Option<CurrentWait<'_>> {
        if self.run.as_ref()?.is_terminal() {
            return None;
        }
        self.teardown_wait()
            .or_else(|| self.stack_wait())
            .or_else(|| self.agent_wait())
    }

    /// Teardown outranks every other wait: once services stop, a starting
    /// service or live agent call is no longer what the run waits on.
    fn teardown_wait(&self) -> Option<CurrentWait<'_>> {
        if let Some(stack) = self.view.stack.stack.as_ref() {
            if let Some(service) = stack
                .services
                .iter()
                .find(|service| service.state == "stopping")
            {
                let position = stack.teardown.as_ref().and_then(|teardown| {
                    let order = &teardown.stop_order;
                    order
                        .iter()
                        .position(|name| *name == service.name)
                        .map(|index| (index + 1, order.len()))
                });
                return Some(CurrentWait::Stopping { service, position });
            }
            if stack.teardown.is_some() {
                return Some(CurrentWait::Teardown(stack));
            }
        }
        self.cancellation_in_progress()
            .then_some(CurrentWait::Cancelling)
    }

    fn stack_wait(&self) -> Option<CurrentWait<'_>> {
        let stack = self.view.stack.stack.as_ref()?;
        let stack_active = self
            .view
            .overview
            .as_ref()
            .and_then(|overview| overview.phase.as_ref())
            .is_none_or(|phase| {
                phase
                    .steps
                    .iter()
                    .any(|step| step.id == "stack" && step.status == "active")
            });
        if !stack_active {
            return None;
        }
        if let Some(step) = stack.step.as_ref() {
            return Some(CurrentWait::Step(step));
        }
        Some(
            stack
                .services
                .iter()
                .enumerate()
                .find(|(_, service)| service.state == "starting")
                .map_or(CurrentWait::Stack(stack), |(index, service)| {
                    CurrentWait::Service {
                        service,
                        position: index + 1,
                        total: stack.services.len(),
                    }
                }),
        )
    }

    /// The latest Hyper Agent or Maneuver Control invocation, while it is live.
    fn agent_wait(&self) -> Option<CurrentWait<'_>> {
        let agents = &self.view.overview.as_ref()?.latest_agents;
        [
            agents.hyper_agent.as_ref(),
            agents.maneuver_control.as_ref(),
        ]
        .into_iter()
        .flatten()
        .find(|invocation| invocation.completion_state == "live")
        .map(CurrentWait::Invocation)
    }

    /// `w`: open what the waiting banner names - a prep step or starting
    /// service on the Stack tab with its log followed, or a live agent call
    /// on the Agents tab. The target is captured now.
    fn inspect_current_wait(&mut self) {
        let Some(target) = self.current_wait().map(CurrentWait::target) else {
            self.hint = Some("Nothing to inspect: the run is not waiting on a stack service, prep step or agent call".to_string());
            return;
        };
        match target {
            WaitTarget::StackRow(name) => {
                self.view.stack.select_following(&name);
                self.select_tab(RunTab::Stack);
                self.request_service_log();
            }
            WaitTarget::Stack => self.select_tab(RunTab::Stack),
            WaitTarget::Invocation(stable_id) => {
                let view = &mut self.view;
                // Pin the captured call: following would move to newer ones.
                view.agent_following = false;
                view.selected_invocation = Some(stable_id);
                view.agent_detail_scroll = 0;
                self.select_tab(RunTab::Agents);
            }
        }
    }

    /// Ask for one Run poll; called by the run loop on its poll cadence.
    /// Terminal refreshes drain every section, then wait at a slower cadence
    /// for the Host's final narrative attempt before one synchronized final wave.
    /// While a historical run is displayed it is polled, and the parked
    /// current run keeps its lifecycle poll.
    pub fn request_poll(&mut self) {
        if self.viewing_history() {
            self.poll_run(PollScope::Historical);
            self.with_current_run(|app| app.poll_run(PollScope::Lifecycle));
        } else {
            self.poll_run(PollScope::Full);
        }
    }

    fn poll_run(&mut self, scope: PollScope) {
        // The cancellation limit bounds the current run's polling only.
        let within_cancellation_limit = scope == PollScope::Historical
            || self
                .cancellation_deadline
                .is_none_or(|deadline| self.clock.now() <= deadline);
        if self.logical_state_name() != "Run" || !within_cancellation_limit {
            return;
        }
        if self.final_refresh.is_some() {
            if self.polling_stopped()
                || self
                    .next_terminal_refresh
                    .is_some_and(|due| self.clock.now() < due)
            {
                return;
            }
            self.next_terminal_refresh = Some(self.clock.now() + std::time::Duration::from_secs(2));
        }
        if self.claim_refresh(RefreshKey::Current) {
            self.outbox.push(HostCommand::PollCurrent {
                credential: self.session.credential.clone(),
            });
        }
        if self.run.is_none() {
            return;
        }
        let mut sections = vec![OperatorSection::Overview, OperatorSection::Stack];
        if scope == PollScope::Lifecycle {
            for section in sections {
                self.request_section(section);
            }
            return;
        }
        if self.final_refresh.is_some() {
            sections = OperatorSection::ALL.to_vec();
            if !self.final_narrative_ready
                && !self.view.pending_requests[OperatorSection::Overview.index()]
            {
                self.release_refresh(&RefreshKey::Section(OperatorSection::Overview));
            }
        } else {
            for section in self.displayed_sections() {
                if !sections.contains(section) {
                    sections.push(*section);
                }
            }
            // Hydrate every section once, including tabs not yet opened.
            for section in OperatorSection::ALL {
                let index = section.index();
                if (self.view.cursors[index].is_none() || self.view.before_cursors[index].is_some())
                    && !sections.contains(&section)
                {
                    sections.push(section);
                }
            }
        }
        for section in sections {
            self.request_section(section);
        }
        match self.view.tab {
            _ if self.presenting() => self.request_visible_frame(),
            RunTab::Stack => self.request_service_log(),
            RunTab::Artifacts => self.request_selected_conversation(),
            RunTab::World | RunTab::Overview => self.request_visible_frame(),
            _ => {}
        }
        // An inspector parked behind the presentation layout is not polled.
        if let (Some(inspector), Some(run), false) = (
            self.view.inspector.as_ref(),
            self.run.as_ref(),
            self.presenting(),
        ) {
            self.outbox.push(HostCommand::FetchArtifactContent {
                purpose: ContentPurpose::Inspector,
                mission_run_id: run.mission_run_id.clone(),
                artifact_id: inspector.artifact_id.clone(),
                offset: inspector.offset,
                limit: CONTENT_PAGE_BYTES,
            });
        }
    }

    pub(crate) fn request_section(&mut self, section: OperatorSection) {
        let Some(mission_run_id) = self.run.as_ref().map(|run| run.mission_run_id.clone()) else {
            return;
        };
        if self.view.pending_requests[section.index()] {
            return;
        }
        if !self.claim_refresh(RefreshKey::Section(section)) {
            return;
        }
        let index = section.index();
        self.view.request_sequence += 1;
        let request_id = self.view.request_sequence;
        self.view.latest_requests[index] = request_id;
        self.view.pending_requests[index] = true;
        self.outbox.push(HostCommand::FetchOperatorView {
            mission_run_id,
            section,
            cursor: if let Some(before) = self.view.before_cursors[index].as_ref() {
                crate::host::OperatorCursor::Before(before.clone())
            } else if let Some(after) = self.view.cursors[index].as_ref() {
                crate::host::OperatorCursor::After(after.clone())
            } else {
                crate::host::OperatorCursor::Latest
            },
            raw: false,
            // A different page must not reuse a preceding page's ETag.
            etag: if self.view.before_cursors[index].is_some() {
                None
            } else {
                self.view.etags[index].clone()
            },
            request_id,
        });
    }

    fn request_selected_conversation(&mut self) {
        let Some(run) = self.run.as_ref() else {
            return;
        };
        let Some((_, artifact)) = self.view.selected_artifact() else {
            return;
        };
        if artifact.classification != "conversation" {
            return;
        }
        let command = HostCommand::FetchConversationEntries {
            mission_run_id: run.mission_run_id.clone(),
            artifact_id: artifact.artifact_id.clone(),
        };
        if self.claim_refresh(RefreshKey::Conversation(artifact.artifact_id.clone())) {
            self.outbox.push(command);
        }
    }

    fn request_frame(&mut self) {
        let Some(mission_run_id) = self.run.as_ref().map(|run| run.mission_run_id.clone()) else {
            return;
        };
        let source = self.view.frame_source;
        if !self.claim_refresh(RefreshKey::Frame(source)) {
            return;
        }
        let etag = self
            .view
            .frame
            .as_ref()
            .filter(|frame| frame.source == source)
            .and_then(|frame| frame.etag.clone());
        self.outbox.push(HostCommand::FetchWorldFrame {
            mission_run_id,
            source,
            etag,
        });
    }

    /// Sections the visible surface reads: the presentation layout's while
    /// it is shown, else the selected tab's.
    fn displayed_sections(&self) -> &'static [OperatorSection] {
        if self.presenting() {
            &presentation::SECTIONS
        } else {
            self.view.tab.sections()
        }
    }

    /// `s` (World tab, presentation layout): the next source the Host
    /// advertises; the old source's frame is cleared.
    pub(super) fn cycle_frame_source(&mut self) {
        let source = super::world::cycle_source(self.view.frame_source, self.view.world.as_ref());
        if source != self.view.frame_source {
            self.view.frame_source = source;
            self.view.media.set_source(source);
            self.view.frame = None;
            self.view.frame_error = None;
            self.release_refresh(&RefreshKey::Frame(source));
            self.request_frame();
        }
    }

    /// Show `tab`; a presentation layout ends and its tab shows again.
    pub(super) fn select_tab(&mut self, tab: RunTab) {
        let presenting = self.presenting();
        if presenting {
            self.end_presentation();
        } else if self.view.tab == tab {
            return;
        }
        self.view.tab = tab;
        for section in tab.sections() {
            self.request_section(*section);
        }
        match tab {
            RunTab::Stack => self.request_service_log(),
            RunTab::Artifacts => self.request_selected_conversation(),
            RunTab::World | RunTab::Overview => self.request_visible_frame(),
            _ => {}
        }
    }

    pub(crate) fn handle_run_key(&mut self, key: KeyEvent) {
        self.hint = None;
        // F4 and the presentation keys work wherever the confirmation dialog
        // does not own the keys; the layout covers a parked inspector.
        let dialog_open =
            self.cancellation == CancellationState::Confirming && !self.cancellation_submitting;
        if !dialog_open
            && (self.presenting() || key.code == KeyCode::F(4))
            && self.handle_presentation_key(key)
        {
            return;
        }
        if self.cancellation == CancellationState::Idle
            && self.view.inspector.is_some()
            && !self.presenting()
        {
            self.handle_inspector_key(key);
            return;
        }
        // The confirmation dialog owns the keys until the request is sent;
        // while it is in flight the Run screen stays navigable.
        match (&self.cancellation, key.code) {
            (CancellationState::Confirming, _) if self.cancellation_submitting => {}
            (CancellationState::Confirming, KeyCode::Esc) => {
                self.cancellation = CancellationState::Idle;
                self.cancellation_origin = None;
                self.cancellation_request_id = None;
                return;
            }
            (CancellationState::Confirming, KeyCode::Enter) => {
                self.submit_cancellation();
                return;
            }
            (CancellationState::Confirming, _) => return,
            _ => {}
        }
        if self.rejection_open() && key.code == KeyCode::Enter {
            self.view.rejection_dismissed = true;
            return;
        }
        let failure_open = self.failure_open();
        if failure_open && key.code == KeyCode::Enter {
            self.view.failure_dismissed = true;
            return;
        }
        if self.view.tab == RunTab::Progress
            && !self.rejection_open()
            && !failure_open
            && self.view.progress.search_editing
            && self.view.progress.handle_key(key)
        {
            return;
        }
        // A historical run is read-only; Esc returns to the current run.
        if self.handle_historical_key(key) {
            return;
        }
        let tab = match key.code {
            KeyCode::Char(c @ '1'..='7') => RunTab::from_digit(c),
            KeyCode::Tab => Some(self.view.tab.offset(1)),
            KeyCode::BackTab => Some(self.view.tab.offset(-1)),
            _ => None,
        };
        if let Some(tab) = tab {
            // `2` on the failure card opens Progress; any tab leaves the card.
            self.view.failure_dismissed |= failure_open;
            self.select_tab(tab);
            return;
        }
        let terminal = self.run.as_ref().is_some_and(RunRecord::is_terminal);
        match key.code {
            KeyCode::Char('?') => {
                self.help_open = true;
                return;
            }
            KeyCode::Char('w') => {
                self.inspect_current_wait();
                return;
            }
            KeyCode::Char('q') => {
                if terminal {
                    self.exit_terminal_run();
                } else if self.cancellation == CancellationState::Idle
                    && self.require_mutations_enabled()
                {
                    self.cancellation = CancellationState::Confirming;
                    self.cancellation_origin = Some(CancellationOrigin::CleanExit);
                }
                return;
            }
            KeyCode::Char('c') if !terminal => {
                if self.cancellation == CancellationState::Idle && self.require_mutations_enabled()
                {
                    self.cancellation = CancellationState::Confirming;
                    self.cancellation_origin = Some(CancellationOrigin::ContinueConsole);
                }
                return;
            }
            KeyCode::Char('e') if terminal => {
                if let Some(reason) = self.new_intent_blocked() {
                    self.hint = Some(reason);
                } else {
                    self.start_new_intent();
                }
                return;
            }
            KeyCode::Char('l') if self.failure_classified() => {
                // The log opens in the inspector, behind the tabs.
                self.end_presentation();
                self.open_failure_log();
                return;
            }
            KeyCode::Char('y') if self.failure_classified() => {
                self.copy_run_identity();
                return;
            }
            KeyCode::Char('x') if terminal => {
                self.export_receipt();
                return;
            }
            _ => {}
        }
        if self.presenting() {
            return;
        }
        match self.view.tab {
            RunTab::Progress => {
                self.view.progress.handle_key(key);
            }
            RunTab::Agents => self.handle_agents_key(key),
            RunTab::World => {
                if key.code == KeyCode::Char('s') {
                    self.cycle_frame_source();
                }
                if key.code == KeyCode::Char('p') {
                    self.view.media.paused = !self.view.media.paused;
                }
            }
            RunTab::Stack => self.handle_stack_key(key),
            RunTab::Artifacts => match key.code {
                KeyCode::Up | KeyCode::Char('k') => self.move_artifact_selection(-1),
                KeyCode::Down | KeyCode::Char('j') => self.move_artifact_selection(1),
                KeyCode::Home => self.move_artifact_selection(isize::MIN),
                KeyCode::End => self.move_artifact_selection(isize::MAX),
                KeyCode::Enter => self.open_artifact_inspector(),
                _ => {}
            },
            RunTab::BeliefContext => match key.code {
                KeyCode::Up | KeyCode::Char('k') => self.view.move_belief_selection(-1),
                KeyCode::Down | KeyCode::Char('j') => self.view.move_belief_selection(1),
                _ => {}
            },
            RunTab::Overview => {}
        }
    }

    /// Wrapped lines scroll within the byte page; Left/Right change the page.
    fn handle_inspector_key(&mut self, key: KeyEvent) {
        let Some(inspector) = self.view.inspector.as_mut() else {
            return;
        };
        let page = isize::try_from(inspector.rows.max(1)).unwrap_or(isize::MAX);
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => inspector.scroll_by(-1),
            KeyCode::Down | KeyCode::Char('j') => inspector.scroll_by(1),
            KeyCode::PageUp => inspector.scroll_by(-page),
            KeyCode::PageDown => inspector.scroll_by(page),
            KeyCode::Home => inspector.scroll_to(0),
            KeyCode::End => inspector.scroll_to(usize::MAX),
            KeyCode::Right | KeyCode::Char('n') => self.next_artifact_page(),
            KeyCode::Left | KeyCode::Char('p') => self.previous_artifact_page(),
            KeyCode::Esc => self.view.inspector = None,
            _ => {}
        }
    }

    fn handle_agents_key(&mut self, key: KeyEvent) {
        let view = &mut self.view;
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => view.move_invocation_selection(-1),
            KeyCode::Down | KeyCode::Char('j') => view.move_invocation_selection(1),
            // Reaching the newest row by hand does not resume following; `f` does.
            KeyCode::Home => view.move_invocation_selection(isize::MIN),
            KeyCode::End => view.move_invocation_selection(isize::MAX),
            KeyCode::PageUp => {
                view.agent_detail_scroll = view.agent_detail_scroll.saturating_sub(5)
            }
            KeyCode::PageDown => {
                view.agent_detail_scroll = view.agent_detail_scroll.saturating_add(5);
            }
            KeyCode::Char('f') => {
                view.agent_following = true;
                view.newer_invocations = 0;
                view.selected_invocation = view.agents.last().map(|item| item.stable_id.clone());
                view.agent_detail_scroll = 0;
            }
            _ => {}
        }
    }

    fn submit_cancellation(&mut self) {
        if self.cancellation_submitting {
            return;
        }
        let Some(run) = self.run.as_ref() else {
            return;
        };
        let cancellation_request_id = uuid::Uuid::new_v4().to_string();
        self.outbox.push(HostCommand::Cancel {
            mission_run_id: run.mission_run_id.clone(),
            request: CancellationRequest {
                cancellation_request_id: cancellation_request_id.clone(),
            },
            credential: self.session.credential.clone(),
        });
        self.cancellation_submitting = true;
        self.cancellation_request_id = Some(cancellation_request_id);
        if self.cancellation_origin == Some(CancellationOrigin::CleanExit) {
            self.cancellation_deadline = Some(self.clock.now() + super::CANCELLATION_POLL_LIMIT);
        }
    }

    fn move_artifact_selection(&mut self, delta: isize) {
        if self.view.artifacts.is_empty() {
            return;
        }
        let current = self.view.selected_artifact().map_or(0, |(index, _)| index);
        let next = step_index(current, delta, self.view.artifacts.len());
        let next_id = self.view.artifacts[next].artifact_id.clone();
        if self.view.selected_artifact.as_deref() != Some(next_id.as_str()) {
            self.view.selected_artifact = Some(next_id);
            self.sync_selected_conversation();
        }
    }

    fn sync_selected_conversation(&mut self) {
        self.view.conversation_entries.clear();
        self.view.conversation_entries_truncated = false;
        self.request_selected_conversation();
    }

    fn open_artifact_inspector(&mut self) {
        let Some((_, artifact)) = self.view.selected_artifact() else {
            return;
        };
        if !artifact.is_inspectable() {
            return;
        }
        let artifact_id = artifact.artifact_id.clone();
        let classification = artifact.classification.clone();
        let Some(run) = self.run.as_ref() else {
            return;
        };
        let mission_run_id = run.mission_run_id.clone();
        self.view.inspector = Some(ArtifactInspector {
            artifact_id: artifact_id.clone(),
            classification,
            offset: 0,
            previous_offsets: Vec::new(),
            page: None,
            scroll: 0,
            rows: 0,
            max_scroll: 0,
            tail: false,
        });
        self.outbox.push(HostCommand::FetchArtifactContent {
            purpose: ContentPurpose::Inspector,
            mission_run_id,
            artifact_id,
            offset: 0,
            limit: CONTENT_PAGE_BYTES,
        });
    }

    fn inspector_fetch(&mut self, offset: u64) {
        let (Some(inspector), Some(run)) = (self.view.inspector.as_ref(), self.run.as_ref()) else {
            return;
        };
        self.outbox.push(HostCommand::FetchArtifactContent {
            purpose: ContentPurpose::Inspector,
            mission_run_id: run.mission_run_id.clone(),
            artifact_id: inspector.artifact_id.clone(),
            offset,
            limit: CONTENT_PAGE_BYTES,
        });
    }

    fn next_artifact_page(&mut self) {
        let Some(inspector) = self.view.inspector.as_mut() else {
            return;
        };
        let Some(next_offset) = inspector
            .page
            .as_ref()
            .filter(|page| !page.eof)
            .and_then(|page| page.next_offset)
        else {
            return;
        };
        inspector.previous_offsets.push(inspector.offset);
        inspector.offset = next_offset;
        inspector.page = None;
        inspector.scroll = 0;
        inspector.max_scroll = 0;
        self.inspector_fetch(next_offset);
    }

    fn previous_artifact_page(&mut self) {
        let Some(inspector) = self.view.inspector.as_mut() else {
            return;
        };
        // A page opened at the tail has no recorded predecessor: step back
        // one page width.
        let Some(previous_offset) = inspector.previous_offsets.pop().or_else(|| {
            (inspector.offset > 0).then(|| inspector.offset.saturating_sub(CONTENT_PAGE_BYTES))
        }) else {
            return;
        };
        inspector.offset = previous_offset;
        inspector.page = None;
        inspector.scroll = 0;
        inspector.max_scroll = 0;
        self.inspector_fetch(previous_offset);
    }

    fn run_matches(&self, mission_run_id: &str) -> bool {
        self.run
            .as_ref()
            .is_some_and(|run| run.mission_run_id == mission_run_id)
    }

    pub(crate) fn reduce_operator_view(
        &mut self,
        mission_run_id: &str,
        section: OperatorSection,
        request_id: u64,
        result: Result<Fetched<OperatorViewPage>, HostError>,
    ) {
        let index = section.index();
        if self.view.latest_requests[index] != request_id || !self.run_matches(mission_run_id) {
            return;
        }
        self.view.pending_requests[index] = false;
        let (page, etag) = match result {
            Ok(Fetched::NotModified) => {
                self.historical_page_arrived(section);
                if self.final_refresh.is_some() && self.final_narrative_ready {
                    self.final_complete.insert(section);
                }
                return;
            }
            Ok(Fetched::Fresh { value, etag })
                if value.meta().mission_run_id == mission_run_id
                    && value.meta().section == section =>
            {
                self.historical_page_arrived(section);
                (value, etag)
            }
            Ok(Fetched::Fresh { .. }) => return,
            // The Run Root is gone: say so, and do not retry what cannot return.
            Err(HostError::NotFound { code, message }) if code == "run_root_unavailable" => {
                self.notice = Some(format!(
                    "Run Root unavailable for {mission_run_id}: {message}"
                ));
                self.mark_historical_unavailable(&message);
                return;
            }
            Err(error) => {
                self.release_refresh(&RefreshKey::Section(section));
                // A historical run still loading from disk: its loading state
                // says so and the next poll retries. Other errors are failures.
                if matches!(error, HostError::Timeout(_)) && self.historical_read_timed_out() {
                    return;
                }
                self.notice = Some(format!(
                    "Host {} poll failed ({error}); showing last known state",
                    section.as_str()
                ));
                return;
            }
        };
        let backfill = self.view.before_cursors[index].is_some();
        let initial = self.view.cursors[index].is_none();
        let has_more = page.meta().has_more;
        if initial || !backfill {
            // Before pages report the *current* watermark, not the initial
            // one. Retain the initial watermark so concurrent deltas replay.
            self.view.cursors[index] = Some(page.meta().next_cursor.clone());
        }
        self.view.before_cursors[index] = if (initial || backfill) && has_more {
            page.meta().before_cursor.clone()
        } else {
            None
        };
        self.view.etags[index] = if backfill { None } else { etag };
        let continue_paging = has_more || backfill;
        let terminal_page = crate::host::is_terminal_status(&page.meta().run_status);
        match page {
            OperatorViewPage::Overview(page) => {
                let mut overview = page.overview;
                let mut retained = self
                    .view
                    .overview
                    .take()
                    .map_or_else(Vec::new, |previous| previous.recent_events);
                if backfill {
                    overview.recent_events.retain(|entry| {
                        !retained.iter().any(|old| old.stable_id == entry.stable_id)
                    });
                }
                merge_timeline(&mut retained, overview.recent_events);
                overview.recent_events = retained;
                self.view.overview = Some(overview);
                self.observe_attention();
            }
            OperatorViewPage::Agents(mut page) => {
                if backfill {
                    page.agents.retain(|item| {
                        !self
                            .view
                            .agents
                            .iter()
                            .any(|old| old.stable_id == item.stable_id)
                    });
                }
                self.view.merge_agent_invocations(page.agents, backfill);
            }
            OperatorViewPage::Environment(page) => {
                let mut environment = page.environment;
                let mut retained = self
                    .view
                    .environment
                    .take()
                    .map_or_else(Vec::new, |previous| previous.timeline);
                if backfill {
                    environment.timeline.retain(|entry| {
                        !retained.iter().any(|old| old.stable_id == entry.stable_id)
                    });
                }
                merge_timeline(&mut retained, environment.timeline);
                environment.timeline = retained;
                self.view.environment = Some(environment);
            }
            OperatorViewPage::Stack(page) => self.apply_stack(page.stack),
            OperatorViewPage::Artifacts(mut page) => {
                if backfill {
                    page.artifacts.retain(|item| {
                        !self
                            .view
                            .artifacts
                            .iter()
                            .any(|old| old.artifact_id == item.artifact_id)
                    });
                }
                if self.view.merge_artifacts(page.artifacts) {
                    self.sync_selected_conversation();
                }
            }
            OperatorViewPage::Progress(mut page) => {
                if backfill {
                    page.progress
                        .nodes
                        .retain(|node| !self.view.progress.nodes.contains_key(&node.node_id));
                }
                self.view.progress.ingest(page.progress);
            }
            OperatorViewPage::Beliefs(page) => self.view.beliefs = Some(page.beliefs),
            OperatorViewPage::Context(page) => self.view.context = Some(page.context),
            OperatorViewPage::World(page) => {
                // A frozen presentation keeps its source and held frame.
                if !self.view.presentation.is_frozen()
                    && !page.world.frames.is_empty()
                    && !page
                        .world
                        .frames
                        .iter()
                        .any(|frame| frame.source == self.view.frame_source.as_str())
                {
                    self.view.frame_source =
                        super::world::cycle_source(self.view.frame_source, Some(&page.world));
                    self.view.media.set_source(self.view.frame_source);
                    self.view.frame = None;
                    self.view.frame_error = None;
                }
                self.view.world = Some(page.world);
            }
        }
        if self.final_refresh.is_some() {
            let narrative_ready = self.view.overview.as_ref().is_some_and(|overview| {
                overview.narrative.terminal
                    && matches!(
                        overview.narrative.status.as_str(),
                        "available" | "unavailable"
                    )
            });
            if narrative_ready && !self.final_narrative_ready {
                self.final_narrative_ready = true;
                self.final_complete.clear();
                self.final_refresh = Some(std::collections::HashSet::from([RefreshKey::Current]));
                self.next_terminal_refresh = Some(self.clock.now());
            } else if terminal_page
                && !continue_paging
                && self
                    .final_refresh
                    .as_ref()
                    .is_some_and(|claimed| claimed.contains(&RefreshKey::Section(section)))
            {
                self.final_complete.insert(section);
            }
        }
        if continue_paging || (self.final_refresh.is_some() && !terminal_page) {
            self.release_refresh(&RefreshKey::Section(section));
            self.request_section(section);
        }
    }

    pub(crate) fn reduce_artifact_content(
        &mut self,
        purpose: ContentPurpose,
        mission_run_id: &str,
        artifact_id: &str,
        requested_offset: u64,
        result: Result<ArtifactContentPage, HostError>,
    ) {
        if !self.run_matches(mission_run_id) {
            return;
        }
        match purpose {
            ContentPurpose::ServiceLog => {
                self.apply_service_log(artifact_id, requested_offset, result);
            }
            ContentPurpose::Inspector => {
                let Some(inspector) = self.view.inspector.as_mut() else {
                    return;
                };
                if inspector.artifact_id != artifact_id || inspector.offset != requested_offset {
                    return;
                }
                match result {
                    Ok(page) => {
                        // Tail: jump once to the last byte page (the log may
                        // grow, so only ever forward), then show its last line.
                        let last_page = page
                            .byte_size
                            .map(|size| size.saturating_sub(CONTENT_PAGE_BYTES))
                            .filter(|last| inspector.tail && !page.eof && *last > requested_offset);
                        if let Some(offset) = last_page {
                            inspector.offset = offset;
                            self.inspector_fetch(offset);
                            return;
                        }
                        if inspector.tail {
                            inspector.tail = false;
                            inspector.scroll = usize::MAX;
                        }
                        inspector.page = Some(page);
                    }
                    Err(HostError::NotFound { .. }) => {
                        self.view.inspector = None;
                        self.notice = Some("Artifact became unavailable".to_string());
                    }
                    Err(error) => {
                        self.notice = Some(format!(
                            "Host Artifact preview failed ({error}); showing last known state"
                        ));
                    }
                }
            }
        }
    }

    pub(crate) fn reduce_conversation_entries(
        &mut self,
        mission_run_id: &str,
        artifact_id: &str,
        result: Result<EvidencePage<ConversationEntry>, HostError>,
    ) {
        let selected = self.view.selected_artifact().is_some_and(|(_, artifact)| {
            artifact.classification == "conversation" && artifact.artifact_id == artifact_id
        });
        if !self.run_matches(mission_run_id) || !selected {
            return;
        }
        match result {
            Ok(page) => {
                self.view.conversation_entries = page.items;
                self.view.conversation_entries_truncated = page.truncated;
            }
            Err(error) => {
                self.release_refresh(&RefreshKey::Conversation(artifact_id.to_string()));
                self.notice = Some(format!(
                    "Host conversation poll failed ({error}); showing last known state"
                ));
            }
        }
    }

    pub(crate) fn reduce_world_frame(
        &mut self,
        mission_run_id: &str,
        source: FrameSource,
        result: Result<Fetched<WorldFrame>, HostError>,
    ) {
        if !self.run_matches(mission_run_id) || self.view.frame_source != source {
            return;
        }
        // A frozen presentation holds its frame: a reply already in flight
        // is dropped, and its claim released for the fetch after resuming.
        if self.view.presentation.is_frozen() {
            self.release_refresh(&RefreshKey::Frame(source));
            return;
        }
        match result {
            Ok(Fetched::NotModified) => {}
            Ok(Fetched::Fresh { value, .. }) => {
                let metadata = WorldFrame {
                    source: value.source,
                    media_type: value.media_type.clone(),
                    etag: value.etag.clone(),
                    sequence: value.sequence,
                    mission_time: value.mission_time.clone(),
                    bytes: Vec::new(),
                };
                self.view.media.submit(value);
                self.view.frame = Some(metadata);
                self.view.frame_error = None;
            }
            Err(HostError::NotFound { code, message }) => {
                self.view.frame = None;
                self.view.frame_error = Some(format!("{code}: {message}"));
            }
            Err(error) => {
                self.release_refresh(&RefreshKey::Frame(source));
                self.view.frame_error = Some(error.to_string());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::parse_rfc3339;

    #[test]
    fn rfc3339_parses_utc_fractions_and_offsets() {
        assert_eq!(parse_rfc3339("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(parse_rfc3339("2026-08-24T12:00:03Z"), Some(1_787_572_803));
        assert_eq!(
            parse_rfc3339("2026-08-24T12:00:03.250Z"),
            parse_rfc3339("2026-08-24T12:00:03Z")
        );
        assert_eq!(
            parse_rfc3339("2026-08-24T22:00:03+10:00"),
            parse_rfc3339("2026-08-24T12:00:03Z")
        );
        assert_eq!(parse_rfc3339("not a time"), None);
    }
}
