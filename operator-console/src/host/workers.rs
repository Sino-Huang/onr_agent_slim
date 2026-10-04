//! Control, evidence, and media worker threads (D9).
//!
//! The UI thread never performs IO. It hands [`HostCommand`]s to
//! [`Workers::dispatch`], which routes each command to one of three std
//! threads through a bounded queue:
//!
//! - **control**: health, presets, preflight, activation, `/current`, run
//!   history, owner intent, cancellation, receipt export;
//! - **evidence**: operator-view sections, Artifact content, conversation
//!   entries;
//! - **media**: world-frame bytes.
//!
//! Identical polls are coalesced per key: while one is queued or in flight a
//! second identical poll is dropped. Mutations (activation, cancellation) are
//! never coalesced or dropped; if their queue is full they wait in a backlog
//! that [`Workers::flush_backlog`] retries. Results come back on one channel as
//! [`HostMessage`]s.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, Sender, SyncSender, TrySendError};
use std::thread::JoinHandle;
use std::time::Duration;

use parking_lot::Mutex;

use super::client::{HostClient, HostError};
use super::dto::{
    ActivationOutcome, ActivationRequest, ArtifactContentPage, CancellationOutcome,
    CancellationRequest, ConversationEntry, CurrentRun, EvidencePage, Fetched, FrameSource, Health,
    MissionIntent, MissionRunsPage, OperatorSection, OperatorViewPage, PreflightQuery,
    ReceiptExportOutcome, StackPreflight, StackPresets, WorldFrame,
};

/// Queue bound per lane: control, evidence, media.
const QUEUE_CAPACITY: [usize; 3] = [16, 32, 4];

/// Why an Artifact content page was requested.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ContentPurpose {
    /// The paged Artifact inspector.
    Inspector,
    /// The Stack tab's auto-following service log tail.
    ServiceLog,
}

/// Effects the run loop forwards to the workers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostCommand {
    /// Perform the health/version handshake.
    Connect,
    /// Load the Stack Presets for the Launch screen.
    FetchPresets,
    /// Run preflight for one preset + toggle combination.
    Preflight {
        request_id: u64,
        query: PreflightQuery,
    },
    /// Submit one Mission Activation with the session credential.
    Submit {
        request: Box<ActivationRequest>,
        credential: String,
    },
    /// Fetch the current Mission Run snapshot.
    PollCurrent { credential: String },
    /// Fetch one run history page (F3), older than `before` when given.
    FetchRunHistory { before: Option<String>, limit: u32 },
    FetchIntent {
        mission_run_id: String,
        credential: String,
    },
    Cancel {
        mission_run_id: String,
        request: CancellationRequest,
        credential: String,
    },
    /// Ask the Host to write the terminal receipt under the Run Root (`x`).
    ExportReceipt {
        mission_run_id: String,
        credential: String,
    },
    /// Fetch one incremental operator-view section, conditional on `etag`.
    FetchOperatorView {
        mission_run_id: String,
        section: OperatorSection,
        cursor: super::dto::OperatorCursor,
        raw: bool,
        etag: Option<String>,
        request_id: u64,
    },
    FetchArtifactContent {
        purpose: ContentPurpose,
        mission_run_id: String,
        artifact_id: String,
        offset: u64,
        limit: u64,
    },
    FetchConversationEntries {
        mission_run_id: String,
        artifact_id: String,
    },
    /// Fetch world-frame bytes, conditional on `etag`.
    FetchWorldFrame {
        mission_run_id: String,
        source: FrameSource,
        etag: Option<String>,
    },
}

/// Responses from the workers.
#[derive(Debug)]
pub enum HostMessage {
    Connected(Result<Health, HostError>),
    Presets(Result<StackPresets, HostError>),
    Preflight {
        request_id: u64,
        result: Result<StackPreflight, HostError>,
    },
    Activated(Result<ActivationOutcome, HostError>),
    Current(Result<CurrentRun, HostError>),
    RunHistory {
        before: Option<String>,
        result: Result<MissionRunsPage, HostError>,
    },
    Intent(Result<MissionIntent, HostError>),
    Cancelled(Result<CancellationOutcome, HostError>),
    ReceiptExported(Result<ReceiptExportOutcome, HostError>),
    OperatorView {
        mission_run_id: String,
        section: OperatorSection,
        request_id: u64,
        result: Result<Fetched<OperatorViewPage>, HostError>,
    },
    ArtifactContent {
        purpose: ContentPurpose,
        mission_run_id: String,
        artifact_id: String,
        requested_offset: u64,
        result: Result<ArtifactContentPage, HostError>,
    },
    ConversationEntries {
        mission_run_id: String,
        artifact_id: String,
        result: Result<EvidencePage<ConversationEntry>, HostError>,
    },
    WorldFrame {
        mission_run_id: String,
        source: FrameSource,
        result: Result<Fetched<WorldFrame>, HostError>,
    },
}

impl HostMessage {
    /// Whether this message proves the Runtime Host answered over HTTP.
    pub fn proves_host_response(&self) -> bool {
        fn proves<T>(result: &Result<T, HostError>) -> bool {
            result
                .as_ref()
                .map_or_else(HostError::proves_host_reachable, |_| true)
        }
        match self {
            Self::Connected(result) => proves(result),
            Self::Presets(result) => proves(result),
            Self::Preflight { result, .. } => proves(result),
            Self::Activated(result) => proves(result),
            Self::Current(result) => proves(result),
            Self::RunHistory { result, .. } => proves(result),
            Self::Intent(result) => proves(result),
            Self::Cancelled(result) => proves(result),
            Self::ReceiptExported(result) => proves(result),
            Self::OperatorView { result, .. } => proves(result),
            Self::ArtifactContent { result, .. } => proves(result),
            Self::ConversationEntries { result, .. } => proves(result),
            Self::WorldFrame { result, .. } => proves(result),
        }
    }
}

/// Which worker thread serves a command.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WorkerLane {
    Control = 0,
    Evidence = 1,
    Media = 2,
}

/// Identity of a coalescable poll: two commands with the same key would hit
/// the same URL with the same parameters.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum PollKey {
    Connect,
    Presets,
    Preflight(PreflightQuery),
    Current,
    RunHistory(Option<String>),
    Intent(String),
    OperatorView {
        mission_run_id: String,
        section: OperatorSection,
        cursor: super::dto::OperatorCursor,
        raw: bool,
    },
    ArtifactContent {
        purpose: ContentPurpose,
        mission_run_id: String,
        artifact_id: String,
        offset: u64,
        limit: u64,
    },
    Conversation {
        mission_run_id: String,
        artifact_id: String,
    },
    WorldFrame {
        mission_run_id: String,
        source: FrameSource,
    },
}

impl HostCommand {
    pub fn lane(&self) -> WorkerLane {
        match self {
            Self::Connect
            | Self::FetchPresets
            | Self::Preflight { .. }
            | Self::Submit { .. }
            | Self::PollCurrent { .. }
            | Self::FetchRunHistory { .. }
            | Self::FetchIntent { .. }
            | Self::Cancel { .. }
            | Self::ExportReceipt { .. } => WorkerLane::Control,
            Self::FetchOperatorView { .. }
            | Self::FetchArtifactContent { .. }
            | Self::FetchConversationEntries { .. } => WorkerLane::Evidence,
            Self::FetchWorldFrame { .. } => WorkerLane::Media,
        }
    }

    /// Coalescing key; `None` for mutations, which always run.
    pub fn poll_key(&self) -> Option<PollKey> {
        Some(match self {
            Self::Submit { .. } | Self::Cancel { .. } | Self::ExportReceipt { .. } => return None,
            Self::Connect => PollKey::Connect,
            Self::FetchPresets => PollKey::Presets,
            Self::Preflight { query, .. } => PollKey::Preflight(query.clone()),
            Self::PollCurrent { .. } => PollKey::Current,
            Self::FetchRunHistory { before, .. } => PollKey::RunHistory(before.clone()),
            Self::FetchIntent { mission_run_id, .. } => PollKey::Intent(mission_run_id.clone()),
            Self::FetchOperatorView {
                mission_run_id,
                section,
                cursor,
                raw,
                ..
            } => PollKey::OperatorView {
                mission_run_id: mission_run_id.clone(),
                section: *section,
                cursor: cursor.clone(),
                raw: *raw,
            },
            Self::FetchArtifactContent {
                purpose,
                mission_run_id,
                artifact_id,
                offset,
                limit,
            } => PollKey::ArtifactContent {
                purpose: *purpose,
                mission_run_id: mission_run_id.clone(),
                artifact_id: artifact_id.clone(),
                offset: *offset,
                limit: *limit,
            },
            Self::FetchConversationEntries {
                mission_run_id,
                artifact_id,
            } => PollKey::Conversation {
                mission_run_id: mission_run_id.clone(),
                artifact_id: artifact_id.clone(),
            },
            Self::FetchWorldFrame {
                mission_run_id,
                source,
                ..
            } => PollKey::WorldFrame {
                mission_run_id: mission_run_id.clone(),
                source: *source,
            },
        })
    }
}

/// Execute one command against a blocking client.
pub fn execute(client: &dyn HostClient, command: HostCommand) -> HostMessage {
    match command {
        HostCommand::Connect => HostMessage::Connected(client.health()),
        HostCommand::FetchPresets => HostMessage::Presets(client.stack_presets()),
        HostCommand::Preflight { request_id, query } => HostMessage::Preflight {
            request_id,
            result: client.stack_preflight(&query),
        },
        HostCommand::Submit {
            request,
            credential,
        } => HostMessage::Activated(client.activate(&request, &credential)),
        HostCommand::PollCurrent { credential } => {
            HostMessage::Current(client.current_run(&credential))
        }
        HostCommand::FetchRunHistory { before, limit } => {
            let result = client.mission_runs(before.as_deref(), limit);
            HostMessage::RunHistory { before, result }
        }
        HostCommand::FetchIntent {
            mission_run_id,
            credential,
        } => HostMessage::Intent(client.mission_intent(&mission_run_id, &credential)),
        HostCommand::Cancel {
            mission_run_id,
            request,
            credential,
        } => HostMessage::Cancelled(client.cancel(&mission_run_id, &request, &credential)),
        HostCommand::ExportReceipt {
            mission_run_id,
            credential,
        } => HostMessage::ReceiptExported(client.export_receipt(&mission_run_id, &credential)),
        HostCommand::FetchOperatorView {
            mission_run_id,
            section,
            cursor,
            raw,
            etag,
            request_id,
        } => {
            let result =
                client.operator_view(&mission_run_id, section, &cursor, raw, etag.as_deref());
            HostMessage::OperatorView {
                mission_run_id,
                section,
                request_id,
                result,
            }
        }
        HostCommand::FetchArtifactContent {
            purpose,
            mission_run_id,
            artifact_id,
            offset,
            limit,
        } => {
            let result =
                client.artifact_content(&mission_run_id, &artifact_id, Some(offset), Some(limit));
            HostMessage::ArtifactContent {
                purpose,
                mission_run_id,
                artifact_id,
                requested_offset: offset,
                result,
            }
        }
        HostCommand::FetchConversationEntries {
            mission_run_id,
            artifact_id,
        } => {
            let result = client.all_conversation_entries(&mission_run_id, &artifact_id);
            HostMessage::ConversationEntries {
                mission_run_id,
                artifact_id,
                result,
            }
        }
        HostCommand::FetchWorldFrame {
            mission_run_id,
            source,
            etag,
        } => {
            let result = client.world_frame(&mission_run_id, source, etag.as_deref());
            HostMessage::WorldFrame {
                mission_run_id,
                source,
                result,
            }
        }
    }
}

/// What [`Workers::dispatch`] did with a command.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Dispatch {
    /// Handed to its worker.
    Queued,
    /// An identical poll is already queued or in flight.
    Coalesced {
        command: HostCommand,
        original_request_id: Option<u64>,
    },
    /// A mutation waits in the backlog for queue space.
    Deferred,
    /// A poll was dropped because its queue was full; the next tick retries.
    Dropped(HostCommand),
}

type InFlight = Arc<Mutex<HashMap<PollKey, Option<u64>>>>;

/// The three worker threads plus the coalescing dispatcher.
pub struct Workers {
    lanes: [SyncSender<HostCommand>; 3],
    in_flight: InFlight,
    backlog: VecDeque<HostCommand>,
    messages: Receiver<HostMessage>,
    handles: Vec<JoinHandle<()>>,
}

impl std::fmt::Debug for Workers {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Workers")
            .field("backlog", &self.backlog.len())
            .finish_non_exhaustive()
    }
}

fn spawn_lane(
    name: &str,
    client: Arc<dyn HostClient>,
    rx: Receiver<HostCommand>,
    tx: Sender<HostMessage>,
    in_flight: InFlight,
) -> JoinHandle<()> {
    std::thread::Builder::new()
        .name(format!("host-{name}"))
        .spawn(move || {
            while let Ok(command) = rx.recv() {
                let key = command.poll_key();
                let message = execute(client.as_ref(), command);
                if let Some(key) = key {
                    in_flight.lock().remove(&key);
                }
                if tx.send(message).is_err() {
                    break;
                }
            }
        })
        .expect("spawn host worker thread")
}

impl Workers {
    /// Start the control, evidence, and media threads sharing `client`.
    pub fn spawn(client: impl HostClient + 'static) -> Self {
        let client: Arc<dyn HostClient> = Arc::new(client);
        let (message_tx, messages) = mpsc::channel();
        let in_flight: InFlight = Arc::default();
        let mut handles = Vec::with_capacity(3);
        let lanes = ["control", "evidence", "media"]
            .into_iter()
            .zip(QUEUE_CAPACITY)
            .map(|(name, capacity)| {
                let (tx, rx) = mpsc::sync_channel(capacity);
                handles.push(spawn_lane(
                    name,
                    Arc::clone(&client),
                    rx,
                    message_tx.clone(),
                    Arc::clone(&in_flight),
                ));
                tx
            })
            .collect::<Vec<_>>()
            .try_into()
            .expect("three lanes");
        Workers {
            lanes,
            in_flight,
            backlog: VecDeque::new(),
            messages,
            handles,
        }
    }

    /// Route one command to its worker, coalescing identical polls.
    pub fn dispatch(&mut self, command: HostCommand) -> Dispatch {
        let key = command.poll_key();
        if let Some(key) = key.as_ref() {
            let mut in_flight = self.in_flight.lock();
            if let Some(original_request_id) = in_flight.get(key) {
                return Dispatch::Coalesced {
                    original_request_id: *original_request_id,
                    command,
                };
            }
            let request_id = match &command {
                HostCommand::FetchOperatorView { request_id, .. }
                | HostCommand::Preflight { request_id, .. } => Some(*request_id),
                _ => None,
            };
            in_flight.insert(key.clone(), request_id);
        } else if !self.backlog.is_empty() {
            // Keep mutations in submission order.
            self.backlog.push_back(command);
            return Dispatch::Deferred;
        }
        match self.lanes[command.lane() as usize].try_send(command) {
            Ok(()) => Dispatch::Queued,
            Err(TrySendError::Full(command) | TrySendError::Disconnected(command)) => match key {
                Some(key) => {
                    self.in_flight.lock().remove(&key);
                    Dispatch::Dropped(command)
                }
                None => {
                    self.backlog.push_back(command);
                    Dispatch::Deferred
                }
            },
        }
    }

    /// Retry deferred mutations in order.
    pub fn flush_backlog(&mut self) {
        while let Some(command) = self.backlog.pop_front() {
            match self.lanes[command.lane() as usize].try_send(command) {
                Ok(()) => {}
                Err(TrySendError::Full(command) | TrySendError::Disconnected(command)) => {
                    self.backlog.push_front(command);
                    return;
                }
            }
        }
    }

    /// Next available worker message, if any.
    pub fn try_recv(&self) -> Option<HostMessage> {
        self.messages.try_recv().ok()
    }

    /// Wait up to `timeout` for the next worker message.
    pub fn recv_timeout(&self, timeout: Duration) -> Option<HostMessage> {
        self.messages.recv_timeout(timeout).ok()
    }

    /// Close the queues and wait for every worker to finish its current
    /// request. The console itself just drops [`Workers`] on exit so a slow
    /// request never delays shutdown.
    pub fn shutdown(self) {
        let Workers { lanes, handles, .. } = self;
        drop(lanes);
        for handle in handles {
            let _ = handle.join();
        }
    }
}
