//! Runtime Host API v1.3 wire contract (v1.2 Hosts remain supported).
//!
//! Every type here round-trips the committed examples under
//! `docs/design/operator-console/contract/` exactly (see
//! `tests/contract_examples.rs`). Optional fields that a section may omit use
//! `skip_serializing_if`; fields the Host always emits (possibly as `null`) are
//! plain `Option`s so `null` survives the round trip.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// Minimum Runtime Host minor version (major 1) this console speaks.
pub const REQUIRED_API_MINOR: u32 = 2;

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

impl ApiVersion {
    /// Whether this console can drive a Host at this version.
    pub fn is_supported(self) -> bool {
        self.major == 1 && self.minor >= REQUIRED_API_MINOR
    }
}

/// Environment Stack selection sent with a Mission Activation. It is part of
/// the activation request identity for replay and conflict checks.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct StackSelection {
    pub preset_id: String,
    pub airsim: bool,
    pub perception: String,
    pub update_ownership: String,
    pub simulation_limit_seconds: u64,
}

/// `POST /api/v1/mission-activations` request body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActivationRequest {
    pub activation_request_id: String,
    pub console_session_id: String,
    pub mission_intent: String,
    pub source_authority: String,
    /// Absent means the Host's configured default preset.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stack: Option<StackSelection>,
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

/// Machine-readable host error body (`404`/`409`/`422`).
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

/// The stack a Mission Run was launched with, as recorded by the Host.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunStack {
    pub preset_id: String,
    pub airsim: bool,
    pub perception: String,
}

/// Why a Mission Run ended the way it did. `kind` is one of
/// `mission_rejected`, `worker_failed`, `stack_failed`; the remaining fields
/// depend on the kind, so the shape stays open to new kinds.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TerminalDetail {
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stage: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub service: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

impl TerminalDetail {
    /// One-line operator summary of the terminal cause.
    pub fn summary(&self) -> String {
        match self.kind.as_str() {
            "mission_rejected" => format!(
                "Mission rejected at {}: {}",
                self.stage.as_deref().unwrap_or("intent"),
                self.reason.as_deref().unwrap_or("no reason recorded")
            ),
            "worker_failed" => format!(
                "Worker failed at {}: {}{}",
                self.stage.as_deref().unwrap_or("unknown stage"),
                self.error_type
                    .as_deref()
                    .map(|kind| format!("{kind}: "))
                    .unwrap_or_default(),
                self.message.as_deref().unwrap_or("no message recorded")
            ),
            "stack_failed" => format!(
                "Stack service {} failed: {}",
                self.service.as_deref().unwrap_or("unknown"),
                self.message.as_deref().unwrap_or("no message recorded")
            ),
            other => format!(
                "{other}: {}",
                self.message
                    .as_deref()
                    .or(self.reason.as_deref())
                    .unwrap_or("no detail recorded")
            ),
        }
    }
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stack: Option<RunStack>,
    #[serde(default)]
    pub terminal_detail: Option<TerminalDetail>,
}

impl RunRecord {
    /// Whether the Host reports a terminal lifecycle status.
    pub fn is_terminal(&self) -> bool {
        is_terminal_status(&self.status)
    }
}

/// Whether a Mission Run lifecycle status is terminal.
pub fn is_terminal_status(status: &str) -> bool {
    matches!(status, "succeeded" | "failed" | "cancelled")
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CancellationOutcome {
    Accepted(CancellationAccepted),
    Rejected { code: String, message: String },
}

// ---------------------------------------------------------------------------
// Stack presets and preflight (§3.1)
// ---------------------------------------------------------------------------

/// Toggle values a preset supports. Independently of these lists,
/// `perception != "off"` requires `airsim == true`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StackSupports {
    pub airsim: Vec<bool>,
    pub perception: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StackDefaults {
    pub airsim: bool,
    pub perception: String,
    pub update_ownership: String,
    pub simulation_limit_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StackPreset {
    pub preset_id: String,
    pub title: String,
    pub mission_mode: String,
    pub default_mission_text: String,
    pub supports: StackSupports,
    pub unsupported_reason: Option<String>,
    pub defaults: StackDefaults,
}

/// `GET /api/v1/stack/presets` response body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StackPresets {
    pub schema_version: u32,
    pub default_preset_id: String,
    pub presets: Vec<StackPreset>,
}

/// Toggle state echoed by preflight and the `stack` section.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct StackToggles {
    pub airsim: bool,
    pub perception: String,
    pub update_ownership: String,
}

/// Query of `GET /api/v1/stack/preflight`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PreflightQuery {
    pub preset_id: String,
    pub toggles: StackToggles,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PreflightCheck {
    pub check_id: String,
    pub label: String,
    /// `pass`, `warn`, or `fail`.
    pub status: String,
    pub detail: Option<String>,
    pub hint: Option<String>,
}

/// `GET /api/v1/stack/preflight` response body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StackPreflight {
    pub schema_version: u32,
    pub preset_id: String,
    pub toggles: StackToggles,
    pub launchable: bool,
    pub checks: Vec<PreflightCheck>,
}

impl StackPreflight {
    /// Launch is allowed only when the Host says so and no check failed.
    pub fn allows_launch(&self) -> bool {
        self.launchable && self.checks.iter().all(|check| check.status != "fail")
    }
}

// ---------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactDisplay {
    pub title: String,
    pub summary: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactDescriptor {
    pub schema_version: u32,
    pub artifact_id: String,
    pub kind: String,
    pub media_type: String,
    pub byte_size: Option<u64>,
    pub content_digest: Option<String>,
    pub display: ArtifactDisplay,
    pub published_at: String,
    pub classification: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub r#ref: Option<String>,
}

impl ArtifactDescriptor {
    /// Whether the paged content inspector can open this Artifact.
    pub fn is_inspectable(&self) -> bool {
        matches!(
            self.classification.as_str(),
            "text" | "binary" | "service_log"
        )
    }
}

/// `GET /api/v1/mission-runs/{id}/artifacts` page (e.g. service logs).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactsPage {
    pub schema_version: u32,
    pub mission_id: String,
    pub mission_run_id: String,
    pub artifacts: Vec<ArtifactDescriptor>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactContentPage {
    pub schema_version: u32,
    pub mission_id: String,
    pub mission_run_id: String,
    pub artifact_id: String,
    pub classification: String,
    pub media_type: String,
    pub byte_size: Option<u64>,
    pub offset: u64,
    pub next_offset: Option<u64>,
    pub eof: bool,
    pub truncated: bool,
    pub content: Option<String>,
}

impl ArtifactContentPage {
    /// Byte offset just past this page's content.
    pub fn end_offset(&self) -> u64 {
        self.next_offset.unwrap_or_else(|| {
            self.offset
                .saturating_add(self.content.as_deref().map_or(0, |text| text.len() as u64))
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContentRef {
    pub path: String,
    pub media_type: String,
    pub byte_size: u64,
    pub content_digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConversationEntry {
    pub sequence: u64,
    pub author: String,
    pub time: String,
    pub audience: String,
    pub kind: String,
    pub content: Option<String>,
    pub content_ref: Option<ContentRef>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConversationEntriesPage {
    pub schema_version: u32,
    pub mission_id: String,
    pub mission_run_id: String,
    pub artifact_id: String,
    pub entries: Vec<ConversationEntry>,
    pub next_cursor: Option<String>,
}

/// Collected evidence items plus a visible pagination-cap disposition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvidencePage<T> {
    pub items: Vec<T>,
    pub truncated: bool,
}

// ---------------------------------------------------------------------------
// Operator Debug View (ADR 0014) sections
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OperatorSection {
    Overview,
    Progress,
    Agents,
    Beliefs,
    Context,
    World,
    Environment,
    Stack,
    Artifacts,
}

impl OperatorSection {
    pub const ALL: [Self; 9] = [
        Self::Overview,
        Self::Progress,
        Self::Agents,
        Self::Beliefs,
        Self::Context,
        Self::World,
        Self::Environment,
        Self::Stack,
        Self::Artifacts,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Overview => "overview",
            Self::Progress => "progress",
            Self::Agents => "agents",
            Self::Beliefs => "beliefs",
            Self::Context => "context",
            Self::World => "world",
            Self::Environment => "environment",
            Self::Stack => "stack",
            Self::Artifacts => "artifacts",
        }
    }

    /// Dense index for per-section bookkeeping arrays.
    pub fn index(self) -> usize {
        Self::ALL
            .iter()
            .position(|section| *section == self)
            .expect("ALL lists every section")
    }
}

/// Exactly one operator-view paging direction; cursors are opaque Host tokens.
#[derive(Debug, Clone, Default, PartialEq, Eq, Hash)]
pub enum OperatorCursor {
    #[default]
    Latest,
    After(String),
    Before(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorDebugDisposition {
    pub enabled: bool,
    pub reasoning_label: String,
    pub reasoning_authority: String,
    pub disposition: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorPageMeta {
    pub schema_version: u32,
    pub mission_id: String,
    pub mission_run_id: String,
    pub run_status: String,
    pub section: OperatorSection,
    pub debug: OperatorDebugDisposition,
    pub next_cursor: String,
    pub before_cursor: Option<String>,
    pub has_more: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NarrativeEvidence {
    pub kind: String,
    pub message: String,
}

/// Run Narrative record as embedded in the overview section.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunNarrative {
    pub status: String,
    pub text: Option<String>,
    pub generated_at: Option<String>,
    pub source_watermark: u64,
    pub terminal: bool,
    pub evidence: Option<NarrativeEvidence>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecordedDebugReasoning {
    pub label: String,
    pub authority: String,
    pub disposition: String,
    pub content: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorToolCall {
    pub name: String,
    pub args: serde_json::Value,
    pub arguments_text: Option<String>,
    pub partial: bool,
    pub result: serde_json::Value,
    pub error: serde_json::Value,
    pub duration_ms: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorAgentInvocation {
    pub stable_id: String,
    pub invocation_id: String,
    pub parent_id: Option<String>,
    pub role: String,
    pub phase: String,
    pub kind: String,
    pub name: String,
    pub status: String,
    pub completion_state: String,
    pub started_at: Option<String>,
    pub updated_at: Option<String>,
    pub finished_at: Option<String>,
    pub duration_ms: Option<u64>,
    pub revision: u64,
    pub outcome: Option<String>,
    pub content: Option<String>,
    pub decision: serde_json::Value,
    pub recorded_debug_reasoning: RecordedDebugReasoning,
    pub tool_calls: Vec<OperatorToolCall>,
    pub debug_payload_disposition: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorTimelineEntry {
    pub stable_id: String,
    pub observation_sequence: u64,
    pub event_id: String,
    pub occurred_at: Option<String>,
    pub component: Option<String>,
    pub authority: Option<String>,
    pub event_kind: String,
    pub status: Option<String>,
    pub outcome: Option<String>,
    pub correlation_id: Option<String>,
    pub replay_disposition: String,
    pub payload: serde_json::Value,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorEnvironment {
    pub authority: String,
    pub position: serde_json::Value,
    pub velocity: serde_json::Value,
    pub mission_time_seconds: Option<serde_json::Number>,
    pub fsm_state: Option<String>,
    pub fsm_status: Option<String>,
    pub active_maneuver: serde_json::Value,
    pub maneuver_feedback: serde_json::Value,
    /// Mission-mode world model state; emitted by the Host since v1.1 but
    /// absent from the v1.1 example.
    #[serde(default, skip_serializing_if = "serde_json::Value::is_null")]
    pub world_model_info: serde_json::Value,
    pub perceptions: serde_json::Value,
    pub belief_changes: Vec<serde_json::Value>,
    pub warnings: Vec<String>,
    pub raw: bool,
    pub timeline: Vec<OperatorTimelineEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorLatestAgents {
    pub hyper_agent: Option<OperatorAgentInvocation>,
    pub maneuver_control: Option<OperatorAgentInvocation>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorFsmSummary {
    pub state: Option<String>,
    pub status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorEnvironmentSummary {
    pub position: serde_json::Value,
    pub velocity: serde_json::Value,
    pub mission_time_seconds: Option<serde_json::Number>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorCounts {
    pub agents: u64,
    pub environment_events: u64,
    pub artifacts: u64,
    pub warnings: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorHitlStatus {
    pub status: String,
    pub requires_action: bool,
}

/// One step of the run phase stepper.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PhaseStep {
    pub id: String,
    pub label: String,
    /// `pending`, `active`, `done`, or `failed`.
    pub status: String,
    pub detail: Option<String>,
}

/// Code-owned run phase derived by the Host (`overview.phase`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunPhase {
    pub current: String,
    pub steps: Vec<PhaseStep>,
}

/// Record counts per Importance Level (mapping v1).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImportanceCounts {
    pub critical: u64,
    pub warning: u64,
    pub notable: u64,
    pub routine: u64,
    pub debug: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorOverview {
    pub authority: String,
    pub latest_agents: OperatorLatestAgents,
    pub fsm: OperatorFsmSummary,
    pub environment: OperatorEnvironmentSummary,
    pub active_maneuver: serde_json::Value,
    pub recent_events: Vec<OperatorTimelineEntry>,
    pub counts: OperatorCounts,
    pub narrative: RunNarrative,
    pub hitl: OperatorHitlStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<RunPhase>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub counts_by_importance: Option<ImportanceCounts>,
}

/// Narrative root of the progress hierarchy.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProgressNarrative {
    pub status: String,
    pub text: Option<String>,
    pub generated_at: Option<String>,
    pub source_watermark: u64,
}

/// One node of the flat progress stream; the console builds the tree by
/// `parent_id` and merges by `node_id`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProgressNode {
    pub node_id: String,
    pub parent_id: Option<String>,
    /// `summary`, `record`, or `live`.
    pub level: String,
    pub importance: String,
    pub time_start: Option<String>,
    pub time_end: Option<String>,
    pub mission_time_seconds: Option<f64>,
    pub source: String,
    pub event_kind: Option<String>,
    pub outcome: Option<String>,
    pub title: String,
    pub text: Option<String>,
    pub child_count: u64,
    pub authoritative: bool,
    pub details: serde_json::Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OperatorProgress {
    pub mapping_version: u32,
    pub narrative: ProgressNarrative,
    pub nodes: Vec<ProgressNode>,
}

/// One belief entity. Reporting-reliability fields are `null` for
/// `bayesian_risk` marginals.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BeliefEntity {
    pub entity_id: String,
    pub label: String,
    pub mean: f64,
    pub honest_probability: Option<f64>,
    pub credible_interval: Option<[f64; 2]>,
    pub variance: Option<f64>,
    pub outcome_counts: Option<BTreeMap<String, u64>>,
    pub delta_since_previous: Option<f64>,
    pub delta_since_prior: Option<f64>,
    /// Sparkline values; index `i` matches `history[i].revision`.
    pub means_by_revision: Vec<Option<f64>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BeliefChange {
    pub entity_id: String,
    pub mean: f64,
    pub delta: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BeliefRevision {
    pub revision: u64,
    pub created_at: String,
    pub top_changes: Vec<BeliefChange>,
}

/// `beliefs` section; `belief_kind == None` carries a `reason`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OperatorBeliefs {
    pub belief_kind: Option<String>,
    pub reason: Option<String>,
    pub revision: Option<u64>,
    pub created_at: Option<String>,
    pub entities: Vec<BeliefEntity>,
    pub history: Vec<BeliefRevision>,
}

impl OperatorBeliefs {
    /// Index of the entity selected by stable ID; the first entity when the
    /// selection is unset or no longer published.
    pub fn selected_index(&self, entity_id: Option<&str>) -> Option<usize> {
        if self.entities.is_empty() {
            return None;
        }
        Some(
            entity_id
                .and_then(|id| {
                    self.entities
                        .iter()
                        .position(|entity| entity.entity_id == id)
                })
                .unwrap_or(0),
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MissionSnapshotSummary {
    pub version: u64,
    pub plan_revision: Option<u64>,
    pub created_at: Option<String>,
    pub sequence: Option<u64>,
    pub source_health: BTreeMap<String, String>,
    /// Whether each source is fresh.
    pub source_freshness: BTreeMap<String, bool>,
    pub source_revisions: BTreeMap<String, u64>,
    pub missing_sources: Vec<String>,
}

/// An enabled FSM Transition Candidate.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitionCandidate {
    pub event: String,
    pub source: String,
    pub target: String,
    #[serde(default)]
    pub condition: Option<String>,
    #[serde(default)]
    pub readiness: serde_json::Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FsmStatusSummary {
    pub active_state: Option<String>,
    pub status: Option<String>,
    pub plan_revision: Option<u64>,
    pub statechart_revision: Option<u64>,
    #[serde(default)]
    pub last_applied_event: Option<String>,
    pub sequence: Option<u64>,
    pub transition_candidates: Vec<TransitionCandidate>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ActiveManeuverSummary {
    pub maneuver_id: Option<String>,
    pub action: Option<String>,
    pub status: Option<String>,
    pub phase: Option<String>,
    pub progress: Option<f64>,
    pub progress_detail: serde_json::Value,
    #[serde(default)]
    pub deadline_seconds: Option<f64>,
    #[serde(default)]
    pub remaining_seconds: Option<f64>,
    #[serde(default)]
    pub mission_time_seconds: Option<f64>,
    #[serde(default)]
    pub plan_revision: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TransitionIntentSummary {
    pub intent_id: String,
    pub status: Option<String>,
    pub source_state: Option<String>,
    pub target: String,
    #[serde(default)]
    pub condition: Option<String>,
    #[serde(default)]
    pub rationale: Option<String>,
    #[serde(default)]
    pub selected_at_seconds: Option<f64>,
    pub plan_revision: Option<u64>,
    pub sequence: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HyperOutcomeSummary {
    pub disposition: String,
    #[serde(default)]
    pub evidence_summary: Option<String>,
    pub plan_revision: Option<u64>,
    pub trigger_identities: Vec<String>,
    pub request_identities: Vec<String>,
    pub sequence: Option<u64>,
}

/// `context` section: Context Coordination state behind the agents. Every
/// member may be `null`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OperatorContext {
    pub mission_snapshot: Option<MissionSnapshotSummary>,
    pub fsm_status: Option<FsmStatusSummary>,
    pub active_maneuver: Option<ActiveManeuverSummary>,
    pub latest_transition_intent: Option<TransitionIntentSummary>,
    pub latest_hyper_outcome: Option<HyperOutcomeSummary>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorldViewer {
    pub available: bool,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorldState {
    pub mission_time_seconds: Option<f64>,
    pub flight_state: Option<String>,
    pub active_maneuver: Option<String>,
    pub state_version: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorldFrameInfo {
    pub source: String,
    pub sequence: Option<u64>,
    pub media_type: String,
}

/// `world.airsim.annotation`: what the boxes on `camera_front_annotated` are.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AirSimAnnotation {
    /// `ideal_segmentation`, `perception_ideal`, or `perception_yolo`.
    pub kind: String,
    /// Provenance sentence the console shows next to the annotated frame.
    pub disclosure: String,
    /// `exact` when the boxes belong to this frame; `none` when no
    /// perception sample matches it.
    #[serde(rename = "match")]
    pub match_: String,
    pub objects: u64,
    pub perception_mission_time_seconds: Option<serde_json::Number>,
}

/// `world.airsim` (v1.3): how AirSim frames relate to the Mission clock.
/// Times are kept as JSON numbers so integral values round-trip exactly.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AirSimStatus {
    /// `world_model_follower` or `scene_clock`.
    pub mode: String,
    /// `off`, `ideal`, or `yolo`.
    pub perception: String,
    /// `capturing`, `unavailable`, or `stopped`.
    pub state: String,
    pub reason: Option<String>,
    pub frame_mission_time_seconds: Option<serde_json::Number>,
    pub world_mission_time_seconds: Option<serde_json::Number>,
    pub lag_seconds: Option<serde_json::Number>,
    pub ship_phase_error_seconds: Option<serde_json::Number>,
    pub annotation: Option<AirSimAnnotation>,
}

/// `world` section.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OperatorWorld {
    pub viewer: WorldViewer,
    pub state: Option<WorldState>,
    pub frames: Vec<WorldFrameInfo>,
    /// Absent before v1.3 and when AirSim is off.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub airsim: Option<AirSimStatus>,
}

/// One Environment Stack service.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StackService {
    pub name: String,
    pub required: bool,
    /// `pending`, `starting`, `ready`, `exited`, `failed`, or `stopped`.
    pub state: String,
    pub pid: Option<u32>,
    pub port: Option<u16>,
    pub started_at: Option<String>,
    pub ready_at: Option<String>,
    pub exit_code: Option<i32>,
    pub log_artifact_id: Option<String>,
    pub last_line: Option<String>,
    pub importance: String,
}

/// `stack` section.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OperatorStack {
    pub preset_id: String,
    pub toggles: StackToggles,
    pub services: Vec<StackService>,
}

macro_rules! operator_page {
    ($(#[$doc:meta])* $name:ident, $field:ident: $body:ty) => {
        $(#[$doc])*
        #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
        pub struct $name {
            #[serde(flatten)]
            pub meta: OperatorPageMeta,
            pub $field: $body,
        }
    };
}

operator_page!(OperatorOverviewPage, overview: OperatorOverview);
operator_page!(OperatorProgressPage, progress: OperatorProgress);
operator_page!(OperatorAgentsPage, agents: Vec<OperatorAgentInvocation>);
operator_page!(OperatorBeliefsPage, beliefs: OperatorBeliefs);
operator_page!(OperatorContextPage, context: OperatorContext);
operator_page!(OperatorWorldPage, world: OperatorWorld);
operator_page!(OperatorEnvironmentPage, environment: OperatorEnvironment);
operator_page!(OperatorStackPage, stack: OperatorStack);
operator_page!(OperatorArtifactsPage, artifacts: Vec<ArtifactDescriptor>);

/// A decoded operator-view page of any section.
#[derive(Debug, Clone, PartialEq)]
pub enum OperatorViewPage {
    Overview(Box<OperatorOverviewPage>),
    Progress(Box<OperatorProgressPage>),
    Agents(Box<OperatorAgentsPage>),
    Beliefs(Box<OperatorBeliefsPage>),
    Context(Box<OperatorContextPage>),
    World(Box<OperatorWorldPage>),
    Environment(Box<OperatorEnvironmentPage>),
    Stack(Box<OperatorStackPage>),
    Artifacts(Box<OperatorArtifactsPage>),
}

impl OperatorViewPage {
    pub fn meta(&self) -> &OperatorPageMeta {
        match self {
            Self::Overview(page) => &page.meta,
            Self::Progress(page) => &page.meta,
            Self::Agents(page) => &page.meta,
            Self::Beliefs(page) => &page.meta,
            Self::Context(page) => &page.meta,
            Self::World(page) => &page.meta,
            Self::Environment(page) => &page.meta,
            Self::Stack(page) => &page.meta,
            Self::Artifacts(page) => &page.meta,
        }
    }

    /// Decode a `200` body for `section`.
    pub fn decode(section: OperatorSection, body: &[u8]) -> Result<Self, serde_json::Error> {
        Ok(match section {
            OperatorSection::Overview => Self::Overview(Box::new(serde_json::from_slice(body)?)),
            OperatorSection::Progress => Self::Progress(Box::new(serde_json::from_slice(body)?)),
            OperatorSection::Agents => Self::Agents(Box::new(serde_json::from_slice(body)?)),
            OperatorSection::Beliefs => Self::Beliefs(Box::new(serde_json::from_slice(body)?)),
            OperatorSection::Context => Self::Context(Box::new(serde_json::from_slice(body)?)),
            OperatorSection::World => Self::World(Box::new(serde_json::from_slice(body)?)),
            OperatorSection::Environment => {
                Self::Environment(Box::new(serde_json::from_slice(body)?))
            }
            OperatorSection::Stack => Self::Stack(Box::new(serde_json::from_slice(body)?)),
            OperatorSection::Artifacts => Self::Artifacts(Box::new(serde_json::from_slice(body)?)),
        })
    }
}

/// World-frame sources served by `GET .../world-frame?source=`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FrameSource {
    World,
    CameraFrontAnnotated,
    CameraFront,
    CameraThirdPerson,
}

impl FrameSource {
    /// Cycle order for `s`.
    pub const ALL: [Self; 4] = [
        Self::World,
        Self::CameraFrontAnnotated,
        Self::CameraFront,
        Self::CameraThirdPerson,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::World => "world",
            Self::CameraFrontAnnotated => "camera_front_annotated",
            Self::CameraFront => "camera_front",
            Self::CameraThirdPerson => "camera_third_person",
        }
    }

    /// Short operator-facing name for titles.
    pub fn label(self) -> &'static str {
        match self {
            Self::World => "world",
            Self::CameraFrontAnnotated => "front · annotated",
            Self::CameraFront => "front",
            Self::CameraThirdPerson => "third-person",
        }
    }
}

/// Raw world-frame bytes plus the Host's frame headers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorldFrame {
    pub source: FrameSource,
    pub media_type: String,
    pub etag: Option<String>,
    pub sequence: Option<u64>,
    pub mission_time: Option<String>,
    pub bytes: Vec<u8>,
}

/// Result of a conditional (`If-None-Match`) GET.
#[derive(Debug, Clone, PartialEq)]
pub enum Fetched<T> {
    /// `200` with a body and its validator, if any.
    Fresh { value: T, etag: Option<String> },
    /// `304 Not Modified`: the previously delivered value is still current.
    NotModified,
}
