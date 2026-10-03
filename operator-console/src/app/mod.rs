//! Application state machine for the Operator Console.
//!
//! Pure presentation-layer logic: the app never performs IO itself. Keyboard
//! and resize events go in, host effects come out through an outbox drained by
//! the run loop, and host responses arrive back as [`HostMessage`] values.
//!
//! - [`launch`]: Launch screen (Stack Preset, toggles, preflight, intent
//!   editor, demo prompts, review, activation).
//! - [`run`]: Run screen tabs, operator-view polling and reducers, Artifact
//!   inspector, cancellation.
//! - [`stack`]: Stack tab service list and service-log tail.
//! - [`progress`]: Incremental progress hierarchy, selection, follow and search.
//! - [`world`]: Bounded off-thread image decoding and protocol encoding.

pub mod launch;
pub mod progress;
pub mod run;
pub mod stack;
pub mod world;

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};

pub use launch::{IntentEditor, LaunchField, LaunchState, WrappedIntent};
pub use run::{ArtifactInspector, RunTab, RunView};
pub use stack::{LogTail, StackView};

use crate::host::{
    ActivationAccepted, ActivationOutcome, CancellationOutcome, FrameSource, Health, HostCommand,
    HostError, HostMessage, OperatorSection, RunRecord, RunStack,
};

const CANCELLATION_POLL_LIMIT: Duration = Duration::from_secs(15);

pub trait Clock: Send + Sync + std::fmt::Debug {
    /// Monotonic time for deadlines and liveness.
    fn now(&self) -> Instant;
    /// Wall-clock seconds since the Unix epoch, for run elapsed time.
    fn unix_now(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_secs() as i64)
    }
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
    Detached,
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

/// Smallest supported terminal; the layout adapts above it (see
/// `ui::layout::Breakpoint`).
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
    /// The run is terminal and polling stopped after the final refresh, so
    /// response age says nothing about the Host.
    Idle,
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
    /// Launch screen: Stack Preset, toggles, preflight, Mission Intent.
    Launch,
    /// Operator is reviewing the Mission Intent and stack before activation.
    ReviewActivation,
    /// An activation POST is in flight; further submits are ignored.
    Submitting,
    /// Observing the current Mission Run.
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
            AppState::Launch => "Launch",
            AppState::ReviewActivation => "ReviewActivation",
            AppState::Submitting => "Submitting",
            AppState::Run => "Run",
            AppState::Error { .. } => "Error",
            AppState::ResizeRequired { .. } => "ResizeRequired",
        }
    }
}

/// Polls that get exactly one refresh after the run turns terminal.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) enum RefreshKey {
    Current,
    Section(OperatorSection),
    ServiceLog(String),
    Conversation(String),
    Frame(FrameSource),
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
    /// Compatible host health once connected.
    pub health: Option<Health>,
    /// Launch screen state; retained across runs so `e` keeps the preset.
    pub launch: LaunchState,
    /// Accepted activation, once submitted.
    pub activation: Option<ActivationAccepted>,
    /// Latest known Mission Run snapshot.
    pub run: Option<RunRecord>,
    /// Run screen tabs and section state.
    pub view: RunView,
    /// Transient hint shown in the footer.
    pub hint: Option<String>,
    /// Non-fatal notice shown in the footer (e.g. a lapsed poll).
    pub notice: Option<String>,
    /// Whether the `?` help overlay is open.
    pub help_open: bool,
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
    review_snapshot: Option<launch::ReviewSnapshot>,
    outbox: Vec<HostCommand>,
    session_state_file: SessionStateFile,
    recovered_state: Option<OwnerSessionState>,
    cancellation_deadline: Option<Instant>,
    cancellation_origin: Option<CancellationOrigin>,
    cancellation_submitting: bool,
    cancellation_request_id: Option<String>,
    clean_exit_action: Option<CleanExitAction>,
    /// `Some` once the run is terminal: polls already given their final refresh.
    final_refresh: Option<HashSet<RefreshKey>>,
    final_complete: HashSet<OperatorSection>,
    final_narrative_ready: bool,
    next_terminal_refresh: Option<Instant>,
    pub image_picker: Option<ratatui_image::picker::Picker>,
    media_clock_origin: Instant,
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
            health: None,
            launch: LaunchState::default(),
            activation: None,
            run: None,
            view: RunView::default(),
            hint: None,
            notice: None,
            help_open: false,
            last_host_response: None,
            liveness_thresholds: LivenessThresholds::default(),
            cancellation: CancellationState::Idle,
            last_size: (MIN_WIDTH, MIN_HEIGHT),
            should_quit: false,
            submitted: false,
            review_request_id: None,
            review_snapshot: None,
            outbox: vec![HostCommand::Connect],
            session_state_file,
            recovered_state,
            cancellation_deadline: None,
            cancellation_origin: None,
            cancellation_submitting: false,
            cancellation_request_id: None,
            clean_exit_action: None,
            final_refresh: None,
            final_complete: HashSet::new(),
            final_narrative_ready: false,
            next_terminal_refresh: None,
            image_picker: None,
            media_clock_origin: clock.now(),
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

    /// Wall-clock seconds since the Unix epoch from the app clock.
    pub fn unix_now(&self) -> i64 {
        self.clock.unix_now()
    }

    /// Classify the Runtime Host connection using successful response receipt.
    pub fn liveness(&self) -> Liveness {
        let Some(last_response) = self.last_host_response else {
            return Liveness::Live;
        };
        let elapsed = self.clock.now().saturating_duration_since(last_response);
        if elapsed < self.liveness_thresholds.stale {
            Liveness::Live
        } else if self.polling_stopped() {
            Liveness::Idle
        } else if elapsed >= self.liveness_thresholds.offline {
            Liveness::Offline
        } else {
            Liveness::Stale
        }
    }

    /// Whether run polling has stopped because the run is terminal.
    pub fn polling_stopped(&self) -> bool {
        self.final_narrative_ready
            && OperatorSection::ALL
                .iter()
                .all(|section| self.final_complete.contains(section))
            && self.logical_state_name() == "Run"
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

    pub fn recovered_owner(&self) -> bool {
        self.recovered_state.is_some()
    }

    pub fn take_clean_exit_action(&mut self) -> Option<CleanExitAction> {
        self.clean_exit_action.take()
    }

    /// Fire due deadlines: the clean-exit cancellation limit and the
    /// debounced Launch preflight.
    pub fn check_deadlines(&mut self) {
        let now = self.clock.now();
        if self
            .cancellation_deadline
            .is_some_and(|deadline| now >= deadline)
        {
            self.cancellation_deadline = None;
            if self.cancellation_origin == Some(CancellationOrigin::CleanExit) {
                self.clean_exit_action = Some(CleanExitAction::CancellationTimedOut);
            }
        }
        if let Some(command) = self.launch.take_due_preflight(now) {
            self.outbox.push(command);
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

    /// Handle a keyboard event.
    pub fn handle_key(&mut self, key: KeyEvent) {
        if key.kind != KeyEventKind::Press {
            return;
        }
        let control = key.modifiers.contains(KeyModifiers::CONTROL);
        if control && key.code == KeyCode::Char('q') {
            // Detach: leave any run untouched; the owner session survives
            // for recovery.
            self.should_quit = true;
            self.clean_exit_action = Some(CleanExitAction::Detached);
            return;
        }
        if control && key.code == KeyCode::Char('c') {
            self.handle_interrupt();
            return;
        }
        if self.help_open {
            if matches!(key.code, KeyCode::Esc | KeyCode::Char('?') | KeyCode::F(1)) {
                self.help_open = false;
            }
            return;
        }
        if self.logical_state_name() == "Run"
            && self.cancellation == CancellationState::Idle
            && !self.rejection_open()
            && self.view.tab == RunTab::Progress
            && self.view.progress.search_editing
            && self.view.progress.handle_key(key)
        {
            return;
        }
        if key.code == KeyCode::F(1) {
            self.help_open = true;
            return;
        }
        match self.state.clone() {
            AppState::ResizeRequired { .. } | AppState::Connecting | AppState::Submitting => {}
            AppState::Launch => self.handle_launch_key(key),
            AppState::ReviewActivation => self.handle_review_key(key),
            AppState::Run => self.handle_run_key(key),
            AppState::Error { retry_connect, .. } => match key.code {
                KeyCode::Esc if self.health.is_some() => self.enter_launch(),
                KeyCode::Char('r') if retry_connect => {
                    self.state = AppState::Connecting;
                    self.outbox.push(HostCommand::Connect);
                }
                KeyCode::Char('q') => self.should_quit = true,
                _ => {}
            },
        }
    }

    /// `Ctrl+C`: managed exit during an owned active run, otherwise quit.
    fn handle_interrupt(&mut self) {
        if self.logical_state_name() != "Run" {
            self.should_quit = true;
            return;
        }
        let terminal = self.run.as_ref().is_some_and(RunRecord::is_terminal);
        if terminal {
            self.exit_terminal_run();
        } else if !self.ownership_available() {
            self.should_quit = true;
        } else if !self.mutations_enabled() {
            self.notice = Some(
                "Host is stale or offline; wait to cancel, or Ctrl+Q to explicitly detach"
                    .to_string(),
            );
        } else if self.cancellation == CancellationState::Idle {
            self.cancellation = CancellationState::Confirming;
            self.cancellation_origin = Some(CancellationOrigin::CleanExit);
        }
    }

    /// Enter the Launch screen, loading presets if needed.
    pub(crate) fn enter_launch(&mut self) {
        *self.logical_state_mut() = AppState::Launch;
        if self.launch.presets.is_none() {
            self.outbox.push(HostCommand::FetchPresets);
        } else {
            self.launch
                .schedule_preflight(self.clock.now(), Duration::ZERO);
        }
    }

    /// Leave a terminal run for a fresh Launch with the same preset/toggles.
    pub(crate) fn start_new_intent(&mut self) {
        if let Err(error) = self.session_state_file.remove() {
            self.notice = Some(format!("Could not remove owner session: {error}"));
            return;
        }
        self.recovered_state = None;
        self.activation = None;
        self.run = None;
        self.view = RunView::default();
        self.configure_images(self.image_picker.clone());
        self.final_complete.clear();
        self.final_narrative_ready = false;
        self.next_terminal_refresh = None;
        self.final_refresh = None;
        self.cancellation = CancellationState::Idle;
        self.cancellation_origin = None;
        self.cancellation_request_id = None;
        self.cancellation_submitting = false;
        self.cancellation_deadline = None;
        self.notice = None;
        self.submitted = false;
        self.enter_launch();
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

    /// Mark one poll as given its post-terminal refresh. Returns whether the
    /// poll should be issued.
    pub(crate) fn claim_refresh(&mut self, key: RefreshKey) -> bool {
        match self.final_refresh.as_mut() {
            None => true,
            Some(done) => done.insert(key),
        }
    }

    /// Let a failed post-terminal refresh retry on the next tick.
    pub(crate) fn release_refresh(&mut self, key: &RefreshKey) {
        if let Some(done) = self.final_refresh.as_mut() {
            done.remove(key);
        }
    }

    /// Dispatch backpressure is local: release claims without changing Host
    /// liveness, and retain the request ID owned by an already queued poll.
    pub fn handle_dispatch(&mut self, dispatch: crate::host::workers::Dispatch) {
        use crate::host::workers::Dispatch;
        match dispatch {
            Dispatch::Dropped(command) => match command {
                HostCommand::FetchOperatorView {
                    mission_run_id,
                    section,
                    request_id,
                    ..
                } => {
                    let index = section.index();
                    if self
                        .run
                        .as_ref()
                        .is_some_and(|run| run.mission_run_id == mission_run_id)
                        && self.view.latest_requests[index] == request_id
                    {
                        self.view.pending_requests[index] = false;
                        self.release_refresh(&RefreshKey::Section(section));
                    }
                }
                HostCommand::PollCurrent { .. } => self.release_refresh(&RefreshKey::Current),
                HostCommand::FetchWorldFrame { source, .. } => {
                    self.release_refresh(&RefreshKey::Frame(source))
                }
                HostCommand::FetchConversationEntries { artifact_id, .. } => {
                    self.release_refresh(&RefreshKey::Conversation(artifact_id));
                }
                HostCommand::FetchArtifactContent {
                    purpose: crate::host::ContentPurpose::ServiceLog,
                    artifact_id,
                    offset,
                    ..
                } => {
                    self.release_refresh(&RefreshKey::ServiceLog(format!(
                        "{artifact_id}@{offset}"
                    )));
                }
                HostCommand::Preflight { request_id, .. } => self
                    .launch
                    .retry_dropped_preflight(request_id, self.clock.now()),
                // Startup and inspector reads have no periodic refresh claim.
                // Keep them in the outbox until a lane accepts them.
                command => self.outbox.push(command),
            },
            Dispatch::Coalesced {
                command,
                original_request_id: Some(original),
            } => match command {
                HostCommand::FetchOperatorView {
                    mission_run_id,
                    section,
                    request_id,
                    ..
                } => {
                    let index = section.index();
                    if self
                        .run
                        .as_ref()
                        .is_some_and(|run| run.mission_run_id == mission_run_id)
                        && self.view.latest_requests[index] == request_id
                    {
                        self.view.latest_requests[index] = original;
                    }
                }
                HostCommand::Preflight { request_id, .. } => {
                    self.launch.adopt_preflight_request(request_id, original)
                }
                _ => {}
            },
            _ => {}
        }
    }

    /// Handle a response from the workers.
    pub fn handle_host_message(&mut self, message: HostMessage) {
        if message.proves_host_response() {
            self.last_host_response = Some(self.clock.now());
        }
        match message {
            HostMessage::Connected(Ok(health)) => self.on_connected(health),
            HostMessage::Connected(Err(error)) => {
                *self.logical_state_mut() = AppState::Error {
                    message: format!("Cannot reach Runtime Host at {}: {error}", self.host_addr),
                    retry_connect: true,
                };
            }
            HostMessage::Presets(result) => self.on_presets(result),
            HostMessage::Preflight { request_id, result } => {
                self.launch.apply_preflight(request_id, result);
            }
            HostMessage::Activated(Ok(ActivationOutcome::Accepted(accepted))) => {
                self.on_activation_accepted(accepted);
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
            HostMessage::Current(Ok(current)) => self.on_current(current.mission_run),
            HostMessage::Current(Err(error)) => {
                self.release_refresh(&RefreshKey::Current);
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
                    self.launch.editor.set_text(intent.mission_intent);
                }
            }
            HostMessage::Intent(Err(error)) => {
                if self.recovered_state.is_some() {
                    *self.logical_state_mut() = AppState::Error {
                        message: format!("Owner recovery failed: {error}"),
                        retry_connect: false,
                    };
                }
            }
            HostMessage::Cancelled(result) => self.on_cancelled(result),
            HostMessage::OperatorView {
                mission_run_id,
                section,
                request_id,
                result,
            } => self.reduce_operator_view(&mission_run_id, section, request_id, result),
            HostMessage::ArtifactContent {
                purpose,
                mission_run_id,
                artifact_id,
                requested_offset,
                result,
            } => self.reduce_artifact_content(
                purpose,
                &mission_run_id,
                &artifact_id,
                requested_offset,
                result,
            ),
            HostMessage::ConversationEntries {
                mission_run_id,
                artifact_id,
                result,
            } => self.reduce_conversation_entries(&mission_run_id, &artifact_id, result),
            HostMessage::WorldFrame {
                mission_run_id,
                source,
                result,
            } => self.reduce_world_frame(&mission_run_id, source, result),
        }
    }

    fn on_connected(&mut self, health: Health) {
        if !health.api_version.is_supported() {
            let message = if health.api_version.major == 1 {
                format!(
                    "Runtime Host too old: API v{}.{} (console requires v1.{}+). Restart the Host from this checkout.",
                    health.api_version.major,
                    health.api_version.minor,
                    crate::host::REQUIRED_API_MINOR
                )
            } else {
                format!(
                    "Incompatible Runtime Host API v{}.{} (console requires major version 1)",
                    health.api_version.major, health.api_version.minor
                )
            };
            *self.logical_state_mut() = AppState::Error {
                message,
                retry_connect: true,
            };
            return;
        }
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
            self.enter_launch();
        }
    }

    fn on_activation_accepted(&mut self, accepted: ActivationAccepted) {
        let stack = self.launch.selection().map(|selection| RunStack {
            preset_id: selection.preset_id,
            airsim: selection.airsim,
            perception: selection.perception,
        });
        self.run = Some(RunRecord {
            mission_id: accepted.mission_id.clone(),
            mission_run_id: accepted.mission_run_id.clone(),
            status: accepted.status.clone(),
            created_at: Some(accepted.created_at.clone()),
            started_at: None,
            finished_at: None,
            terminal_classification: None,
            stack,
            terminal_detail: None,
        });
        self.activation = Some(accepted);
        self.notice = None;
        self.view = RunView::default();
        self.configure_images(self.image_picker.clone());
        self.final_complete.clear();
        self.final_narrative_ready = false;
        self.next_terminal_refresh = None;
        self.final_refresh = None;
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

    fn on_current(&mut self, mission_run: Option<RunRecord>) {
        if let Some(owner) = self.recovered_state.as_ref()
            && mission_run
                .as_ref()
                .is_none_or(|run| owner.mission_run_id != run.mission_run_id)
        {
            self.notice = Some(match mission_run.as_ref() {
                Some(run) => format!(
                    "Recovered owner run {} does not match Host current run {}; launch a new mission when available",
                    owner.mission_run_id, run.mission_run_id
                ),
                None => {
                    "Recovered owner run is no longer present; launch a new mission".to_string()
                }
            });
            // Discard only in-memory recovery authority. The persisted owner
            // record remains until a normal activation replaces it.
            self.recovered_state = None;
            self.run = None;
            self.enter_launch();
            return;
        }
        let run_changed = self.run.as_ref().map(|run| run.mission_run_id.as_str())
            != mission_run.as_ref().map(|run| run.mission_run_id.as_str());
        if run_changed {
            self.view = RunView::default();
            self.configure_images(self.image_picker.clone());
            self.final_complete.clear();
            self.final_narrative_ready = false;
            self.next_terminal_refresh = None;
            self.final_refresh = None;
        }
        self.run = mission_run;
        if self.run.as_ref().is_some_and(RunRecord::is_terminal) && self.final_refresh.is_none() {
            // The final refresh of every section is requested from here on.
            self.final_refresh = Some(HashSet::from([RefreshKey::Current]));
            self.next_terminal_refresh = Some(self.clock.now());
        }
        if self.recovered_state.is_some() && self.run.is_some() {
            *self.logical_state_mut() = AppState::Run;
        }
        let cancelled = self
            .run
            .as_ref()
            .is_some_and(|run| run.status == "cancelled");
        if cancelled && self.cancellation_origin == Some(CancellationOrigin::CleanExit) {
            if let Err(error) = self.session_state_file.remove() {
                self.notice = Some(format!("Could not remove owner session: {error}"));
            } else {
                self.clean_exit_action = Some(CleanExitAction::Cancelled);
                self.cancellation_deadline = None;
            }
        } else if cancelled && self.cancellation_origin == Some(CancellationOrigin::ContinueConsole)
        {
            self.reset_cancellation();
        }
    }

    fn reset_cancellation(&mut self) {
        self.cancellation = CancellationState::Idle;
        self.cancellation_origin = None;
        self.cancellation_request_id = None;
        self.cancellation_submitting = false;
    }

    fn on_cancelled(&mut self, result: Result<CancellationOutcome, HostError>) {
        let failure = match result {
            Ok(CancellationOutcome::Accepted(accepted)) => {
                let Some(pending_request_id) = self.cancellation_request_id.as_deref() else {
                    return;
                };
                let current_run_id = self.run.as_ref().map(|run| run.mission_run_id.as_str());
                if current_run_id == Some(accepted.mission_run_id.as_str())
                    && pending_request_id == accepted.cancellation_request_id
                    && accepted.disposition == "cancellation_requested"
                {
                    let cancellation_request_id = self
                        .cancellation_request_id
                        .take()
                        .expect("validated pending cancellation request exists");
                    self.cancellation_submitting = false;
                    self.cancellation = CancellationState::Requested {
                        cancellation_request_id,
                    };
                    self.request_poll();
                    return;
                }
                "Cancellation contract failure: Host acceptance did not match the current run and pending request".to_string()
            }
            Ok(CancellationOutcome::Rejected { code, message }) => {
                format!("Cancellation rejected ({code}): {message}")
            }
            Err(error) => format!("Cancellation failed: {error}"),
        };
        self.cancellation_submitting = false;
        self.cancellation = CancellationState::Idle;
        self.cancellation_request_id = None;
        if self.cancellation_origin == Some(CancellationOrigin::ContinueConsole) {
            self.cancellation_origin = None;
        }
        self.notice = Some(failure);
    }

    fn require_mutations_enabled(&mut self) -> bool {
        if self.mutations_enabled() {
            true
        } else {
            self.notice = Some(
                "Mutation controls disabled while the Host connection is stale or offline, or this console does not own the run"
                    .to_string(),
            );
            false
        }
    }

    /// `q` on a terminal run: forget the owner session and exit.
    fn exit_terminal_run(&mut self) {
        if let Err(error) = self.session_state_file.remove() {
            self.notice = Some(format!("Could not remove owner session: {error}"));
        } else {
            self.clean_exit_action = Some(CleanExitAction::TerminalRun);
        }
    }
}
