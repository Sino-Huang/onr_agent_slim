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
    ActivationOutcome, ActivationRequest, CancellationOutcome, CancellationRequest, CurrentRun,
    Health, HostError, MissionIntent, RunRecord,
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
    Cancelled(Result<CancellationOutcome, HostError>),
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
            clock,
        }
    }

    pub fn set_session_state_file(&mut self, file: SessionStateFile) {
        self.session_state_file = file;
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
        match (&self.cancellation, key.code) {
            (CancellationState::Idle, KeyCode::Char('c')) => {
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
        }
    }

    /// Handle a response from the host worker thread.
    pub fn handle_host_message(&mut self, message: HostMessage) {
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
                self.outbox.push(HostCommand::PollCurrent {
                    credential: self.session.credential.clone(),
                });
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
                self.run = current.mission_run;
                self.notice = None;
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
}

fn is_terminal(status: &str) -> bool {
    matches!(status, "succeeded" | "failed" | "cancelled")
}
