//! Runtime Host HTTP client and wire contract.
//!
//! All host communication is isolated behind [`HostClient`] so drawing and
//! input handling never perform IO. The run loop forwards [`crate::app::HostCommand`]
//! values to a worker thread that owns a blocking client.

use std::fmt;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// `GET /api/v1/health` response body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Health {
    pub status: String,
    pub api_version: ApiVersion,
}

/// Host API version.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApiVersion {
    pub major: u32,
    pub minor: u32,
}

/// `POST /api/v1/mission-activations` request body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActivationRequest {
    pub activation_request_id: String,
    pub console_session_id: String,
    pub mission_intent: String,
    pub source_authority: String,
}

/// `202 Accepted` activation response body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActivationAccepted {
    pub activation_request_id: String,
    pub mission_id: String,
    pub mission_run_id: String,
    pub status: String,
    pub created_at: String,
}

/// Machine-readable host error body (`409`/`422`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorBody {
    pub error: ErrorDetail,
}

/// Inner error detail with a stable machine-readable code.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ErrorDetail {
    pub code: String,
    pub message: String,
}

/// Outcome of a Mission Activation attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ActivationOutcome {
    /// The activation was accepted (`202`).
    Accepted(ActivationAccepted),
    /// The activation was rejected with a stable code (`409`).
    Rejected { code: String, message: String },
}

/// One Mission Run snapshot from `GET /api/v1/mission-runs/current`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunRecord {
    pub mission_id: String,
    pub mission_run_id: String,
    pub status: String,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub started_at: Option<String>,
    #[serde(default)]
    pub finished_at: Option<String>,
    #[serde(default)]
    pub terminal_classification: Option<String>,
}

/// `GET /api/v1/mission-runs/current` response body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CurrentRun {
    pub mission_run: Option<RunRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MissionIntent {
    pub mission_run_id: String,
    pub mission_intent: String,
    pub source_authority: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CancellationRequest {
    pub cancellation_request_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CancellationAccepted {
    pub mission_run_id: String,
    pub cancellation_request_id: String,
    pub disposition: String,
    pub status: String,
    pub requested_at: String,
}

/// One redacted trace item exposed through the public evidence projection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TraceViewItem {
    pub schema_version: u32,
    pub trace_id: String,
    pub event_id: String,
    pub mission_id: String,
    pub sequence: u64,
    pub occurred_at: String,
    pub component: String,
    pub authority: String,
    pub event_kind: String,
    pub status: Option<String>,
    pub outcome: Option<String>,
    pub correlation_id: Option<String>,
    pub parent_id: Option<String>,
    pub replay_disposition: String,
    pub payload: serde_json::Map<String, serde_json::Value>,
    pub observation_sequence: Option<u64>,
    pub observed_at: Option<String>,
    pub redacted_fields: Vec<String>,
    pub missing_fields: Vec<String>,
}

/// One observation envelope in a Mission Run evidence page.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObservationEnvelope {
    pub schema_version: u32,
    pub observation_sequence: u64,
    pub observed_at: String,
    pub item: TraceViewItem,
}

/// Public paginated Mission Run observation response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObservationsPage {
    pub schema_version: u32,
    pub mission_id: String,
    pub mission_run_id: String,
    pub observations: Vec<ObservationEnvelope>,
    pub next_cursor: Option<String>,
}

/// One deterministic mapping-version-1 activity projection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunActivity {
    pub schema_version: u32,
    pub activity_id: String,
    pub activity_sequence: u64,
    pub mapping_version: u32,
    pub kind: String,
    pub status: String,
    pub summary: String,
    pub component: String,
    pub event_kind: String,
    pub outcome: Option<String>,
    pub correlation_id: Option<String>,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub observation_sequences: Vec<u64>,
    pub replay_disposition: String,
    pub redacted_fields: Vec<String>,
    pub missing_fields: Vec<String>,
}

/// Public paginated Mission Run activity response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActivitiesPage {
    pub schema_version: u32,
    pub mission_id: String,
    pub mission_run_id: String,
    pub mapping_version: u32,
    pub activities: Vec<RunActivity>,
    pub next_cursor: Option<String>,
}

/// Collected evidence items plus a visible pagination-cap disposition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvidencePage<T> {
    pub items: Vec<T>,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CancellationOutcome {
    Accepted(CancellationAccepted),
    Rejected { code: String, message: String },
}

/// Client-visible host failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostError {
    /// Transport-level failure (connect, timeout, IO).
    Transport(String),
    /// A status code the console does not model.
    UnexpectedStatus(u16, String),
    /// The response body did not match the contract.
    Malformed(String),
    AuthorizationFailed {
        code: String,
        message: String,
    },
    /// The requested Mission Run is unknown to the Host.
    NotFound {
        code: String,
        message: String,
    },
    /// The supplied opaque evidence cursor is invalid.
    InvalidCursor {
        code: String,
        message: String,
    },
}

impl fmt::Display for HostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HostError::Transport(detail) => write!(f, "transport error: {detail}"),
            HostError::UnexpectedStatus(status, detail) => {
                write!(f, "unexpected status {status}: {detail}")
            }
            HostError::Malformed(detail) => write!(f, "malformed response: {detail}"),
            HostError::AuthorizationFailed { code, message } => {
                write!(f, "authorization failed ({code}): {message}")
            }
            HostError::NotFound { code, message } => {
                write!(f, "not found ({code}): {message}")
            }
            HostError::InvalidCursor { code, message } => {
                write!(f, "invalid cursor ({code}): {message}")
            }
        }
    }
}

impl std::error::Error for HostError {}

impl HostError {
    /// Whether this error proves that the Runtime Host returned an HTTP response.
    pub fn proves_host_reachable(&self) -> bool {
        matches!(
            self,
            HostError::UnexpectedStatus(_, _)
                | HostError::AuthorizationFailed { .. }
                | HostError::NotFound { .. }
                | HostError::InvalidCursor { .. }
        )
    }
}

trait EvidenceResponse {
    type Item;

    fn into_items_and_cursor(self) -> (Vec<Self::Item>, Option<String>);
}

impl EvidenceResponse for ObservationsPage {
    type Item = ObservationEnvelope;

    fn into_items_and_cursor(self) -> (Vec<Self::Item>, Option<String>) {
        (self.observations, self.next_cursor)
    }
}

impl EvidenceResponse for ActivitiesPage {
    type Item = RunActivity;

    fn into_items_and_cursor(self) -> (Vec<Self::Item>, Option<String>) {
        (self.activities, self.next_cursor)
    }
}

fn collect_evidence_pages<P>(
    mut fetch: impl FnMut(Option<&str>) -> Result<P, HostError>,
) -> Result<EvidencePage<P::Item>, HostError>
where
    P: EvidenceResponse,
{
    const PAGE_CAP: usize = 100;

    let mut items = Vec::new();
    let mut cursor = None;
    for page_index in 0..PAGE_CAP {
        let page = fetch(cursor.as_deref())?;
        let (page_items, next_cursor) = page.into_items_and_cursor();
        items.extend(page_items);
        let Some(next_cursor) = next_cursor else {
            return Ok(EvidencePage {
                items,
                truncated: false,
            });
        };
        if page_index + 1 == PAGE_CAP {
            return Ok(EvidencePage {
                items,
                truncated: true,
            });
        }
        cursor = Some(next_cursor);
    }
    unreachable!("page collector returns at or before its cap")
}

/// Run a blocking client on its own thread, keeping HTTP outside drawing.
///
/// Commands arrive on `rx`; results are posted to `tx`. The thread exits when
/// the command channel closes (console shutdown).
pub fn spawn_worker(
    client: impl HostClient + 'static,
    rx: std::sync::mpsc::Receiver<crate::app::HostCommand>,
    tx: std::sync::mpsc::Sender<crate::app::HostMessage>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        while let Ok(command) = rx.recv() {
            use crate::app::{HostCommand, HostMessage};
            let message = match command {
                HostCommand::Connect => HostMessage::Connected(client.health()),
                HostCommand::Submit {
                    request,
                    credential,
                } => HostMessage::Activated(client.activate(&request, &credential)),
                HostCommand::PollCurrent { credential } => {
                    HostMessage::Current(client.current_run(&credential))
                }
                HostCommand::FetchIntent {
                    mission_run_id,
                    credential,
                } => HostMessage::Intent(client.mission_intent(&mission_run_id, &credential)),
                HostCommand::FetchActivities { mission_run_id } => {
                    HostMessage::Activities(client.all_activities(&mission_run_id))
                }
                HostCommand::FetchObservations { mission_run_id } => {
                    HostMessage::Observations(client.all_observations(&mission_run_id))
                }
                HostCommand::Cancel {
                    mission_run_id,
                    request,
                    credential,
                } => HostMessage::Cancelled(client.cancel(&mission_run_id, &request, &credential)),
            };
            if tx.send(message).is_err() {
                break;
            }
        }
    })
}

/// Blocking Runtime Host client.
pub trait HostClient: Send {
    /// `GET /api/v1/health`.
    fn health(&self) -> Result<Health, HostError>;
    /// `POST /api/v1/mission-activations` with a Bearer credential.
    fn activate(
        &self,
        request: &ActivationRequest,
        credential: &str,
    ) -> Result<ActivationOutcome, HostError>;
    /// `GET /api/v1/mission-runs/current` with a Bearer credential.
    fn current_run(&self, credential: &str) -> Result<CurrentRun, HostError>;
    fn mission_intent(
        &self,
        mission_run_id: &str,
        credential: &str,
    ) -> Result<MissionIntent, HostError>;
    fn cancel(
        &self,
        mission_run_id: &str,
        request: &CancellationRequest,
        credential: &str,
    ) -> Result<CancellationOutcome, HostError>;
    /// `GET /api/v1/mission-runs/{id}/observations`.
    fn observations(
        &self,
        mission_run_id: &str,
        cursor: Option<&str>,
    ) -> Result<ObservationsPage, HostError>;
    /// `GET /api/v1/mission-runs/{id}/activities`.
    fn activities(
        &self,
        mission_run_id: &str,
        cursor: Option<&str>,
    ) -> Result<ActivitiesPage, HostError>;
    /// Collect at most 100 observation pages for one Mission Run.
    fn all_observations(
        &self,
        mission_run_id: &str,
    ) -> Result<EvidencePage<ObservationEnvelope>, HostError> {
        collect_evidence_pages(|cursor| self.observations(mission_run_id, cursor))
    }
    /// Collect at most 100 activity pages for one Mission Run.
    fn all_activities(&self, mission_run_id: &str) -> Result<EvidencePage<RunActivity>, HostError> {
        collect_evidence_pages(|cursor| self.activities(mission_run_id, cursor))
    }
}

/// ureq-backed blocking client for the loopback Runtime Host.
pub struct UreqHostClient {
    base_url: String,
    agent: ureq::Agent,
}

impl UreqHostClient {
    /// Build a client for `base_url` with bounded request timeouts.
    pub fn new(base_url: &str, timeout: Duration) -> Self {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(timeout))
            .http_status_as_error(false)
            .build();
        UreqHostClient {
            base_url: base_url.trim_end_matches('/').to_string(),
            agent: config.new_agent(),
        }
    }

    fn authorization(credential: &str) -> String {
        format!("Bearer {credential}")
    }

    fn read_json<T: serde::de::DeserializeOwned>(
        response: ureq::http::Response<ureq::Body>,
    ) -> Result<T, HostError> {
        response
            .into_body()
            .read_json()
            .map_err(|e| HostError::Malformed(e.to_string()))
    }

    fn error_detail(response: ureq::http::Response<ureq::Body>) -> Result<ErrorDetail, HostError> {
        Ok(Self::read_json::<ErrorBody>(response)?.error)
    }

    fn evidence_get<T: serde::de::DeserializeOwned>(
        &self,
        mission_run_id: &str,
        resource: &str,
        cursor: Option<&str>,
    ) -> Result<T, HostError> {
        let url = format!(
            "{}/api/v1/mission-runs/{mission_run_id}/{resource}",
            self.base_url
        );
        let request = self.agent.get(&url);
        let response = match cursor {
            Some(cursor) => request.query("cursor", cursor),
            None => request,
        }
        .call()
        .map_err(|e| HostError::Transport(e.to_string()))?;
        match response.status().as_u16() {
            200 => Self::read_json(response),
            404 => {
                let detail = Self::error_detail(response)?;
                Err(HostError::NotFound {
                    code: detail.code,
                    message: detail.message,
                })
            }
            422 => {
                let detail = Self::error_detail(response)?;
                Err(HostError::InvalidCursor {
                    code: detail.code,
                    message: detail.message,
                })
            }
            status => Err(HostError::UnexpectedStatus(
                status,
                format!("{resource} expect 200, 404, or 422"),
            )),
        }
    }
}

impl HostClient for UreqHostClient {
    fn health(&self) -> Result<Health, HostError> {
        let url = format!("{}/api/v1/health", self.base_url);
        let response = self
            .agent
            .get(&url)
            .call()
            .map_err(|e| HostError::Transport(e.to_string()))?;
        if response.status().as_u16() != 200 {
            return Err(HostError::UnexpectedStatus(
                response.status().as_u16(),
                "health check expects 200".to_string(),
            ));
        }
        Self::read_json(response)
    }

    fn activate(
        &self,
        request: &ActivationRequest,
        credential: &str,
    ) -> Result<ActivationOutcome, HostError> {
        let url = format!("{}/api/v1/mission-activations", self.base_url);
        let response = self
            .agent
            .post(&url)
            .header("Authorization", &Self::authorization(credential))
            .send_json(request)
            .map_err(|e| HostError::Transport(e.to_string()))?;
        match response.status().as_u16() {
            202 => Ok(ActivationOutcome::Accepted(Self::read_json(response)?)),
            409 => {
                let detail = Self::error_detail(response)?;
                Ok(ActivationOutcome::Rejected {
                    code: detail.code,
                    message: detail.message,
                })
            }
            422 => {
                let detail = Self::error_detail(response)?;
                Err(HostError::UnexpectedStatus(
                    422,
                    format!("{}: {}", detail.code, detail.message),
                ))
            }
            status => Err(HostError::UnexpectedStatus(
                status,
                "activation expects 202 or 409".to_string(),
            )),
        }
    }

    fn current_run(&self, credential: &str) -> Result<CurrentRun, HostError> {
        let url = format!("{}/api/v1/mission-runs/current", self.base_url);
        let response = self
            .agent
            .get(&url)
            .header("Authorization", &Self::authorization(credential))
            .call()
            .map_err(|e| HostError::Transport(e.to_string()))?;
        if response.status().as_u16() != 200 {
            return Err(HostError::UnexpectedStatus(
                response.status().as_u16(),
                "current run expects 200".to_string(),
            ));
        }
        Self::read_json(response)
    }

    fn mission_intent(
        &self,
        mission_run_id: &str,
        credential: &str,
    ) -> Result<MissionIntent, HostError> {
        let url = format!(
            "{}/api/v1/mission-runs/{mission_run_id}/mission-intent",
            self.base_url
        );
        let response = self
            .agent
            .get(&url)
            .header("Authorization", &Self::authorization(credential))
            .call()
            .map_err(|e| HostError::Transport(e.to_string()))?;
        match response.status().as_u16() {
            200 => Self::read_json(response),
            403 => {
                let detail = Self::error_detail(response)?;
                Err(HostError::AuthorizationFailed {
                    code: detail.code,
                    message: detail.message,
                })
            }
            status => Err(HostError::UnexpectedStatus(
                status,
                "mission intent expects 200 or 403".to_string(),
            )),
        }
    }

    fn cancel(
        &self,
        mission_run_id: &str,
        request: &CancellationRequest,
        credential: &str,
    ) -> Result<CancellationOutcome, HostError> {
        let url = format!(
            "{}/api/v1/mission-runs/{mission_run_id}/cancellations",
            self.base_url
        );
        let response = self
            .agent
            .post(&url)
            .header("Authorization", &Self::authorization(credential))
            .send_json(request)
            .map_err(|e| HostError::Transport(e.to_string()))?;
        match response.status().as_u16() {
            202 => Ok(CancellationOutcome::Accepted(Self::read_json(response)?)),
            403 => {
                let detail = Self::error_detail(response)?;
                Err(HostError::AuthorizationFailed {
                    code: detail.code,
                    message: detail.message,
                })
            }
            409 => {
                let detail = Self::error_detail(response)?;
                Ok(CancellationOutcome::Rejected {
                    code: detail.code,
                    message: detail.message,
                })
            }
            status => Err(HostError::UnexpectedStatus(
                status,
                "cancellation expects 202, 403, or 409".to_string(),
            )),
        }
    }

    fn observations(
        &self,
        mission_run_id: &str,
        cursor: Option<&str>,
    ) -> Result<ObservationsPage, HostError> {
        self.evidence_get(mission_run_id, "observations", cursor)
    }

    fn activities(
        &self,
        mission_run_id: &str,
        cursor: Option<&str>,
    ) -> Result<ActivitiesPage, HostError> {
        self.evidence_get(mission_run_id, "activities", cursor)
    }
}
