//! Application state machine for the Operator Console.
//!
//! Pure presentation-layer logic: the app never performs IO itself. Keyboard
//! and resize events go in, host effects come out through an outbox drained by
//! the run loop, and host responses arrive back as [`HostMessage`] values.

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};

use crate::host::{ActivationOutcome, ActivationRequest, CurrentRun, Health, HostError, RunRecord};

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
}

/// Responses from the host worker thread.
#[derive(Debug)]
pub enum HostMessage {
    Connected(Result<Health, HostError>),
    Activated(Result<ActivationOutcome, HostError>),
    Current(Result<CurrentRun, HostError>),
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
    /// Last observed terminal size.
    pub last_size: (u16, u16),
    should_quit: bool,
    submitted: bool,
    review_request_id: Option<String>,
    review_intent_snapshot: Option<String>,
    outbox: Vec<HostCommand>,
}

impl App {
    /// Create a console that immediately starts the host handshake.
    pub fn new(host_addr: String) -> Self {
        App {
            state: AppState::Connecting,
            host_addr,
            session: ConsoleSession::generate(),
            intent: String::new(),
            cursor: 0,
            hint: None,
            notice: None,
            health: None,
            activation: None,
            run: None,
            last_size: (MIN_WIDTH, MIN_HEIGHT),
            should_quit: false,
            submitted: false,
            review_request_id: None,
            review_intent_snapshot: None,
            outbox: vec![HostCommand::Connect],
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
            AppState::Run => {}
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
        if matches!(self.logical_state_name(), "Run") {
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
                    *self.logical_state_mut() = AppState::Editing;
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
                self.run = current.mission_run;
                self.notice = None;
            }
            HostMessage::Current(Err(error)) => {
                self.notice = Some(format!(
                    "Host poll failed ({error}); showing last known state"
                ));
            }
        }
    }
}
