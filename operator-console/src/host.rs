//! Runtime Host HTTP client and wire contract.
//!
//! All host communication is isolated behind [`HostClient`] so drawing and
//! input handling never perform IO. The run loop forwards [`crate::app::HostCommand`]
//! values to a worker thread that owns a blocking client.

use std::fmt;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// `GET /api/v1/health` response body.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Health {
    pub status: String,
    pub api_version: ApiVersion,
}

/// Host API version.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub struct ApiVersion {
    pub major: u32,
    pub minor: u32,
}

/// `POST /api/v1/mission-activations` request body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ActivationRequest {
    pub activation_request_id: String,
    pub console_session_id: String,
    pub mission_intent: String,
    pub source_authority: String,
}

/// `202 Accepted` activation response body.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ActivationAccepted {
    pub activation_request_id: String,
    pub mission_id: String,
    pub mission_run_id: String,
    pub status: String,
    pub created_at: String,
}

/// Machine-readable host error body (`409`/`422`).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ErrorBody {
    pub error: ErrorDetail,
}

/// Inner error detail with a stable machine-readable code.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
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
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
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
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct CurrentRun {
    pub mission_run: Option<RunRecord>,
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
}

impl fmt::Display for HostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HostError::Transport(detail) => write!(f, "transport error: {detail}"),
            HostError::UnexpectedStatus(status, detail) => {
                write!(f, "unexpected status {status}: {detail}")
            }
            HostError::Malformed(detail) => write!(f, "malformed response: {detail}"),
        }
    }
}

impl std::error::Error for HostError {}

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
}
