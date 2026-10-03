//! Run screen: tabs, operator-view polling with per-section cursors and
//! ETags, section reducers, the Artifact inspector, and cancellation keys.

use crossterm::event::{KeyCode, KeyEvent};

use super::progress::ProgressView;
use super::world::WorldMedia;
use super::{App, CancellationOrigin, CancellationState, RefreshKey, StackView};
use crate::host::{
    ArtifactContentPage, ArtifactDescriptor, CancellationRequest, ContentPurpose,
    ConversationEntry, EvidencePage, Fetched, FrameSource, HostCommand, HostError,
    OperatorAgentInvocation, OperatorBeliefs, OperatorContext, OperatorEnvironment,
    OperatorOverview, OperatorSection, OperatorTimelineEntry, OperatorViewPage, OperatorWorld,
    RunRecord, WorldFrame,
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactInspector {
    pub artifact_id: String,
    pub classification: String,
    pub offset: u64,
    pub previous_offsets: Vec<u64>,
    pub page: Option<ArtifactContentPage>,
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
    pub agents: Vec<OperatorAgentInvocation>,
    pub selected_invocation: Option<String>,
    pub agent_following: bool,
    pub newer_invocations: usize,
    pub agent_detail_scroll: u16,
    pub environment: Option<OperatorEnvironment>,
    pub artifacts: Vec<ArtifactDescriptor>,
    pub selected_artifact: Option<String>,
    pub conversation_entries: Vec<ConversationEntry>,
    pub conversation_entries_truncated: bool,
    pub inspector: Option<ArtifactInspector>,
    pub stack: StackView,
    pub frame_source: FrameSource,
    pub frame: Option<WorldFrame>,
    pub frame_error: Option<String>,
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
            agents: Vec::new(),
            selected_invocation: None,
            agent_following: true,
            newer_invocations: 0,
            agent_detail_scroll: 0,
            environment: None,
            artifacts: Vec::new(),
            selected_artifact: None,
            conversation_entries: Vec::new(),
            conversation_entries_truncated: false,
            inspector: None,
            stack: StackView::default(),
            frame_source: FrameSource::World,
            frame: None,
            frame_error: None,
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
        let mut added = 0usize;
        for invocation in incoming {
            if let Some(existing) = self
                .agents
                .iter_mut()
                .find(|item| item.stable_id == invocation.stable_id)
            {
                *existing = invocation;
            } else {
                self.agents.push(invocation);
                if !backfill {
                    added += 1;
                }
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
            self.newer_invocations = self.newer_invocations.saturating_add(added);
            if self.selected_invocation().is_none() {
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
            && (self.view.tab == RunTab::World
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
            .saturating_duration_since(self.media_clock_origin);
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
            && self
                .run
                .as_ref()
                .and_then(|run| run.terminal_detail.as_ref())
                .is_some_and(|detail| detail.kind == "mission_rejected")
    }

    /// Ask for one Run poll; called by the run loop on its poll cadence.
    /// Terminal refreshes drain every section, then wait at a slower cadence
    /// for the Host's final narrative attempt before one synchronized final wave.
    pub fn request_poll(&mut self) {
        let within_cancellation_limit = self
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
        if self.final_refresh.is_some() {
            sections = OperatorSection::ALL.to_vec();
            if !self.final_narrative_ready
                && !self.view.pending_requests[OperatorSection::Overview.index()]
            {
                self.release_refresh(&RefreshKey::Section(OperatorSection::Overview));
            }
        } else {
            for section in self.view.tab.sections() {
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
            RunTab::Stack => self.request_service_log(),
            RunTab::Artifacts => self.request_selected_conversation(),
            RunTab::World | RunTab::Overview => self.request_visible_frame(),
            _ => {}
        }
        if let (Some(inspector), Some(run)) = (self.view.inspector.as_ref(), self.run.as_ref()) {
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

    fn select_tab(&mut self, tab: RunTab) {
        if self.view.tab == tab {
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
        if self.cancellation == CancellationState::Idle && self.view.inspector.is_some() {
            match key.code {
                KeyCode::Right | KeyCode::Char('n') => self.next_artifact_page(),
                KeyCode::Left | KeyCode::Char('p') => self.previous_artifact_page(),
                KeyCode::Esc => self.view.inspector = None,
                _ => {}
            }
            return;
        }
        match (&self.cancellation, key.code) {
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
        if self.view.tab == RunTab::Progress
            && !self.rejection_open()
            && self.view.progress.search_editing
            && self.view.progress.handle_key(key)
        {
            return;
        }
        let tab = match key.code {
            KeyCode::Char(c @ '1'..='7') => RunTab::from_digit(c),
            KeyCode::Tab => Some(self.view.tab.offset(1)),
            KeyCode::BackTab => Some(self.view.tab.offset(-1)),
            _ => None,
        };
        if let Some(tab) = tab {
            self.select_tab(tab);
            return;
        }
        let terminal = self.run.as_ref().is_some_and(RunRecord::is_terminal);
        match key.code {
            KeyCode::Char('?') => {
                self.help_open = true;
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
                self.start_new_intent();
                return;
            }
            _ => {}
        }
        match self.view.tab {
            RunTab::Progress => {
                self.view.progress.handle_key(key);
            }
            RunTab::Agents => self.handle_agents_key(key),
            RunTab::World => {
                if key.code == KeyCode::Char('s') {
                    let source = super::world::cycle_source(
                        self.view.frame_source,
                        self.view.world.as_ref(),
                    );
                    if source != self.view.frame_source {
                        self.view.frame_source = source;
                        self.view.media.set_source(source);
                        self.view.frame = None;
                        self.view.frame_error = None;
                        self.release_refresh(&RefreshKey::Frame(source));
                        self.request_frame();
                    }
                }
                if key.code == KeyCode::Char('p') {
                    self.view.media.paused = !self.view.media.paused;
                }
            }
            RunTab::Stack => self.handle_stack_key(key),
            RunTab::Artifacts => match key.code {
                KeyCode::Up | KeyCode::Char('k') => self.move_artifact_selection(-1),
                KeyCode::Down | KeyCode::Char('j') => self.move_artifact_selection(1),
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

    fn handle_agents_key(&mut self, key: KeyEvent) {
        let view = &mut self.view;
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => view.move_invocation_selection(-1),
            KeyCode::Down | KeyCode::Char('j') => view.move_invocation_selection(1),
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
        self.inspector_fetch(next_offset);
    }

    fn previous_artifact_page(&mut self) {
        let Some(inspector) = self.view.inspector.as_mut() else {
            return;
        };
        let Some(previous_offset) = inspector.previous_offsets.pop() else {
            return;
        };
        inspector.offset = previous_offset;
        inspector.page = None;
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
                if self.final_refresh.is_some() && self.final_narrative_ready {
                    self.final_complete.insert(section);
                }
                return;
            }
            Ok(Fetched::Fresh { value, etag })
                if value.meta().mission_run_id == mission_run_id
                    && value.meta().section == section =>
            {
                (value, etag)
            }
            Ok(Fetched::Fresh { .. }) => return,
            Err(error) => {
                self.release_refresh(&RefreshKey::Section(section));
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
                if !page.world.frames.is_empty()
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
                    Ok(page) => inspector.page = Some(page),
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
