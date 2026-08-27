//! Application state machine for the Operator Console.
//!
//! Pure presentation-layer logic: the app never performs IO itself. Keyboard
//! and resize events go in, host effects come out through an outbox drained by
//! the run loop, and host responses arrive back as [`HostMessage`] values.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};

use crate::host::{
    ActivationOutcome, ActivationRequest, ArtifactContentPage, ArtifactDescriptor,
    CancellationOutcome, CancellationRequest, ConversationEntry, CurrentRun, EvidencePage, Health,
    HostError, MissionIntent, NarrativeResponse, ObservationEnvelope, OperatorAgentInvocation,
    OperatorEnvironment, OperatorOverview, OperatorSection, OperatorTimelineEntry,
    OperatorViewPage, RunActivity, RunNarrative, RunRecord,
};

const CANCELLATION_POLL_LIMIT: Duration = Duration::from_secs(10);

pub trait Clock: Send + Sync + std::fmt::Debug {
    fn now(&self) -> Instant;
}

#[derive(Debug)]
struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> Instant {
        Instant::now()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CleanExitAction {
    Cancelled,
    CancellationTimedOut,
    TerminalRun,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct OwnerSessionState {
    pub host_authority: String,
    pub host_api_major: u32,
    pub mission_run_id: String,
    pub console_session_id: String,
    pub credential: String,
}

#[derive(Debug, Clone)]
pub struct SessionStateFile {
    path: PathBuf,
}

impl SessionStateFile {
    pub fn default_path() -> Self {
        let base = std::env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state"))
            })
            .unwrap_or_else(|| PathBuf::from(".local/state"));
        Self::at(base.join("onr/operator-console/session.json"))
    }

    pub fn at(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn load(&self) -> io::Result<Option<OwnerSessionState>> {
        match fs::read(&self.path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map(Some)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }

    pub fn save(&self, state: &OwnerSessionState) -> io::Result<()> {
        let parent = self
            .path
            .parent()
            .ok_or_else(|| io::Error::other("state path has no parent"))?;
        #[cfg(unix)]
        let mut created_directories = Vec::new();
        #[cfg(unix)]
        {
            let mut directory = Some(parent);
            while let Some(path) = directory {
                if path.exists() {
                    break;
                }
                created_directories.push(path.to_path_buf());
                directory = path.parent();
            }
        }
        fs::create_dir_all(parent)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            for path in created_directories {
                fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
            }
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
        }
        let temporary = parent.join(format!(".session-{}.tmp", uuid::Uuid::new_v4()));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temporary)?;
        file.write_all(&serde_json::to_vec(state).map_err(io::Error::other)?)?;
        file.sync_all()?;
        fs::rename(&temporary, &self.path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&self.path, fs::Permissions::from_mode(0o600))?;
        }
        Ok(())
    }

    pub fn remove(&self) -> io::Result<()> {
        match fs::remove_file(&self.path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CancellationState {
    Idle,
    Confirming,
    Requested { cancellation_request_id: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CancellationOrigin {
    ContinueConsole,
    CleanExit,
}

/// Minimum terminal size supported by the fixed dashboard layout.
pub const MIN_WIDTH: u16 = 100;
pub const MIN_HEIGHT: u16 = 30;

/// Value sent as `source_authority` on a Mission Activation.
pub const SOURCE_AUTHORITY: &str = "operator_console";

/// Runtime Host connection freshness derived from successful response receipt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Liveness {
    Live,
    Stale,
    Offline,
}

/// Inclusive thresholds used to classify Runtime Host liveness.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LivenessThresholds {
    pub stale: Duration,
    pub offline: Duration,
}

impl Default for LivenessThresholds {
    fn default() -> Self {
        Self {
            stale: Duration::from_secs(5),
            offline: Duration::from_secs(30),
        }
    }
}

/// Top-level console states.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AppState {
    /// Health/version handshake with the Runtime Host is in flight.
    Connecting,
    /// Operator is composing the multiline Mission Intent.
    Editing,
    /// Operator is reviewing the Mission Intent before activation.
    ReviewActivation,
    /// An activation POST is in flight; further submits are ignored.
    Submitting,
    /// Observing the current Mission Run dashboard.
    Run,
    /// A recoverable failure; `retry_connect` offers `r` to reconnect.
    Error {
        message: String,
        retry_connect: bool,
    },
    /// Terminal is below [`MIN_WIDTH`]x[`MIN_HEIGHT`]; `resume` restores on resize.
    ResizeRequired { resume: Box<AppState> },
}

impl AppState {
    /// Human-readable state name for tests and status chrome.
    pub fn name(&self) -> &'static str {
        match self {
            AppState::Connecting => "Connecting",
            AppState::Editing => "Editing",
            AppState::ReviewActivation => "ReviewActivation",
            AppState::Submitting => "Submitting",
            AppState::Run => "Run",
            AppState::Error { .. } => "Error",
            AppState::ResizeRequired { .. } => "ResizeRequired",
        }
    }
}

/// Effects the run loop forwards to the host worker thread.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostCommand {
    /// Perform the health/version handshake.
    Connect,
    /// Submit one Mission Activation with the session credential.
    Submit {
        request: Box<ActivationRequest>,
        credential: String,
    },
    /// Fetch the current Mission Run snapshot.
    PollCurrent { credential: String },
    FetchIntent {
        mission_run_id: String,
        credential: String,
    },
    /// Fetch all public activity evidence pages for the current Mission Run.
    FetchActivities { mission_run_id: String },
    /// Fetch all public observation evidence pages for the current Mission Run.
    FetchObservations { mission_run_id: String },
    /// Fetch the optional Run Narrative for the current Mission Run.
    FetchNarrative { mission_run_id: String },
    /// Fetch all public Artifact descriptors for the current Mission Run.
    FetchArtifacts { mission_run_id: String },
    /// Fetch one incremental v1.1 operator-view section.
    FetchOperatorView {
        mission_run_id: String,
        section: OperatorSection,
        cursor: Option<String>,
        raw: bool,
        request_id: u64,
    },
    FetchArtifactContent {
        mission_run_id: String,
        artifact_id: String,
        offset: u64,
    },
    FetchConversationEntries {
        mission_run_id: String,
        artifact_id: String,
    },
    Cancel {
        mission_run_id: String,
        request: CancellationRequest,
        credential: String,
    },
}

/// Responses from the host worker thread.
#[derive(Debug)]
pub enum HostMessage {
    Connected(Result<Health, HostError>),
    Activated(Result<ActivationOutcome, HostError>),
    Current(Result<CurrentRun, HostError>),
    Intent(Result<MissionIntent, HostError>),
    Activities(Result<EvidencePage<RunActivity>, HostError>),
    Observations(Result<EvidencePage<ObservationEnvelope>, HostError>),
    Narrative {
        mission_run_id: String,
        result: Result<NarrativeResponse, HostError>,
    },
    Artifacts(Result<EvidencePage<ArtifactDescriptor>, HostError>),
    OperatorView {
        mission_run_id: String,
        section: OperatorSection,
        request_id: u64,
        result: Result<OperatorViewPage, HostError>,
    },
    ArtifactContent(Result<ArtifactContentPage, HostError>),
    ConversationEntries {
        mission_run_id: String,
        artifact_id: String,
        result: Result<EvidencePage<ConversationEntry>, HostError>,
    },
    Cancelled(Result<CancellationOutcome, HostError>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum PaneFocus {
    #[default]
    Activities,
    Artifacts,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum OperatorTab {
    #[default]
    Overview,
    Agents,
    Environment,
    Artifacts,
}

impl OperatorTab {
    pub const ALL: [Self; 4] = [
        Self::Overview,
        Self::Agents,
        Self::Environment,
        Self::Artifacts,
    ];

    pub fn index(self) -> usize {
        match self {
            Self::Overview => 0,
            Self::Agents => 1,
            Self::Environment => 2,
            Self::Artifacts => 3,
        }
    }

    pub fn section(self) -> OperatorSection {
        match self {
            Self::Overview => OperatorSection::Overview,
            Self::Agents => OperatorSection::Agents,
            Self::Environment => OperatorSection::Environment,
            Self::Artifacts => OperatorSection::Artifacts,
        }
    }

    fn next(self) -> Self {
        Self::ALL[(self.index() + 1) % Self::ALL.len()]
    }

    fn previous(self) -> Self {
        Self::ALL[(self.index() + Self::ALL.len() - 1) % Self::ALL.len()]
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

/// Console Session identity generated before activation.
#[derive(Debug, Clone)]
pub struct ConsoleSession {
    pub session_id: String,
    pub credential: String,
}

impl ConsoleSession {
    fn generate() -> Self {
        let credential = format!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        );
        ConsoleSession {
            session_id: uuid::Uuid::new_v4().to_string(),
            credential,
        }
    }
}

/// The console application.
#[derive(Debug)]
pub struct App {
    pub state: AppState,
    /// Runtime Host base URL, e.g. `http://127.0.0.1:8787`.
    pub host_addr: String,
    /// Console Session identity for this console process.
    pub session: ConsoleSession,
    /// Multiline Mission Intent editor buffer.
    pub intent: String,
    /// Cursor position as a character index into `intent`.
    pub cursor: usize,
    /// Transient hint shown in the editor footer.
    pub hint: Option<String>,
    /// Non-fatal notice shown on the dashboard (e.g. a lapsed poll).
    pub notice: Option<String>,
    /// Compatible host health once connected.
    pub health: Option<Health>,
    /// Accepted activation, once submitted.
    pub activation: Option<crate::host::ActivationAccepted>,
    /// Latest known Mission Run snapshot.
    pub run: Option<RunRecord>,
    /// Latest redacted activity projection for the current Mission Run.
    pub activities: Vec<RunActivity>,
    /// Latest redacted observation projection for the current Mission Run.
    pub observations: Vec<ObservationEnvelope>,
    /// Latest optional Run Narrative for the current Mission Run.
    pub narrative: Option<RunNarrative>,
    /// Whether the activity timeline hit the client page cap.
    pub activities_truncated: bool,
    /// Whether the observation timeline hit the client page cap.
    pub observations_truncated: bool,
    /// Stable activity selection keyed by activity id.
    pub selected_activity: Option<String>,
    /// Latest public Artifact descriptors for the current Mission Run.
    pub artifacts: Vec<ArtifactDescriptor>,
    pub artifacts_truncated: bool,
    /// Stable Artifact selection keyed by Artifact ID.
    pub selected_artifact: Option<String>,
    /// Entries for the selected conversation Artifact.
    pub conversation_entries: Vec<ConversationEntry>,
    pub conversation_entries_truncated: bool,
    pub pane_focus: PaneFocus,
    pub inspector: Option<ArtifactInspector>,
    /// Active v1.1 operator-view tab.
    pub active_tab: OperatorTab,
    pub operator_overview: Option<OperatorOverview>,
    pub agent_invocations: Vec<OperatorAgentInvocation>,
    pub selected_invocation: Option<String>,
    pub agent_following: bool,
    pub newer_invocations: usize,
    pub agent_detail_scroll: u16,
    pub operator_environment: Option<OperatorEnvironment>,
    pub environment_timeline: Vec<OperatorTimelineEntry>,
    pub environment_raw: bool,
    pub environment_scroll: u16,
    /// Last definitive Runtime Host HTTP response receipt.
    pub last_host_response: Option<Instant>,
    /// Thresholds used to derive Runtime Host liveness.
    pub liveness_thresholds: LivenessThresholds,
    pub cancellation: CancellationState,
    /// Last observed terminal size.
    pub last_size: (u16, u16),
    should_quit: bool,
    submitted: bool,
    review_request_id: Option<String>,
    review_intent_snapshot: Option<String>,
    outbox: Vec<HostCommand>,
    session_state_file: SessionStateFile,
    recovered_state: Option<OwnerSessionState>,
    cancellation_deadline: Option<Instant>,
    cancellation_origin: Option<CancellationOrigin>,
    cancellation_submitting: bool,
    cancellation_request_id: Option<String>,
    clean_exit_action: Option<CleanExitAction>,
    operator_cursors: [Option<String>; 4],
    operator_request_sequence: u64,
    operator_latest_requests: [u64; 4],
    clock: Arc<dyn Clock>,
}

impl App {
    /// Create a console that immediately starts the host handshake.
    pub fn new(host_addr: String) -> Self {
        Self::new_with_session_file(host_addr, SessionStateFile::default_path())
    }

    pub fn new_with_session_file(host_addr: String, session_state_file: SessionStateFile) -> Self {
        Self::new_with_session_file_and_clock(host_addr, session_state_file, Arc::new(SystemClock))
    }

    pub fn new_with_session_file_and_clock(
        host_addr: String,
        session_state_file: SessionStateFile,
        clock: Arc<dyn Clock>,
    ) -> Self {
        let recovered_state = session_state_file
            .load()
            .ok()
            .flatten()
            .filter(|state| state.host_authority == host_addr && state.host_api_major == 1);
        let session = recovered_state
            .as_ref()
            .map_or_else(ConsoleSession::generate, |state| ConsoleSession {
                session_id: state.console_session_id.clone(),
                credential: state.credential.clone(),
            });
        App {
            state: AppState::Connecting,
            host_addr,
            session,
            intent: String::new(),
            cursor: 0,
            hint: None,
            notice: None,
            health: None,
            activation: None,
            run: None,
            activities: Vec::new(),
            observations: Vec::new(),
            narrative: None,
            activities_truncated: false,
            observations_truncated: false,
            selected_activity: None,
            artifacts: Vec::new(),
            artifacts_truncated: false,
            selected_artifact: None,
            conversation_entries: Vec::new(),
            conversation_entries_truncated: false,
            pane_focus: PaneFocus::default(),
            inspector: None,
            active_tab: OperatorTab::default(),
            operator_overview: None,
            agent_invocations: Vec::new(),
            selected_invocation: None,
            agent_following: true,
            newer_invocations: 0,
            agent_detail_scroll: 0,
            operator_environment: None,
            environment_timeline: Vec::new(),
            environment_raw: false,
            environment_scroll: 0,
            last_host_response: None,
            liveness_thresholds: LivenessThresholds::default(),
            cancellation: CancellationState::Idle,
            last_size: (MIN_WIDTH, MIN_HEIGHT),
            should_quit: false,
            submitted: false,
            review_request_id: None,
            review_intent_snapshot: None,
            outbox: vec![HostCommand::Connect],
            session_state_file,
            recovered_state,
            cancellation_deadline: None,
            cancellation_origin: None,
            cancellation_submitting: false,
            cancellation_request_id: None,
            clean_exit_action: None,
            operator_cursors: [None, None, None, None],
            operator_request_sequence: 0,
            operator_latest_requests: [0, 0, 0, 0],
            clock,
        }
    }

    pub fn set_session_state_file(&mut self, file: SessionStateFile) {
        self.session_state_file = file;
    }

    /// Override liveness thresholds, primarily for deterministic tests.
    pub fn with_liveness_thresholds(mut self, thresholds: LivenessThresholds) -> Self {
        self.liveness_thresholds = thresholds;
        self
    }

    /// Classify the Runtime Host connection using successful response receipt.
    pub fn liveness(&self) -> Liveness {
        let Some(last_response) = self.last_host_response else {
            return Liveness::Live;
        };
        let elapsed = self.clock.now().saturating_duration_since(last_response);
        if elapsed >= self.liveness_thresholds.offline {
            Liveness::Offline
        } else if elapsed >= self.liveness_thresholds.stale {
            Liveness::Stale
        } else {
            Liveness::Live
        }
    }

    /// Whether the current Run matches this Console Session's ownership record.
    pub fn ownership_available(&self) -> bool {
        let Some(run) = self.run.as_ref() else {
            return false;
        };
        self.activation
            .as_ref()
            .is_some_and(|activation| activation.mission_run_id == run.mission_run_id)
            || self
                .recovered_state
                .as_ref()
                .is_some_and(|owner| owner.mission_run_id == run.mission_run_id)
    }

    /// Whether Host connectivity and Console Session ownership permit mutations.
    pub fn mutations_enabled(&self) -> bool {
        self.liveness() == Liveness::Live && self.ownership_available()
    }

    /// Whether the connected Runtime Host advertises the v1.1 operator view.
    pub fn operator_view_available(&self) -> bool {
        self.health
            .as_ref()
            .is_some_and(|health| health.api_version.major == 1 && health.api_version.minor >= 1)
    }

    /// Whether the console is deliberately retaining the v1.0 dashboard.
    pub fn legacy_view(&self) -> bool {
        self.health
            .as_ref()
            .is_some_and(|health| health.api_version.major == 1 && health.api_version.minor < 1)
    }

    /// Resolve the stable activity id selection to its current index and item.
    pub fn selected_activity(&self) -> Option<(usize, &RunActivity)> {
        let selected = self.selected_activity.as_deref()?;
        self.activities
            .iter()
            .enumerate()
            .find(|(_, activity)| activity.activity_id == selected)
    }

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
        self.agent_invocations
            .iter()
            .enumerate()
            .find(|(_, invocation)| invocation.stable_id == selected)
    }

    /// Return observations linked by the selected activity projection.
    pub fn selected_observations(&self) -> Vec<&ObservationEnvelope> {
        let Some((_, activity)) = self.selected_activity() else {
            return Vec::new();
        };
        self.observations
            .iter()
            .filter(|observation| {
                activity
                    .observation_sequences
                    .contains(&observation.observation_sequence)
            })
            .collect()
    }

    pub fn recovered_owner(&self) -> bool {
        self.recovered_state.is_some()
    }

    pub fn take_clean_exit_action(&mut self) -> Option<CleanExitAction> {
        self.clean_exit_action.take()
    }

    pub fn check_deadlines(&mut self) {
        if self
            .cancellation_deadline
            .is_some_and(|deadline| self.clock.now() >= deadline)
        {
            self.cancellation_deadline = None;
            if self.cancellation_origin == Some(CancellationOrigin::CleanExit) {
                self.clean_exit_action = Some(CleanExitAction::CancellationTimedOut);
            }
        }
    }

    /// Whether the run loop should exit.
    pub fn should_quit(&self) -> bool {
        self.should_quit
    }

    /// Drain pending host effects.
    pub fn take_commands(&mut self) -> Vec<HostCommand> {
        std::mem::take(&mut self.outbox)
    }

    /// The state that logically receives host messages, unwrapping a
    /// resize overlay so polling continues while the terminal is too small.
    fn logical_state_mut(&mut self) -> &mut AppState {
        match self.state {
            AppState::ResizeRequired { ref mut resume } => resume,
            ref mut other => other,
        }
    }

    /// The logical state name ignoring a resize overlay.
    pub fn logical_state_name(&self) -> &'static str {
        match &self.state {
            AppState::ResizeRequired { resume } => resume.name(),
            other => other.name(),
        }
    }

    /// The Activation Request ID assigned to the current review, if any.
    pub fn review_request_id(&self) -> Option<&str> {
        self.review_request_id.as_deref()
    }

    /// Test hook: pin the review request id for deterministic frame fixtures.
    #[doc(hidden)]
    pub fn pin_review_request_id(&mut self, id: &str) {
        self.review_request_id = Some(id.to_string());
    }

    /// Cursor position as (line, column) character offsets.
    pub fn cursor_line_col(&self) -> (usize, usize) {
        let before: Vec<&str> = self.intent[..self.byte_cursor()].split('\n').collect();
        (
            before.len() - 1,
            before.last().map_or(0, |l| l.chars().count()),
        )
    }

    /// Byte index of the cursor (cursor counts chars).
    fn byte_cursor(&self) -> usize {
        self.intent
            .char_indices()
            .nth(self.cursor)
            .map_or(self.intent.len(), |(i, _)| i)
    }

    /// Insert one character at the cursor.
    fn insert_char(&mut self, c: char) {
        let at = self.byte_cursor();
        self.intent.insert(at, c);
        self.cursor += 1;
    }

    /// Handle a keyboard event.
    pub fn handle_key(&mut self, key: KeyEvent) {
        if key.kind != KeyEventKind::Press {
            return;
        }
        if key.modifiers.contains(KeyModifiers::CONTROL)
            && matches!(key.code, KeyCode::Char('c') | KeyCode::Char('q'))
        {
            self.should_quit = true;
            return;
        }
        match self.state.clone() {
            AppState::ResizeRequired { .. } | AppState::Connecting | AppState::Submitting => {}
            AppState::Editing => self.handle_editing_key(key),
            AppState::ReviewActivation => self.handle_review_key(key),
            AppState::Run => self.handle_run_key(key),
            AppState::Error { retry_connect, .. } => match key.code {
                KeyCode::Esc => self.state = AppState::Editing,
                KeyCode::Char('r') if retry_connect => {
                    self.state = AppState::Connecting;
                    self.outbox.push(HostCommand::Connect);
                }
                _ => {}
            },
        }
    }

    fn handle_run_key(&mut self, key: KeyEvent) {
        if self.cancellation == CancellationState::Idle && self.inspector.is_some() {
            match key.code {
                KeyCode::Right | KeyCode::Char('n') => self.next_artifact_page(),
                KeyCode::Left | KeyCode::Char('p') => self.previous_artifact_page(),
                KeyCode::Esc => self.inspector = None,
                _ => {}
            }
            return;
        }
        if self.cancellation == CancellationState::Idle
            && self.operator_view_available()
            && self.handle_operator_key(key)
        {
            return;
        }
        match (&self.cancellation, key.code) {
            (CancellationState::Idle, KeyCode::Tab | KeyCode::BackTab) => {
                self.pane_focus = match self.pane_focus {
                    PaneFocus::Activities => PaneFocus::Artifacts,
                    PaneFocus::Artifacts => PaneFocus::Activities,
                };
            }
            (CancellationState::Idle, KeyCode::Up | KeyCode::Char('k')) => match self.pane_focus {
                PaneFocus::Activities => self.move_activity_selection(-1),
                PaneFocus::Artifacts => self.move_artifact_selection(-1),
            },
            (CancellationState::Idle, KeyCode::Down | KeyCode::Char('j')) => {
                match self.pane_focus {
                    PaneFocus::Activities => self.move_activity_selection(1),
                    PaneFocus::Artifacts => self.move_artifact_selection(1),
                }
            }
            (CancellationState::Idle, KeyCode::Enter)
                if self.pane_focus == PaneFocus::Artifacts =>
            {
                self.open_artifact_inspector();
            }
            (CancellationState::Idle, KeyCode::Char('c')) => {
                if !self.require_mutations_enabled() {
                    return;
                }
                self.cancellation = CancellationState::Confirming;
                self.cancellation_origin = Some(CancellationOrigin::ContinueConsole);
            }
            (CancellationState::Idle, KeyCode::Char('q')) => {
                if self
                    .run
                    .as_ref()
                    .is_some_and(|run| is_terminal(&run.status))
                {
                    if let Err(error) = self.session_state_file.remove() {
                        self.notice = Some(format!("Could not remove owner session: {error}"));
                    } else {
                        self.clean_exit_action = Some(CleanExitAction::TerminalRun);
                    }
                } else {
                    if !self.require_mutations_enabled() {
                        return;
                    }
                    self.cancellation = CancellationState::Confirming;
                    self.cancellation_origin = Some(CancellationOrigin::CleanExit);
                }
            }
            (CancellationState::Confirming, KeyCode::Esc) => {
                self.cancellation = CancellationState::Idle;
                self.cancellation_origin = None;
                self.cancellation_request_id = None;
            }
            (CancellationState::Confirming, KeyCode::Enter) if !self.cancellation_submitting => {
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
                    self.cancellation_deadline = Some(self.clock.now() + CANCELLATION_POLL_LIMIT);
                }
            }
            _ => {}
        }
    }

    fn handle_operator_key(&mut self, key: KeyEvent) -> bool {
        let tab = match key.code {
            KeyCode::Char('1') => Some(OperatorTab::Overview),
            KeyCode::Char('2') => Some(OperatorTab::Agents),
            KeyCode::Char('3') => Some(OperatorTab::Environment),
            KeyCode::Char('4') => Some(OperatorTab::Artifacts),
            KeyCode::Tab => Some(self.active_tab.next()),
            KeyCode::BackTab => Some(self.active_tab.previous()),
            _ => None,
        };
        if let Some(tab) = tab {
            self.active_tab = tab;
            self.request_operator_section();
            return true;
        }
        match (self.active_tab, key.code) {
            (OperatorTab::Agents, KeyCode::Up | KeyCode::Char('k')) => {
                self.move_invocation_selection(-1);
                true
            }
            (OperatorTab::Agents, KeyCode::Down | KeyCode::Char('j')) => {
                self.move_invocation_selection(1);
                true
            }
            (OperatorTab::Agents, KeyCode::PageUp) => {
                self.agent_detail_scroll = self.agent_detail_scroll.saturating_sub(5);
                true
            }
            (OperatorTab::Agents, KeyCode::PageDown) => {
                self.agent_detail_scroll = self.agent_detail_scroll.saturating_add(5);
                true
            }
            (OperatorTab::Agents, KeyCode::Char('f')) => {
                self.agent_following = true;
                self.newer_invocations = 0;
                self.selected_invocation = self
                    .agent_invocations
                    .last()
                    .map(|invocation| invocation.stable_id.clone());
                self.agent_detail_scroll = 0;
                true
            }
            (OperatorTab::Environment, KeyCode::Up | KeyCode::Char('k')) => {
                self.environment_scroll = self
                    .environment_scroll
                    .saturating_add(1)
                    .min(self.environment_timeline.len().saturating_sub(1) as u16);
                true
            }
            (OperatorTab::Environment, KeyCode::Down | KeyCode::Char('j')) => {
                self.environment_scroll = self.environment_scroll.saturating_sub(1);
                true
            }
            (OperatorTab::Environment, KeyCode::Char('r')) => {
                self.environment_raw = !self.environment_raw;
                self.environment_timeline.clear();
                self.environment_scroll = 0;
                self.operator_cursors[OperatorTab::Environment.index()] = None;
                self.request_operator_section();
                true
            }
            (OperatorTab::Artifacts, KeyCode::Up | KeyCode::Char('k')) => {
                self.move_artifact_selection(-1);
                true
            }
            (OperatorTab::Artifacts, KeyCode::Down | KeyCode::Char('j')) => {
                self.move_artifact_selection(1);
                true
            }
            (OperatorTab::Artifacts, KeyCode::Enter) => {
                self.open_artifact_inspector();
                true
            }
            _ => false,
        }
    }

    fn move_invocation_selection(&mut self, delta: isize) {
        if self.agent_invocations.is_empty() {
            return;
        }
        self.agent_following = false;
        let current = self.selected_invocation().map_or(0, |(index, _)| index);
        let next = if delta < 0 {
            current.saturating_sub(delta.unsigned_abs())
        } else {
            current
                .saturating_add(delta as usize)
                .min(self.agent_invocations.len() - 1)
        };
        self.selected_invocation = Some(self.agent_invocations[next].stable_id.clone());
        self.agent_detail_scroll = 0;
    }

    fn require_mutations_enabled(&mut self) -> bool {
        if self.mutations_enabled() {
            true
        } else {
            self.notice = Some(
                "Mutation controls disabled while the Host connection is stale or offline"
                    .to_string(),
            );
            false
        }
    }

    fn move_activity_selection(&mut self, delta: isize) {
        if self.activities.is_empty() {
            return;
        }
        let current = self.selected_activity().map_or(0, |(index, _)| index);
        let next = if delta < 0 {
            current.saturating_sub(delta.unsigned_abs())
        } else {
            current
                .saturating_add(delta as usize)
                .min(self.activities.len() - 1)
        };
        self.selected_activity = Some(self.activities[next].activity_id.clone());
    }

    fn move_artifact_selection(&mut self, delta: isize) {
        if self.artifacts.is_empty() {
            return;
        }
        let current = self.selected_artifact().map_or(0, |(index, _)| index);
        let next = if delta < 0 {
            current.saturating_sub(delta.unsigned_abs())
        } else {
            current
                .saturating_add(delta as usize)
                .min(self.artifacts.len() - 1)
        };
        let next_id = self.artifacts[next].artifact_id.clone();
        if self.selected_artifact.as_deref() != Some(next_id.as_str()) {
            self.selected_artifact = Some(next_id);
            self.sync_selected_conversation();
        }
    }

    fn sync_selected_conversation(&mut self) {
        let selected = self.selected_artifact().map(|(_, artifact)| {
            (
                artifact.artifact_id.clone(),
                artifact.classification.clone(),
            )
        });
        if let Some((artifact_id, classification)) = selected
            && classification == "conversation"
            && let Some(run) = self.run.as_ref()
        {
            self.outbox.push(HostCommand::FetchConversationEntries {
                mission_run_id: run.mission_run_id.clone(),
                artifact_id,
            });
        } else {
            self.conversation_entries.clear();
            self.conversation_entries_truncated = false;
        }
    }

    fn open_artifact_inspector(&mut self) {
        let Some((_, artifact)) = self.selected_artifact() else {
            return;
        };
        if !matches!(artifact.classification.as_str(), "text" | "binary") {
            return;
        }
        let artifact_id = artifact.artifact_id.clone();
        let classification = artifact.classification.clone();
        let Some(run) = self.run.as_ref() else {
            return;
        };
        self.inspector = Some(ArtifactInspector {
            artifact_id: artifact_id.clone(),
            classification,
            offset: 0,
            previous_offsets: Vec::new(),
            page: None,
        });
        self.outbox.push(HostCommand::FetchArtifactContent {
            mission_run_id: run.mission_run_id.clone(),
            artifact_id,
            offset: 0,
        });
    }

    fn next_artifact_page(&mut self) {
        let Some(inspector) = self.inspector.as_mut() else {
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
        let Some(run) = self.run.as_ref() else {
            return;
        };
        inspector.previous_offsets.push(inspector.offset);
        inspector.offset = next_offset;
        inspector.page = None;
        self.outbox.push(HostCommand::FetchArtifactContent {
            mission_run_id: run.mission_run_id.clone(),
            artifact_id: inspector.artifact_id.clone(),
            offset: next_offset,
        });
    }

    fn previous_artifact_page(&mut self) {
        let Some(inspector) = self.inspector.as_mut() else {
            return;
        };
        let Some(previous_offset) = inspector.previous_offsets.pop() else {
            return;
        };
        let Some(run) = self.run.as_ref() else {
            inspector.previous_offsets.push(previous_offset);
            return;
        };
        inspector.offset = previous_offset;
        inspector.page = None;
        self.outbox.push(HostCommand::FetchArtifactContent {
            mission_run_id: run.mission_run_id.clone(),
            artifact_id: inspector.artifact_id.clone(),
            offset: previous_offset,
        });
    }

    fn handle_editing_key(&mut self, key: KeyEvent) {
        self.hint = None;
        let bare = key.modifiers.is_empty();
        let shift = key.modifiers == KeyModifiers::SHIFT;
        match key.code {
            KeyCode::Enter
                if key.modifiers.contains(KeyModifiers::ALT)
                    || key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                self.open_review();
            }
            KeyCode::Enter if bare => self.insert_char('\n'),
            KeyCode::Char(c) if bare || shift => self.insert_char(c),
            KeyCode::Backspace if bare => {
                if self.cursor > 0 {
                    self.cursor -= 1;
                    let at = self.byte_cursor();
                    self.intent.remove(at);
                }
            }
            KeyCode::Delete if bare => {
                if self.cursor < self.intent.chars().count() {
                    let at = self.byte_cursor();
                    self.intent.remove(at);
                }
            }
            KeyCode::Left if bare => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right if bare => {
                self.cursor = (self.cursor + 1).min(self.intent.chars().count());
            }
            KeyCode::Home if bare => {
                let (line, _) = self.cursor_line_col();
                self.cursor = self.line_col_to_cursor(line, 0);
            }
            KeyCode::End if bare => {
                let (line, _) = self.cursor_line_col();
                let col = self
                    .intent
                    .split('\n')
                    .nth(line)
                    .map_or(0, |l| l.chars().count());
                self.cursor = self.line_col_to_cursor(line, col);
            }
            KeyCode::Up if bare => {
                let (line, col) = self.cursor_line_col();
                if line > 0 {
                    let col = col.min(
                        self.intent
                            .split('\n')
                            .nth(line - 1)
                            .map_or(0, |l| l.chars().count()),
                    );
                    self.cursor = self.line_col_to_cursor(line - 1, col);
                }
            }
            KeyCode::Down if bare => {
                let (line, col) = self.cursor_line_col();
                let lines = self.intent.split('\n').count();
                if line + 1 < lines {
                    let col = col.min(
                        self.intent
                            .split('\n')
                            .nth(line + 1)
                            .map_or(0, |l| l.chars().count()),
                    );
                    self.cursor = self.line_col_to_cursor(line + 1, col);
                }
            }
            _ => {}
        }
    }

    /// Convert (line, column) character offsets to a flat cursor index.
    fn line_col_to_cursor(&self, line: usize, col: usize) -> usize {
        let mut cursor = 0;
        for (n, l) in self.intent.split('\n').enumerate() {
            if n == line {
                return cursor + col.min(l.chars().count());
            }
            cursor += l.chars().count() + 1;
        }
        cursor
    }

    fn open_review(&mut self) {
        if self.intent.trim().is_empty() {
            self.hint = Some("Mission Intent is empty - nothing to review".to_string());
            return;
        }
        if self.review_intent_snapshot.as_deref() != Some(self.intent.as_str()) {
            self.review_request_id = Some(uuid::Uuid::new_v4().to_string());
            self.review_intent_snapshot = Some(self.intent.clone());
        }
        self.submitted = false;
        self.state = AppState::ReviewActivation;
    }

    fn handle_review_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => self.state = AppState::Editing,
            KeyCode::Enter => self.confirm_submit(),
            _ => {}
        }
    }

    /// Confirm the review exactly once and submit the activation.
    fn confirm_submit(&mut self) {
        if self.submitted {
            return;
        }
        self.submitted = true;
        let request = ActivationRequest {
            activation_request_id: self
                .review_request_id
                .clone()
                .expect("review always assigns a request id"),
            console_session_id: self.session.session_id.clone(),
            mission_intent: self.intent.clone(),
            source_authority: SOURCE_AUTHORITY.to_string(),
        };
        self.state = AppState::Submitting;
        self.outbox.push(HostCommand::Submit {
            request: Box::new(request),
            credential: self.session.credential.clone(),
        });
    }

    /// Handle a terminal resize, gating on [`MIN_WIDTH`]x[`MIN_HEIGHT`].
    pub fn handle_resize(&mut self, width: u16, height: u16) {
        self.last_size = (width, height);
        let too_small = width < MIN_WIDTH || height < MIN_HEIGHT;
        match (&self.state, too_small) {
            (AppState::ResizeRequired { .. }, true) => {}
            (AppState::ResizeRequired { .. }, false) => {
                if let AppState::ResizeRequired { resume } =
                    std::mem::replace(&mut self.state, AppState::Connecting)
                {
                    self.state = *resume;
                }
            }
            (_, true) => {
                let resume = std::mem::replace(&mut self.state, AppState::Connecting);
                self.state = AppState::ResizeRequired {
                    resume: Box::new(resume),
                };
            }
            (_, false) => {}
        }
    }

    /// Ask for a Mission Run poll; only meaningful in the Run state.
    pub fn request_poll(&mut self) {
        let within_cancellation_limit = self
            .cancellation_deadline
            .is_none_or(|deadline| self.clock.now() <= deadline);
        if matches!(self.logical_state_name(), "Run") && within_cancellation_limit {
            self.outbox.push(HostCommand::PollCurrent {
                credential: self.session.credential.clone(),
            });
            if let Some(run) = self.run.as_ref() {
                let mission_run_id = run.mission_run_id.clone();
                if self.operator_view_available() {
                    self.request_operator_section();
                } else {
                    self.outbox.push(HostCommand::FetchActivities {
                        mission_run_id: mission_run_id.clone(),
                    });
                    self.outbox.push(HostCommand::FetchObservations {
                        mission_run_id: mission_run_id.clone(),
                    });
                    self.outbox.push(HostCommand::FetchNarrative {
                        mission_run_id: mission_run_id.clone(),
                    });
                    self.outbox.push(HostCommand::FetchArtifacts {
                        mission_run_id: mission_run_id.clone(),
                    });
                }
                if self.active_tab == OperatorTab::Artifacts
                    && let Some((_, artifact)) = self.selected_artifact()
                    && artifact.classification == "conversation"
                {
                    self.outbox.push(HostCommand::FetchConversationEntries {
                        mission_run_id: mission_run_id.clone(),
                        artifact_id: artifact.artifact_id.clone(),
                    });
                }
                if let Some(inspector) = self.inspector.as_ref() {
                    self.outbox.push(HostCommand::FetchArtifactContent {
                        mission_run_id,
                        artifact_id: inspector.artifact_id.clone(),
                        offset: inspector.offset,
                    });
                }
            }
        }
    }

    fn request_operator_section(&mut self) {
        let Some(run) = self.run.as_ref() else {
            return;
        };
        let index = self.active_tab.index();
        self.operator_request_sequence = self.operator_request_sequence.saturating_add(1);
        let request_id = self.operator_request_sequence;
        self.operator_latest_requests[index] = request_id;
        self.outbox.push(HostCommand::FetchOperatorView {
            mission_run_id: run.mission_run_id.clone(),
            section: self.active_tab.section(),
            cursor: self.operator_cursors[index].clone(),
            raw: self.environment_raw,
            request_id,
        });
    }

    /// Handle a response from the host worker thread.
    pub fn handle_host_message(&mut self, message: HostMessage) {
        if host_message_proves_response(&message) {
            self.last_host_response = Some(self.clock.now());
        }
        match message {
            HostMessage::Connected(Ok(health)) => {
                if health.api_version.major == 1 {
                    self.health = Some(health);
                    if let Some(owner) = self.recovered_state.as_ref() {
                        self.outbox.push(HostCommand::FetchIntent {
                            mission_run_id: owner.mission_run_id.clone(),
                            credential: owner.credential.clone(),
                        });
                        self.outbox.push(HostCommand::PollCurrent {
                            credential: owner.credential.clone(),
                        });
                    } else {
                        *self.logical_state_mut() = AppState::Editing;
                    }
                } else {
                    *self.logical_state_mut() = AppState::Error {
                        message: format!(
                            "Incompatible Runtime Host API v{}.{} (console requires major version 1)",
                            health.api_version.major, health.api_version.minor
                        ),
                        retry_connect: true,
                    };
                }
            }
            HostMessage::Connected(Err(error)) => {
                *self.logical_state_mut() = AppState::Error {
                    message: format!("Cannot reach Runtime Host at {}: {error}", self.host_addr),
                    retry_connect: true,
                };
            }
            HostMessage::Activated(Ok(ActivationOutcome::Accepted(accepted))) => {
                self.run = Some(RunRecord {
                    mission_id: accepted.mission_id.clone(),
                    mission_run_id: accepted.mission_run_id.clone(),
                    status: accepted.status.clone(),
                    created_at: Some(accepted.created_at.clone()),
                    started_at: None,
                    finished_at: None,
                    terminal_classification: None,
                });
                self.activation = Some(accepted);
                self.notice = None;
                if let (Some(health), Some(run)) = (self.health.as_ref(), self.run.as_ref()) {
                    let owner = OwnerSessionState {
                        host_authority: self.host_addr.clone(),
                        host_api_major: health.api_version.major,
                        mission_run_id: run.mission_run_id.clone(),
                        console_session_id: self.session.session_id.clone(),
                        credential: self.session.credential.clone(),
                    };
                    if let Err(error) = self.session_state_file.save(&owner) {
                        self.notice = Some(format!("Could not persist owner session: {error}"));
                    }
                }
                *self.logical_state_mut() = AppState::Run;
                self.request_poll();
            }
            HostMessage::Activated(Ok(ActivationOutcome::Rejected { code, message })) => {
                *self.logical_state_mut() = AppState::Error {
                    message: format!("Activation rejected ({code}): {message}"),
                    retry_connect: false,
                };
            }
            HostMessage::Activated(Err(error)) => {
                *self.logical_state_mut() = AppState::Error {
                    message: format!("Activation failed: {error}"),
                    retry_connect: false,
                };
            }
            HostMessage::Current(Ok(current)) => {
                if let (Some(owner), Some(run)) =
                    (self.recovered_state.as_ref(), current.mission_run.as_ref())
                    && owner.mission_run_id != run.mission_run_id
                {
                    self.notice = Some(format!(
                        "Recovered owner run {} does not match Host current run {}",
                        owner.mission_run_id, run.mission_run_id
                    ));
                    return;
                }
                let run_changed = self.run.as_ref().map(|run| run.mission_run_id.as_str())
                    != current
                        .mission_run
                        .as_ref()
                        .map(|run| run.mission_run_id.as_str());
                if run_changed {
                    self.narrative = None;
                    self.reset_operator_view();
                }
                self.run = current.mission_run;
                self.update_evidence_notice();
                if self.recovered_state.is_some() && self.run.is_some() {
                    *self.logical_state_mut() = AppState::Run;
                }
                if self.cancellation_origin == Some(CancellationOrigin::CleanExit)
                    && self
                        .run
                        .as_ref()
                        .is_some_and(|run| run.status == "cancelled")
                {
                    if let Err(error) = self.session_state_file.remove() {
                        self.notice = Some(format!("Could not remove owner session: {error}"));
                    } else {
                        self.clean_exit_action = Some(CleanExitAction::Cancelled);
                        self.cancellation_deadline = None;
                    }
                } else if self.cancellation_origin == Some(CancellationOrigin::ContinueConsole)
                    && self
                        .run
                        .as_ref()
                        .is_some_and(|run| run.status == "cancelled")
                {
                    self.cancellation = CancellationState::Idle;
                    self.cancellation_origin = None;
                    self.cancellation_request_id = None;
                    self.cancellation_submitting = false;
                }
            }
            HostMessage::Current(Err(error)) => {
                self.notice = Some(format!(
                    "Host poll failed ({error}); showing last known state"
                ));
            }
            HostMessage::Intent(Ok(intent)) => {
                if self
                    .recovered_state
                    .as_ref()
                    .is_some_and(|owner| owner.mission_run_id == intent.mission_run_id)
                {
                    self.intent = intent.mission_intent;
                    self.cursor = self.intent.chars().count();
                }
            }
            HostMessage::Intent(Err(error)) => {
                *self.logical_state_mut() = AppState::Error {
                    message: format!("Owner recovery failed: {error}"),
                    retry_connect: false,
                };
            }
            HostMessage::Activities(Ok(page)) => {
                let selected = self.selected_activity.clone();
                self.activities = page.items;
                self.activities_truncated = page.truncated;
                self.selected_activity = selected
                    .filter(|id| {
                        self.activities
                            .iter()
                            .any(|activity| &activity.activity_id == id)
                    })
                    .or_else(|| {
                        self.activities
                            .first()
                            .map(|activity| activity.activity_id.clone())
                    });
                self.update_evidence_notice();
            }
            HostMessage::Activities(Err(error)) => {
                self.notice = Some(format!(
                    "Host evidence poll failed ({error}); showing last known state"
                ));
            }
            HostMessage::Observations(Ok(page)) => {
                self.observations = page.items;
                self.observations_truncated = page.truncated;
                self.update_evidence_notice();
            }
            HostMessage::Observations(Err(error)) => {
                self.notice = Some(format!(
                    "Host evidence poll failed ({error}); showing last known state"
                ));
            }
            HostMessage::Narrative {
                mission_run_id,
                result: Ok(response),
            } => {
                if self.narrative_response_matches(&mission_run_id) {
                    self.narrative = Some(response.narrative);
                }
            }
            HostMessage::Narrative {
                mission_run_id,
                result: Err(error),
            } => {
                if self.narrative_response_matches(&mission_run_id) {
                    self.notice = Some(format!(
                        "Host evidence poll failed ({error}); showing last known state"
                    ));
                }
            }
            HostMessage::Artifacts(Ok(page)) => {
                let selected = self.selected_artifact.clone();
                self.artifacts = page.items;
                self.artifacts_truncated = page.truncated;
                self.selected_artifact = selected
                    .clone()
                    .filter(|id| {
                        self.artifacts
                            .iter()
                            .any(|artifact| &artifact.artifact_id == id)
                    })
                    .or_else(|| {
                        self.artifacts
                            .first()
                            .map(|artifact| artifact.artifact_id.clone())
                    });
                if self.selected_artifact != selected {
                    self.sync_selected_conversation();
                }
                self.update_evidence_notice();
            }
            HostMessage::Artifacts(Err(error)) => {
                self.notice = Some(format!(
                    "Host evidence poll failed ({error}); showing last known state"
                ));
            }
            HostMessage::OperatorView {
                mission_run_id,
                section,
                request_id,
                result,
            } => {
                self.reduce_operator_view(mission_run_id, section, request_id, result);
            }
            HostMessage::ArtifactContent(Ok(page)) => {
                if let Some(inspector) = self.inspector.as_mut()
                    && inspector.artifact_id == page.artifact_id
                    && inspector.offset == page.offset
                {
                    inspector.page = Some(page);
                }
            }
            HostMessage::ArtifactContent(Err(HostError::NotFound { .. })) => {
                self.inspector = None;
                self.notice = Some("Artifact became unavailable".to_string());
            }
            HostMessage::ArtifactContent(Err(error)) => {
                self.notice = Some(format!(
                    "Host Artifact preview failed ({error}); showing last known state"
                ));
            }
            HostMessage::ConversationEntries {
                mission_run_id,
                artifact_id,
                result: Ok(page),
            } => {
                if self.conversation_response_matches(&mission_run_id, &artifact_id) {
                    self.conversation_entries = page.items;
                    self.conversation_entries_truncated = page.truncated;
                    self.update_evidence_notice();
                }
            }
            HostMessage::ConversationEntries {
                mission_run_id,
                artifact_id,
                result: Err(error),
            } => {
                if self.conversation_response_matches(&mission_run_id, &artifact_id) {
                    self.notice = Some(format!(
                        "Host evidence poll failed ({error}); showing last known state"
                    ));
                }
            }
            HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(accepted))) => {
                let Some(pending_request_id) = self.cancellation_request_id.as_deref() else {
                    return;
                };
                let current_run_id = self.run.as_ref().map(|run| run.mission_run_id.as_str());
                if current_run_id != Some(accepted.mission_run_id.as_str())
                    || pending_request_id != accepted.cancellation_request_id
                    || accepted.disposition != "cancellation_requested"
                {
                    self.cancellation_submitting = false;
                    self.cancellation = CancellationState::Idle;
                    self.cancellation_request_id = None;
                    if self.cancellation_origin == Some(CancellationOrigin::ContinueConsole) {
                        self.cancellation_origin = None;
                    }
                    self.notice = Some(
                        "Cancellation contract failure: Host acceptance did not match the current run and pending request"
                            .to_string(),
                    );
                    return;
                }
                let cancellation_request_id = self
                    .cancellation_request_id
                    .take()
                    .expect("validated pending cancellation request exists");
                self.cancellation_submitting = false;
                self.cancellation = CancellationState::Requested {
                    cancellation_request_id,
                };
                self.request_poll();
            }
            HostMessage::Cancelled(Ok(CancellationOutcome::Rejected { code, message })) => {
                self.cancellation_submitting = false;
                self.cancellation = CancellationState::Idle;
                self.cancellation_request_id = None;
                if self.cancellation_origin == Some(CancellationOrigin::ContinueConsole) {
                    self.cancellation_origin = None;
                }
                self.notice = Some(format!("Cancellation rejected ({code}): {message}"));
            }
            HostMessage::Cancelled(Err(error)) => {
                self.cancellation_submitting = false;
                self.cancellation = CancellationState::Idle;
                self.cancellation_request_id = None;
                if self.cancellation_origin == Some(CancellationOrigin::ContinueConsole) {
                    self.cancellation_origin = None;
                }
                self.notice = Some(format!("Cancellation failed: {error}"));
            }
        }
    }

    fn update_evidence_notice(&mut self) {
        let count = if self.observations_truncated {
            Some(self.observations.len())
        } else if self.activities_truncated {
            Some(self.activities.len())
        } else if self.artifacts_truncated {
            Some(self.artifacts.len())
        } else if self.conversation_entries_truncated {
            Some(self.conversation_entries.len())
        } else {
            None
        };
        self.notice = count.map(|count| {
            format!(
                "Showing the first {count} evidence entries; the Host retains the full timeline"
            )
        });
    }

    fn conversation_response_matches(&self, mission_run_id: &str, artifact_id: &str) -> bool {
        self.run
            .as_ref()
            .is_some_and(|run| run.mission_run_id == mission_run_id)
            && self.selected_artifact().is_some_and(|(_, artifact)| {
                artifact.classification == "conversation" && artifact.artifact_id == artifact_id
            })
    }

    fn narrative_response_matches(&self, mission_run_id: &str) -> bool {
        self.run
            .as_ref()
            .is_some_and(|run| run.mission_run_id == mission_run_id)
    }

    fn reset_operator_view(&mut self) {
        self.operator_overview = None;
        self.agent_invocations.clear();
        self.selected_invocation = None;
        self.agent_following = true;
        self.newer_invocations = 0;
        self.agent_detail_scroll = 0;
        self.operator_environment = None;
        self.environment_timeline.clear();
        self.environment_scroll = 0;
        self.artifacts.clear();
        self.selected_artifact = None;
        self.operator_cursors = [None, None, None, None];
        self.operator_latest_requests = [0, 0, 0, 0];
    }

    fn reduce_operator_view(
        &mut self,
        mission_run_id: String,
        section: OperatorSection,
        request_id: u64,
        result: Result<OperatorViewPage, HostError>,
    ) {
        let tab = match section {
            OperatorSection::Overview => OperatorTab::Overview,
            OperatorSection::Agents => OperatorTab::Agents,
            OperatorSection::Environment => OperatorTab::Environment,
            OperatorSection::Artifacts => OperatorTab::Artifacts,
        };
        let index = tab.index();
        if self.operator_latest_requests[index] != request_id
            || self
                .run
                .as_ref()
                .is_none_or(|run| run.mission_run_id != mission_run_id)
        {
            return;
        }
        let page = match result {
            Ok(page)
                if page.meta().mission_run_id == mission_run_id
                    && page.meta().section == section =>
            {
                page
            }
            Ok(_) => return,
            Err(error) => {
                self.notice = Some(format!(
                    "Host operator-view poll failed ({error}); showing last known state"
                ));
                return;
            }
        };
        self.operator_cursors[index] = Some(page.meta().next_cursor.clone());
        match page {
            OperatorViewPage::Overview(page) => {
                let mut page = *page;
                let mut retained = self
                    .operator_overview
                    .as_ref()
                    .map_or_else(Vec::new, |overview| overview.recent_events.clone());
                merge_timeline(&mut retained, page.overview.recent_events);
                page.overview.recent_events = retained;
                self.operator_overview = Some(page.overview);
            }
            OperatorViewPage::Agents(page) => self.merge_agent_invocations(page.agents),
            OperatorViewPage::Environment(page) => {
                let mut page = *page;
                if page.environment.raw != self.environment_raw {
                    return;
                }
                merge_timeline(&mut self.environment_timeline, page.environment.timeline);
                page.environment.timeline = self.environment_timeline.clone();
                self.operator_environment = Some(page.environment);
            }
            OperatorViewPage::Artifacts(page) => self.merge_operator_artifacts(page.artifacts),
        }
    }

    fn merge_agent_invocations(&mut self, incoming: Vec<OperatorAgentInvocation>) {
        let mut added = 0usize;
        for invocation in incoming {
            if let Some(existing) = self
                .agent_invocations
                .iter_mut()
                .find(|item| item.stable_id == invocation.stable_id)
            {
                *existing = invocation;
            } else {
                self.agent_invocations.push(invocation);
                added += 1;
            }
        }
        self.agent_invocations.sort_by(|left, right| {
            left.started_at
                .cmp(&right.started_at)
                .then_with(|| left.updated_at.cmp(&right.updated_at))
                .then_with(|| left.stable_id.cmp(&right.stable_id))
        });
        if self.agent_following {
            self.selected_invocation = self
                .agent_invocations
                .last()
                .map(|invocation| invocation.stable_id.clone());
            self.newer_invocations = 0;
        } else {
            self.newer_invocations = self.newer_invocations.saturating_add(added);
            if !self.agent_invocations.iter().any(|invocation| {
                self.selected_invocation.as_deref() == Some(invocation.stable_id.as_str())
            }) {
                self.selected_invocation = self
                    .agent_invocations
                    .first()
                    .map(|invocation| invocation.stable_id.clone());
            }
        }
    }

    fn merge_operator_artifacts(&mut self, incoming: Vec<ArtifactDescriptor>) {
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
            .filter(|id| {
                self.artifacts
                    .iter()
                    .any(|artifact| &artifact.artifact_id == id)
            })
            .or_else(|| {
                self.artifacts
                    .first()
                    .map(|artifact| artifact.artifact_id.clone())
            });
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

fn result_proves_response<T>(result: &Result<T, HostError>) -> bool {
    result
        .as_ref()
        .map_or_else(HostError::proves_host_reachable, |_| true)
}

fn host_message_proves_response(message: &HostMessage) -> bool {
    match message {
        HostMessage::Connected(result) => result_proves_response(result),
        HostMessage::Activated(result) => result_proves_response(result),
        HostMessage::Current(result) => result_proves_response(result),
        HostMessage::Intent(result) => result_proves_response(result),
        HostMessage::Activities(result) => result_proves_response(result),
        HostMessage::Observations(result) => result_proves_response(result),
        HostMessage::Narrative { result, .. } => result_proves_response(result),
        HostMessage::Artifacts(result) => result_proves_response(result),
        HostMessage::OperatorView { result, .. } => result_proves_response(result),
        HostMessage::ArtifactContent(result) => result_proves_response(result),
        HostMessage::ConversationEntries { result, .. } => result_proves_response(result),
        HostMessage::Cancelled(result) => result_proves_response(result),
    }
}

fn is_terminal(status: &str) -> bool {
    matches!(status, "succeeded" | "failed" | "cancelled")
}
