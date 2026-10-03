//! The committed wire-contract examples under
//! `docs/design/operator-console/contract/` are the single source of truth
//! for the console's HTTP client shapes. Each example the console consumes
//! must round-trip through the client DTOs with exact value equality, and the
//! fixture HTTP server (tests/support) serves these same bytes/shapes.
//!
//! v1.2 is the console's required surface; v1.3 adds AirSim-on/perception-off
//! stack presets and the World section's AirSim status. v1/v1.1 examples that
//! the v1.2 Host still serves unchanged (activation, errors, owner intent,
//! cancellation, Artifacts, agents/environment/artifacts sections) keep their
//! exact round trips; `/current` examples from v1 predate `stack` and
//! `terminal_detail` and only need to decode.

use operator_console::host::{
    ActivationAccepted, ActivationRequest, ArtifactContentPage, ArtifactsPage,
    CancellationAccepted, CancellationRequest, ConversationEntriesPage, CurrentRun, ErrorBody,
    FrameSource, Health, MissionIntent, OperatorAgentsPage, OperatorArtifactsPage,
    OperatorBeliefsPage, OperatorContextPage, OperatorEnvironmentPage, OperatorOverviewPage,
    OperatorProgressPage, OperatorSection, OperatorStackPage, OperatorViewPage, OperatorWorldPage,
    StackPreflight, StackPresets,
};
use serde_json::Value;

const ROOT: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../docs/design/operator-console/contract/"
);

fn raw(version: &str, name: &str) -> String {
    let path = format!("{ROOT}{version}/{name}");
    std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("read {path}: {error}"))
}

fn example(version: &str, name: &str) -> Value {
    serde_json::from_str(raw(version, name).trim_end())
        .unwrap_or_else(|error| panic!("parse {version}/{name}: {error}"))
}

/// Parse a committed example into `T` and assert the DTO covers exactly the
/// example's fields - no missing, no extra.
fn exact<T>(version: &str, name: &str) -> T
where
    T: serde::de::DeserializeOwned + serde::Serialize + std::fmt::Debug,
{
    let value = example(version, name);
    let dto: T = serde_json::from_value(value.clone())
        .unwrap_or_else(|error| panic!("decode {version}/{name}: {error}"));
    assert_eq!(
        serde_json::to_value(&dto).unwrap(),
        value,
        "{version}/{name} must round-trip exactly through the DTO"
    );
    dto
}

#[test]
fn v1_2_and_v1_3_health_are_supported_and_v1_0_is_too_old() {
    let health: Health = exact("v1.2", "health.response.json");
    assert_eq!((health.api_version.major, health.api_version.minor), (1, 2));
    assert!(health.api_version.is_supported());
    let health: Health = exact("v1.3", "health.response.json");
    assert_eq!((health.api_version.major, health.api_version.minor), (1, 3));
    assert!(health.api_version.is_supported());
    let old: Health = exact("v1", "health.response.json");
    assert!(!old.api_version.is_supported());
}

#[test]
fn stack_presets_carry_supports_defaults_and_unsupported_reason() {
    let _: StackPresets = exact("v1.2", "stack-presets.response.json");
    let presets: StackPresets = exact("v1.3", "stack-presets.response.json");
    assert_eq!(presets.default_preset_id, "mission1-harbor");
    let harbor = &presets.presets[0];
    assert_eq!(harbor.supports.airsim, [false, true]);
    assert_eq!(harbor.supports.perception, ["off"]);
    assert!(harbor.unsupported_reason.is_some());
    assert_eq!(harbor.defaults.simulation_limit_seconds, 600);
    let airsim = &presets.presets[1];
    assert_eq!(airsim.supports.airsim, [true]);
    assert_eq!(airsim.supports.perception, ["off", "ideal", "yolo"]);
    assert!(airsim.unsupported_reason.is_some());
}

#[test]
fn preflight_with_a_failing_check_does_not_allow_launch() {
    let preflight: StackPreflight = exact("v1.2", "stack-preflight.response.json");
    assert!(!preflight.launchable);
    assert!(!preflight.allows_launch());
    let statuses: Vec<&str> = preflight
        .checks
        .iter()
        .map(|check| check.status.as_str())
        .collect();
    assert_eq!(statuses, ["pass", "warn", "fail"]);
    assert!(preflight.toggles.airsim);
}

#[test]
fn activation_request_carries_stack_and_v1_request_still_round_trips() {
    let request: ActivationRequest = exact("v1.2", "mission-activation.request.json");
    let stack = request.stack.expect("v1.2 example has a stack");
    assert_eq!(stack.preset_id, "mission1-harbor");
    assert_eq!(stack.update_ownership, "coordinator_driven");
    let legacy: ActivationRequest = exact("v1", "mission-activation.request.json");
    assert_eq!(legacy.stack, None);
    assert!(legacy.mission_intent.contains('\n'));
}

#[test]
fn current_run_examples_carry_stack_and_terminal_detail() {
    let active: CurrentRun = exact("v1.2", "mission-runs.current.active.response.json");
    let run = active.mission_run.unwrap();
    assert_eq!(run.stack.as_ref().unwrap().preset_id, "mission1-harbor");
    assert_eq!(run.terminal_detail, None);
    assert!(!run.is_terminal());

    let rejected: CurrentRun = exact("v1.2", "mission-runs.current.rejected.response.json");
    let run = rejected.mission_run.unwrap();
    assert_eq!(run.status, "failed");
    assert_eq!(
        run.terminal_classification.as_deref(),
        Some("mission_rejected")
    );
    let detail = run.terminal_detail.unwrap();
    assert_eq!(detail.kind, "mission_rejected");
    assert_eq!(detail.stage.as_deref(), Some("intent"));
    assert!(detail.summary().contains("personal errand"));

    let worker: CurrentRun = exact("v1.2", "mission-runs.current.worker-failed.response.json");
    let detail = worker.mission_run.unwrap().terminal_detail.unwrap();
    assert_eq!(detail.error_type.as_deref(), Some("RuntimeError"));
    assert_eq!(
        detail.summary(),
        "Worker failed at closed_loop: RuntimeError: external environment has no planning data"
    );

    let stack: CurrentRun = exact("v1.2", "mission-runs.current.stack-failed.response.json");
    let detail = stack.mission_run.unwrap().terminal_detail.unwrap();
    assert_eq!(detail.service.as_deref(), Some("perception"));
    assert_eq!(
        detail.summary(),
        "Stack service perception failed: health not ready in 900 s"
    );
}

#[test]
fn v1_current_run_examples_still_decode() {
    let none: CurrentRun = exact("v1", "mission-runs.current.none.response.json");
    assert_eq!(none.mission_run, None);
    for name in [
        "mission-runs.current.active.response.json",
        "mission-runs.current.awaiting-human-decision.response.json",
    ] {
        let current: CurrentRun = serde_json::from_value(example("v1", name)).unwrap();
        let run = current.mission_run.unwrap();
        assert_eq!(run.stack, None);
        assert_eq!(run.terminal_detail, None);
    }
}

#[test]
fn unchanged_v1_examples_round_trip_exactly() {
    let accepted: ActivationAccepted = exact("v1", "mission-activation.accepted.response.json");
    assert_eq!(accepted.status, "queued");
    for (name, code) in [
        (
            "mission-activation.conflict.response.json",
            "activation_request_conflict",
        ),
        (
            "mission-activation.run-active.response.json",
            "mission_run_active",
        ),
        (
            "mission-activation.invalid.response.json",
            "invalid_request",
        ),
        (
            "mission-run-cancellation.conflict.response.json",
            "cancellation_request_conflict",
        ),
        (
            "mission-run-owner.authorization-failed.response.json",
            "authorization_failed",
        ),
        (
            "mission-run.not-found.response.json",
            "mission_run_not_found",
        ),
        (
            "mission-run-artifact.not-found.response.json",
            "artifact_not_found",
        ),
        (
            "mission-run-artifact.unavailable.response.json",
            "artifact_unavailable",
        ),
    ] {
        let error: ErrorBody = exact("v1", name);
        assert_eq!(error.error.code, code);
    }
    let intent: MissionIntent = exact("v1", "mission-intent.response.json");
    assert_eq!(intent.source_authority, "operator_console");
    let request: CancellationRequest = exact("v1", "mission-run-cancellation.request.json");
    let cancellation: CancellationAccepted =
        exact("v1", "mission-run-cancellation.accepted.response.json");
    assert_eq!(
        cancellation.cancellation_request_id,
        request.cancellation_request_id
    );
}

#[test]
fn artifact_content_and_entries_examples_round_trip_exactly() {
    let first: ArtifactContentPage =
        exact("v1", "mission-run-artifact-content.text-page.response.json");
    assert_eq!(first.next_offset, Some(4096));
    assert_eq!(first.end_offset(), 4096);
    let last: ArtifactContentPage = exact(
        "v1",
        "mission-run-artifact-content.text-final.response.json",
    );
    assert!(last.eof);
    let binary: ArtifactContentPage =
        exact("v1", "mission-run-artifact-content.binary.response.json");
    assert_eq!(binary.content, None);
    let entries: ConversationEntriesPage =
        exact("v1", "mission-run-artifact-entries.page.response.json");
    assert!(entries.entries[2].content_ref.is_some());
    let _: ConversationEntriesPage =
        exact("v1", "mission-run-artifact-entries.empty.response.json");
}

#[test]
fn service_log_artifacts_are_inspectable() {
    let page: ArtifactsPage = exact("v1.2", "mission-run-artifacts.service-log.response.json");
    let log = &page.artifacts[0];
    assert_eq!(log.classification, "service_log");
    assert!(log.is_inspectable());
    assert_eq!(log.byte_size, Some(20_480));
}

#[test]
fn world_frame_not_found_has_a_stable_code() {
    let error: ErrorBody = exact("v1.2", "mission-run-world-frame.not-found.response.json");
    assert_eq!(error.error.code, "frame_unavailable");
}

#[test]
fn overview_v1_2_adds_phase_and_importance_counts() {
    let page: OperatorOverviewPage = exact("v1.2", "mission-run-operator-overview.response.json");
    let phase = page.overview.phase.expect("v1.2 overview has a phase");
    assert_eq!(phase.current, "executing");
    assert_eq!(phase.steps.len(), 6);
    assert_eq!(phase.steps[4].status, "active");
    let counts = page.overview.counts_by_importance.unwrap();
    assert_eq!((counts.warning, counts.debug), (2, 646));
    let legacy: OperatorOverviewPage = exact("v1.1", "mission-run-operator-overview.response.json");
    assert_eq!(legacy.overview.phase, None);
}

#[test]
fn progress_section_is_a_flat_node_stream() {
    let page: OperatorProgressPage = exact("v1.2", "mission-run-operator-progress.response.json");
    let progress = page.progress;
    assert_eq!(progress.mapping_version, 1);
    assert_eq!(progress.narrative.source_watermark, 412);
    let parents: Vec<Option<&str>> = progress
        .nodes
        .iter()
        .map(|node| node.parent_id.as_deref())
        .collect();
    assert_eq!(
        parents,
        [Some("root"), Some("summary:7"), Some("root"), Some("live")]
    );
    assert!(!progress.nodes[0].authoritative);
}

#[test]
fn beliefs_section_has_entities_history_and_an_explicit_none_state() {
    let page: OperatorBeliefsPage = exact("v1.2", "mission-run-operator-beliefs.response.json");
    let beliefs = page.beliefs;
    assert_eq!(
        beliefs.belief_kind.as_deref(),
        Some("reporting_reliability")
    );
    assert_eq!(beliefs.entities[0].credible_interval, Some([0.41, 0.81]));
    assert_eq!(
        beliefs.entities[0].means_by_revision.len(),
        beliefs.history.len()
    );
    let none: OperatorBeliefsPage =
        exact("v1.2", "mission-run-operator-beliefs.none.response.json");
    assert_eq!(none.beliefs.belief_kind, None);
    assert!(none.beliefs.reason.unwrap().contains("no Bayesian belief"));
}

#[test]
fn context_world_and_stack_sections_round_trip_exactly() {
    let context: OperatorContextPage = exact("v1.2", "mission-run-operator-context.response.json");
    let fsm = context.context.fsm_status.unwrap();
    assert!(!fsm.transition_candidates.is_empty());
    let world: OperatorWorldPage = exact("v1.2", "mission-run-operator-world.response.json");
    assert!(world.world.viewer.available);
    assert_eq!(world.world.frames[0].source, "world");
    assert_eq!(world.world.airsim, None, "a v1.2 Host has no AirSim status");
    let stack: OperatorStackPage = exact("v1.2", "mission-run-operator-stack.response.json");
    assert_eq!(stack.stack.services[0].state, "ready");
    assert_eq!(
        stack.stack.services[0].log_artifact_id.as_deref(),
        Some("service-log-physical-runtime")
    );
}

#[test]
fn v1_3_world_adds_the_annotated_front_camera_and_airsim_status() {
    let follower: OperatorWorldPage =
        exact("v1.3", "mission-run-operator-world.follower.response.json");
    let sources: Vec<&str> = follower
        .world
        .frames
        .iter()
        .map(|frame| frame.source.as_str())
        .collect();
    let advertised: Vec<&str> = FrameSource::ALL.iter().map(|s| s.as_str()).collect();
    assert_eq!(sources, advertised);
    let airsim = follower.world.airsim.unwrap();
    assert_eq!(
        (airsim.mode.as_str(), airsim.perception.as_str()),
        ("world_model_follower", "off")
    );
    let annotation = airsim.annotation.unwrap();
    assert_eq!(annotation.kind, "ideal_segmentation");
    assert_eq!(annotation.perception_mission_time_seconds, None);

    let scene: OperatorWorldPage = exact(
        "v1.3",
        "mission-run-operator-world.scene-clock.response.json",
    );
    let airsim = scene.world.airsim.unwrap();
    assert_eq!(
        (airsim.mode.as_str(), airsim.perception.as_str()),
        ("scene_clock", "yolo")
    );
    assert_eq!(airsim.lag_seconds, None);
    let annotation = airsim.annotation.unwrap();
    assert_eq!(
        (annotation.kind.as_str(), annotation.match_.as_str()),
        ("perception_yolo", "exact")
    );
}

#[test]
fn unchanged_v1_1_sections_round_trip_exactly() {
    let agents: OperatorAgentsPage = exact("v1.1", "mission-run-operator-agents.response.json");
    assert_eq!(agents.agents[0].tool_calls[0].args["attempt"], 2);
    let environment: OperatorEnvironmentPage =
        exact("v1.1", "mission-run-operator-environment.response.json");
    assert!(environment.environment.world_model_info.is_null());
    let artifacts: OperatorArtifactsPage =
        exact("v1.1", "mission-run-operator-artifacts.response.json");
    assert_eq!(artifacts.artifacts[1].source.as_deref(), Some("planner"));
}

#[test]
fn environment_keeps_world_model_info_when_the_host_emits_it() {
    let mut value = example("v1.1", "mission-run-operator-environment.response.json");
    value["environment"]["world_model_info"] =
        serde_json::json!({"mission_mode": "mission1", "event_report_checks": []});
    let page: OperatorEnvironmentPage = serde_json::from_value(value.clone()).unwrap();
    assert_eq!(
        page.environment.world_model_info["mission_mode"],
        "mission1"
    );
    assert_eq!(serde_json::to_value(&page).unwrap(), value);
}

#[test]
fn every_section_decodes_into_its_own_page_variant() {
    for (section, version, name) in [
        (
            OperatorSection::Overview,
            "v1.2",
            "mission-run-operator-overview.response.json",
        ),
        (
            OperatorSection::Progress,
            "v1.2",
            "mission-run-operator-progress.response.json",
        ),
        (
            OperatorSection::Agents,
            "v1.1",
            "mission-run-operator-agents.response.json",
        ),
        (
            OperatorSection::Beliefs,
            "v1.2",
            "mission-run-operator-beliefs.response.json",
        ),
        (
            OperatorSection::Context,
            "v1.2",
            "mission-run-operator-context.response.json",
        ),
        (
            OperatorSection::World,
            "v1.2",
            "mission-run-operator-world.response.json",
        ),
        (
            OperatorSection::Environment,
            "v1.1",
            "mission-run-operator-environment.response.json",
        ),
        (
            OperatorSection::Stack,
            "v1.2",
            "mission-run-operator-stack.response.json",
        ),
        (
            OperatorSection::Artifacts,
            "v1.1",
            "mission-run-operator-artifacts.response.json",
        ),
    ] {
        let page = OperatorViewPage::decode(section, raw(version, name).as_bytes())
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(page.meta().section, section);
        let matches = matches!(
            (section, &page),
            (OperatorSection::Overview, OperatorViewPage::Overview(_))
                | (OperatorSection::Progress, OperatorViewPage::Progress(_))
                | (OperatorSection::Agents, OperatorViewPage::Agents(_))
                | (OperatorSection::Beliefs, OperatorViewPage::Beliefs(_))
                | (OperatorSection::Context, OperatorViewPage::Context(_))
                | (OperatorSection::World, OperatorViewPage::World(_))
                | (
                    OperatorSection::Environment,
                    OperatorViewPage::Environment(_)
                )
                | (OperatorSection::Stack, OperatorViewPage::Stack(_))
                | (OperatorSection::Artifacts, OperatorViewPage::Artifacts(_))
        );
        assert!(matches, "{name} decoded into the wrong variant");
    }
}
