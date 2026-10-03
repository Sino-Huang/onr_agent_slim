//! Blocking Runtime Host HTTP client.
//!
//! All host communication is isolated behind [`HostClient`] so drawing and
//! input handling never perform IO. The workers in [`crate::host::workers`]
//! own the client and run it off the UI thread.

use std::fmt;
use std::time::Duration;

use super::dto::{
    ActivationOutcome, ActivationRequest, ArtifactContentPage, CancellationOutcome,
    CancellationRequest, ConversationEntriesPage, ConversationEntry, CurrentRun, ErrorBody,
    ErrorDetail, EvidencePage, Fetched, FrameSource, Health, MissionIntent, OperatorSection,
    OperatorViewPage, PreflightQuery, StackPreflight, StackPresets, WorldFrame,
};

/// Largest response body the console reads (world frames are the biggest).
const MAX_BODY_BYTES: u64 = 32 * 1024 * 1024;
/// Client-side bound on cursor pages collected for one listing.
const PAGE_CAP: usize = 100;

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
    /// The requested Mission Run, Artifact, or frame is unknown to the Host.
    NotFound {
        code: String,
        message: String,
    },
    /// The supplied opaque evidence cursor is invalid.
    InvalidCursor {
        code: String,
        message: String,
    },
    /// A query or body value is outside the endpoint contract.
    InvalidRequest {
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
            HostError::NotFound { code, message } => write!(f, "not found ({code}): {message}"),
            HostError::InvalidCursor { code, message } => {
                write!(f, "invalid cursor ({code}): {message}")
            }
            HostError::InvalidRequest { code, message } => {
                write!(f, "invalid request ({code}): {message}")
            }
        }
    }
}

impl std::error::Error for HostError {}

impl HostError {
    /// Whether this error proves that the Runtime Host returned an HTTP response.
    pub fn proves_host_reachable(&self) -> bool {
        !matches!(self, HostError::Transport(_) | HostError::Malformed(_))
    }
}

/// Collect cursor pages until the Host stops returning a cursor, bounded at
/// [`PAGE_CAP`] pages; hitting the cap is reported as `truncated`.
pub fn collect_pages<T>(
    mut fetch: impl FnMut(Option<&str>) -> Result<(Vec<T>, Option<String>), HostError>,
) -> Result<EvidencePage<T>, HostError> {
    let mut items = Vec::new();
    let mut cursor: Option<String> = None;
    for _ in 0..PAGE_CAP {
        let (page_items, next_cursor) = fetch(cursor.as_deref())?;
        items.extend(page_items);
        match next_cursor {
            None => {
                return Ok(EvidencePage {
                    items,
                    truncated: false,
                });
            }
            Some(next) => cursor = Some(next),
        }
    }
    Ok(EvidencePage {
        items,
        truncated: true,
    })
}

/// Blocking Runtime Host client. Implementations must be shareable between
/// the control, evidence, and media workers.
pub trait HostClient: Send + Sync {
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
    /// `GET /api/v1/mission-runs/{id}/mission-intent` (owner only).
    fn mission_intent(
        &self,
        mission_run_id: &str,
        credential: &str,
    ) -> Result<MissionIntent, HostError>;
    /// `POST /api/v1/mission-runs/{id}/cancellations` (owner only).
    fn cancel(
        &self,
        mission_run_id: &str,
        request: &CancellationRequest,
        credential: &str,
    ) -> Result<CancellationOutcome, HostError>;
    /// `GET /api/v1/stack/presets`.
    fn stack_presets(&self) -> Result<StackPresets, HostError>;
    /// `GET /api/v1/stack/preflight`.
    fn stack_preflight(&self, query: &PreflightQuery) -> Result<StackPreflight, HostError>;
    /// `GET /api/v1/mission-runs/{id}/artifacts/{artifact_id}/content`.
    fn artifact_content(
        &self,
        mission_run_id: &str,
        artifact_id: &str,
        offset: Option<u64>,
        limit: Option<u64>,
    ) -> Result<ArtifactContentPage, HostError>;
    /// `GET /api/v1/mission-runs/{id}/artifacts/{artifact_id}/entries`.
    fn conversation_entries(
        &self,
        mission_run_id: &str,
        artifact_id: &str,
        cursor: Option<&str>,
    ) -> Result<ConversationEntriesPage, HostError>;
    /// `GET /api/v1/mission-runs/{id}/operator-view`, conditional on `etag`.
    fn operator_view(
        &self,
        mission_run_id: &str,
        section: OperatorSection,
        cursor: &super::dto::OperatorCursor,
        raw: bool,
        etag: Option<&str>,
    ) -> Result<Fetched<OperatorViewPage>, HostError>;
    /// `GET /api/v1/mission-runs/{id}/world-frame`, conditional on `etag`.
    fn world_frame(
        &self,
        mission_run_id: &str,
        source: FrameSource,
        etag: Option<&str>,
    ) -> Result<Fetched<WorldFrame>, HostError>;
    /// Collect at most 100 conversation entry pages for one Artifact.
    fn all_conversation_entries(
        &self,
        mission_run_id: &str,
        artifact_id: &str,
    ) -> Result<EvidencePage<ConversationEntry>, HostError> {
        collect_pages(|cursor| {
            self.conversation_entries(mission_run_id, artifact_id, cursor)
                .map(|page| (page.entries, page.next_cursor))
        })
    }
}

type Response = ureq::http::Response<ureq::Body>;

/// ureq-backed blocking client for the loopback Runtime Host.
#[derive(Debug, Clone)]
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

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base_url)
    }

    fn authorization(credential: &str) -> String {
        format!("Bearer {credential}")
    }

    fn transport(error: ureq::Error) -> HostError {
        HostError::Transport(error.to_string())
    }

    fn read_json<T: serde::de::DeserializeOwned>(response: Response) -> Result<T, HostError> {
        response
            .into_body()
            .with_config()
            .limit(MAX_BODY_BYTES)
            .read_json()
            .map_err(|e| HostError::Malformed(e.to_string()))
    }

    fn read_bytes(response: Response) -> Result<Vec<u8>, HostError> {
        response
            .into_body()
            .with_config()
            .limit(MAX_BODY_BYTES)
            .read_to_vec()
            .map_err(|e| HostError::Transport(e.to_string()))
    }

    fn error_detail(response: Response) -> Result<ErrorDetail, HostError> {
        Ok(Self::read_json::<ErrorBody>(response)?.error)
    }

    fn header(response: &Response, name: &str) -> Option<String> {
        response
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string)
    }

    /// Map the shared `404`/`422` error contract; `invalid_cursor` codes
    /// become [`HostError::InvalidCursor`], everything else on `422` is an
    /// invalid request.
    fn common_error(response: Response, resource: &str) -> HostError {
        let status = response.status().as_u16();
        match status {
            403 | 404 | 422 => match Self::error_detail(response) {
                Ok(ErrorDetail { code, message }) => match status {
                    403 => HostError::AuthorizationFailed { code, message },
                    404 => HostError::NotFound { code, message },
                    _ if code == "invalid_cursor" => HostError::InvalidCursor { code, message },
                    _ => HostError::InvalidRequest { code, message },
                },
                Err(error) => error,
            },
            status => HostError::UnexpectedStatus(status, format!("{resource} returned {status}")),
        }
    }

    fn get_json<T: serde::de::DeserializeOwned>(
        &self,
        request: ureq::RequestBuilder<ureq::typestate::WithoutBody>,
        resource: &str,
    ) -> Result<T, HostError> {
        let response = request.call().map_err(Self::transport)?;
        if response.status().as_u16() == 200 {
            Self::read_json(response)
        } else {
            Err(Self::common_error(response, resource))
        }
    }

    fn artifact_id_path(artifact_id: &str) -> String {
        let mut encoded = String::with_capacity(artifact_id.len());
        for byte in artifact_id.bytes() {
            if byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-') {
                encoded.push(char::from(byte));
            } else {
                use std::fmt::Write as _;
                write!(&mut encoded, "%{byte:02X}").expect("writing to String cannot fail");
            }
        }
        encoded
    }
}

impl HostClient for UreqHostClient {
    fn health(&self) -> Result<Health, HostError> {
        let response = self
            .agent
            .get(&self.url("/api/v1/health"))
            .call()
            .map_err(Self::transport)?;
        match response.status().as_u16() {
            200 => Self::read_json(response),
            status => Err(HostError::UnexpectedStatus(
                status,
                "health check expects 200".to_string(),
            )),
        }
    }

    fn activate(
        &self,
        request: &ActivationRequest,
        credential: &str,
    ) -> Result<ActivationOutcome, HostError> {
        let response = self
            .agent
            .post(&self.url("/api/v1/mission-activations"))
            .header("Authorization", &Self::authorization(credential))
            .send_json(request)
            .map_err(Self::transport)?;
        match response.status().as_u16() {
            202 => Ok(ActivationOutcome::Accepted(Self::read_json(response)?)),
            409 => {
                let detail = Self::error_detail(response)?;
                Ok(ActivationOutcome::Rejected {
                    code: detail.code,
                    message: detail.message,
                })
            }
            _ => Err(Self::common_error(response, "activation")),
        }
    }

    fn current_run(&self, credential: &str) -> Result<CurrentRun, HostError> {
        self.get_json(
            self.agent
                .get(&self.url("/api/v1/mission-runs/current"))
                .header("Authorization", &Self::authorization(credential)),
            "current run",
        )
    }

    fn mission_intent(
        &self,
        mission_run_id: &str,
        credential: &str,
    ) -> Result<MissionIntent, HostError> {
        self.get_json(
            self.agent
                .get(&self.url(&format!(
                    "/api/v1/mission-runs/{mission_run_id}/mission-intent"
                )))
                .header("Authorization", &Self::authorization(credential)),
            "mission intent",
        )
    }

    fn cancel(
        &self,
        mission_run_id: &str,
        request: &CancellationRequest,
        credential: &str,
    ) -> Result<CancellationOutcome, HostError> {
        let response = self
            .agent
            .post(&self.url(&format!(
                "/api/v1/mission-runs/{mission_run_id}/cancellations"
            )))
            .header("Authorization", &Self::authorization(credential))
            // The Host waits for owned process-group teardown before replying.
            .config()
            .timeout_global(Some(Duration::from_secs(12)))
            .build()
            .send_json(request)
            .map_err(Self::transport)?;
        match response.status().as_u16() {
            202 => Ok(CancellationOutcome::Accepted(Self::read_json(response)?)),
            409 => {
                let detail = Self::error_detail(response)?;
                Ok(CancellationOutcome::Rejected {
                    code: detail.code,
                    message: detail.message,
                })
            }
            _ => Err(Self::common_error(response, "cancellation")),
        }
    }

    fn stack_presets(&self) -> Result<StackPresets, HostError> {
        self.get_json(
            self.agent.get(&self.url("/api/v1/stack/presets")),
            "stack presets",
        )
    }

    fn stack_preflight(&self, query: &PreflightQuery) -> Result<StackPreflight, HostError> {
        self.get_json(
            self.agent
                .get(&self.url("/api/v1/stack/preflight"))
                .query("preset_id", &query.preset_id)
                .query(
                    "airsim",
                    if query.toggles.airsim {
                        "true"
                    } else {
                        "false"
                    },
                )
                .query("perception", &query.toggles.perception)
                .query("update_ownership", &query.toggles.update_ownership),
            "stack preflight",
        )
    }

    fn artifact_content(
        &self,
        mission_run_id: &str,
        artifact_id: &str,
        offset: Option<u64>,
        limit: Option<u64>,
    ) -> Result<ArtifactContentPage, HostError> {
        let artifact_id = Self::artifact_id_path(artifact_id);
        let mut request = self.agent.get(&self.url(&format!(
            "/api/v1/mission-runs/{mission_run_id}/artifacts/{artifact_id}/content"
        )));
        if let Some(offset) = offset {
            request = request.query("offset", offset.to_string());
        }
        if let Some(limit) = limit {
            request = request.query("limit", limit.to_string());
        }
        self.get_json(request, "artifact content")
    }

    fn conversation_entries(
        &self,
        mission_run_id: &str,
        artifact_id: &str,
        cursor: Option<&str>,
    ) -> Result<ConversationEntriesPage, HostError> {
        let artifact_id = Self::artifact_id_path(artifact_id);
        let mut request = self.agent.get(&self.url(&format!(
            "/api/v1/mission-runs/{mission_run_id}/artifacts/{artifact_id}/entries"
        )));
        if let Some(cursor) = cursor {
            request = request.query("cursor", cursor);
        }
        let result = self.get_json(request, "conversation entries");
        // The entries endpoint reports a bad cursor as a plain 422.
        match result {
            Err(HostError::InvalidRequest { code, message }) => {
                Err(HostError::InvalidCursor { code, message })
            }
            other => other,
        }
    }

    fn operator_view(
        &self,
        mission_run_id: &str,
        section: OperatorSection,
        cursor: &super::dto::OperatorCursor,
        raw: bool,
        etag: Option<&str>,
    ) -> Result<Fetched<OperatorViewPage>, HostError> {
        let mut request = self
            .agent
            .get(&self.url(&format!(
                "/api/v1/mission-runs/{mission_run_id}/operator-view"
            )))
            .query("section", section.as_str())
            .query("limit", "100");
        match cursor {
            super::dto::OperatorCursor::Latest => {}
            super::dto::OperatorCursor::After(cursor) => {
                request = request.query("cursor", cursor);
            }
            super::dto::OperatorCursor::Before(before) => {
                request = request.query("before", before);
            }
        }
        if section == OperatorSection::Environment {
            request = request.query("raw", if raw { "true" } else { "false" });
        }
        if let Some(etag) = etag {
            request = request.header("If-None-Match", etag);
        }
        let response = request.call().map_err(Self::transport)?;
        match response.status().as_u16() {
            200 => {
                let etag = Self::header(&response, "etag");
                let body = Self::read_bytes(response)?;
                let value = OperatorViewPage::decode(section, &body)
                    .map_err(|error| HostError::Malformed(error.to_string()))?;
                Ok(Fetched::Fresh { value, etag })
            }
            304 => Ok(Fetched::NotModified),
            _ => Err(Self::common_error(response, "operator view")),
        }
    }

    fn world_frame(
        &self,
        mission_run_id: &str,
        source: FrameSource,
        etag: Option<&str>,
    ) -> Result<Fetched<WorldFrame>, HostError> {
        let mut request = self
            .agent
            .get(&self.url(&format!(
                "/api/v1/mission-runs/{mission_run_id}/world-frame"
            )))
            .query("source", source.as_str());
        if let Some(etag) = etag {
            request = request.header("If-None-Match", etag);
        }
        let response = request.call().map_err(Self::transport)?;
        match response.status().as_u16() {
            200 => {
                let etag = Self::header(&response, "etag");
                let media_type = Self::header(&response, "content-type")
                    .unwrap_or_else(|| "application/octet-stream".to_string());
                let sequence =
                    Self::header(&response, "x-frame-sequence").and_then(|v| v.parse().ok());
                let mission_time = Self::header(&response, "x-mission-time");
                let bytes = Self::read_bytes(response)?;
                Ok(Fetched::Fresh {
                    value: WorldFrame {
                        source,
                        media_type,
                        etag: etag.clone(),
                        sequence,
                        mission_time,
                        bytes,
                    },
                    etag,
                })
            }
            304 => Ok(Fetched::NotModified),
            _ => Err(Self::common_error(response, "world frame")),
        }
    }
}
