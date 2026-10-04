//! The production ureq client against the in-process v1.5 fixture Host.

mod support;

use std::time::Duration;

use operator_console::host::{
    ActivationOutcome, ActivationRequest, CancellationOutcome, CancellationRequest, Fetched,
    FrameSource, HostClient, HostError, OperatorSection, OperatorViewPage, PreflightQuery,
    StackSelection, StackToggles, UreqHostClient,
};
use support::{FixtureHost, SERVICE_LOG_ARTIFACT};

fn client(host: &FixtureHost) -> UreqHostClient {
    UreqHostClient::new(&host.url(), Duration::from_secs(2))
}

fn stack(preset_id: &str) -> StackSelection {
    StackSelection {
        preset_id: preset_id.to_string(),
        airsim: false,
        perception: "off".to_string(),
        update_ownership: "coordinator_driven".to_string(),
        simulation_limit_seconds: 600,
    }
}

fn request(request_id: &str, intent: &str, stack: Option<StackSelection>) -> ActivationRequest {
    ActivationRequest {
        activation_request_id: request_id.to_string(),
        console_session_id: "session-1".to_string(),
        mission_intent: intent.to_string(),
        source_authority: "operator_console".to_string(),
        stack,
    }
}

fn activate(client: &UreqHostClient) -> String {
    match client
        .activate(
            &request("request-1", "survey", Some(stack("mission1-harbor"))),
            "cred",
        )
        .unwrap()
    {
        ActivationOutcome::Accepted(accepted) => accepted.mission_run_id,
        other => panic!("expected acceptance, got {other:?}"),
    }
}

#[test]
fn health_reports_api_v1_5() {
    let host = FixtureHost::start();
    let health = client(&host).health().unwrap();
    assert_eq!((health.api_version.major, health.api_version.minor), (1, 5));
}

#[test]
fn unreachable_host_is_a_transport_error_that_proves_nothing() {
    let error = UreqHostClient::new("http://127.0.0.1:1", Duration::from_millis(200))
        .health()
        .unwrap_err();
    assert!(matches!(error, HostError::Transport(_)));
    assert!(!error.proves_host_reachable());
}

#[test]
fn a_host_that_does_not_answer_in_time_is_a_timeout_that_proves_nothing() {
    // Accepted by the kernel backlog, never answered.
    let silent = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", silent.local_addr().unwrap());
    let error = UreqHostClient::new(&url, Duration::from_millis(200))
        .health()
        .unwrap_err();
    assert!(matches!(error, HostError::Timeout(_)), "{error:?}");
    assert!(!error.proves_host_reachable());
    assert!(
        error.to_string().starts_with("request timed out"),
        "{error}"
    );
}

#[test]
fn presets_and_preflight_echo_the_queried_toggles() {
    let host = FixtureHost::start();
    let client = client(&host);
    let presets = client.stack_presets().unwrap();
    assert_eq!(presets.presets.len(), 8);
    assert!(presets.toggle_choices.is_some());
    let query = PreflightQuery {
        preset_id: "mission1-airsim".to_string(),
        toggles: StackToggles {
            airsim: true,
            perception: "yolo".to_string(),
            update_ownership: "environment_driven".to_string(),
        },
    };
    let preflight = client.stack_preflight(&query).unwrap();
    assert_eq!(preflight.preset_id, query.preset_id);
    assert_eq!(preflight.toggles, query.toggles);
    assert!(preflight.allows_launch());
    assert!(host.requests().iter().any(|path| path
        == "/api/v1/stack/preflight?preset_id=mission1-airsim&airsim=true&perception=yolo&update_ownership=environment_driven"));

    host.set_preflight_fails(true);
    let blocked = client.stack_preflight(&query).unwrap();
    assert!(!blocked.allows_launch());
}

#[test]
fn activation_sends_stack_and_replays_or_conflicts_on_the_stack_identity() {
    let host = FixtureHost::start();
    let client = client(&host);
    let first = client
        .activate(
            &request("request-1", "survey", Some(stack("mission1-harbor"))),
            "cred",
        )
        .unwrap();
    assert_eq!(
        host.last_activation_body().unwrap()["stack"]["preset_id"],
        "mission1-harbor"
    );
    let replay = client
        .activate(
            &request("request-1", "survey", Some(stack("mission1-harbor"))),
            "cred",
        )
        .unwrap();
    assert_eq!(first, replay);
    let conflict = client
        .activate(
            &request("request-1", "survey", Some(stack("mission1-airsim"))),
            "cred",
        )
        .unwrap();
    assert!(matches!(
        conflict,
        ActivationOutcome::Rejected { ref code, .. } if code == "activation_request_conflict"
    ));
    let active = client
        .activate(&request("request-2", "other", None), "cred")
        .unwrap();
    assert!(matches!(
        active,
        ActivationOutcome::Rejected { ref code, .. } if code == "mission_run_active"
    ));
    assert_eq!(host.last_authorization().as_deref(), Some("Bearer cred"));
}

#[test]
fn current_run_reports_stack_and_terminal_detail() {
    let host = FixtureHost::start();
    let client = client(&host);
    assert_eq!(client.current_run("cred").unwrap().mission_run, None);
    activate(&client);
    let run = client.current_run("cred").unwrap().mission_run.unwrap();
    assert_eq!(run.status, "queued");
    assert_eq!(run.stack.unwrap().preset_id, "mission1-harbor");
    host.reject_run("'buy me a coffee' is a personal errand");
    let run = client.current_run("cred").unwrap().mission_run.unwrap();
    assert!(run.is_terminal());
    assert_eq!(
        run.terminal_classification.as_deref(),
        Some("mission_rejected")
    );
    assert_eq!(
        run.terminal_detail.unwrap().reason.as_deref(),
        Some("'buy me a coffee' is a personal errand")
    );
}

#[test]
fn run_history_pages_follow_the_before_cursor() {
    let host = FixtureHost::start();
    let client = client(&host);
    let page = client.mission_runs(None, 50).unwrap();
    assert_eq!(page.mission_runs.len(), 4);
    let before = page.next_before.unwrap();
    let last = client.mission_runs(Some(&before), 50).unwrap();
    assert!(last.mission_runs.is_empty());
    assert_eq!(last.next_before, None);
    assert!(matches!(
        client.mission_runs(Some("run-unknown"), 50),
        Err(HostError::InvalidCursor { .. })
    ));
}

#[test]
fn owner_endpoints_authorize_and_cancellation_is_idempotent() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate(&client);
    assert_eq!(
        client
            .mission_intent(&run_id, "cred")
            .unwrap()
            .mission_intent,
        "survey"
    );
    assert!(matches!(
        client.mission_intent(&run_id, "other").unwrap_err(),
        HostError::AuthorizationFailed { .. }
    ));
    let cancel = CancellationRequest {
        cancellation_request_id: "cancel-1".to_string(),
    };
    let first = client.cancel(&run_id, &cancel, "cred").unwrap();
    assert_eq!(first, client.cancel(&run_id, &cancel, "cred").unwrap());
    assert!(matches!(first, CancellationOutcome::Accepted(_)));
    assert!(matches!(
        client.cancel(&run_id, &cancel, "other").unwrap_err(),
        HostError::AuthorizationFailed { .. }
    ));
}

#[test]
fn every_operator_section_decodes_and_etags_revalidate_with_304() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate(&client);
    for section in OperatorSection::ALL {
        let Fetched::Fresh { value, etag } = client
            .operator_view(&run_id, section, &Default::default(), false, None)
            .unwrap()
        else {
            panic!("first fetch of {section:?} must be fresh");
        };
        assert_eq!(value.meta().section, section);
        assert_eq!(value.meta().mission_run_id, run_id);
        let etag = etag.expect("fixture sends an ETag");
        assert_eq!(
            client
                .operator_view(&run_id, section, &Default::default(), false, Some(&etag))
                .unwrap(),
            Fetched::NotModified
        );
    }
    host.promote_to_running();
    let refreshed = client
        .operator_view(
            &run_id,
            OperatorSection::Stack,
            &Default::default(),
            false,
            Some("\"stack-queued\""),
        )
        .unwrap();
    assert!(matches!(
        refreshed,
        Fetched::Fresh {
            value: OperatorViewPage::Stack(_),
            ..
        }
    ));
    assert!(matches!(
        client
            .operator_view(
                "run-unknown",
                OperatorSection::Overview,
                &Default::default(),
                false,
                None
            )
            .unwrap_err(),
        HostError::NotFound { .. }
    ));
}

#[test]
fn world_frame_is_404_until_available_then_bytes_with_headers_and_304() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate(&client);
    match client
        .world_frame(&run_id, FrameSource::World, None)
        .unwrap_err()
    {
        HostError::NotFound { code, .. } => assert_eq!(code, "frame_unavailable"),
        other => panic!("expected frame_unavailable, got {other:?}"),
    }
    host.set_world_frame(b"\x89PNG fake");
    let Fetched::Fresh { value, etag } = client
        .world_frame(&run_id, FrameSource::World, None)
        .unwrap()
    else {
        panic!("frame must be fresh");
    };
    assert_eq!(value.bytes, b"\x89PNG fake");
    assert_eq!(value.media_type, "image/png");
    assert_eq!(value.sequence, Some(812));
    assert_eq!(value.mission_time.as_deref(), Some("143.5"));
    assert_eq!(
        client
            .world_frame(&run_id, FrameSource::World, etag.as_deref())
            .unwrap(),
        Fetched::NotModified
    );
}

#[test]
fn artifact_content_pages_text_binary_service_logs_and_errors() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate(&client);
    let first = client
        .artifact_content(&run_id, "planner-log", Some(0), Some(4096))
        .unwrap();
    assert_eq!(first.next_offset, Some(4096));
    let last = client
        .artifact_content(&run_id, "planner-log", Some(4096), Some(4096))
        .unwrap();
    assert!(last.eof);
    let binary = client
        .artifact_content(&run_id, "detection-frame", Some(0), Some(4096))
        .unwrap();
    assert_eq!(binary.content, None);
    assert!(matches!(
        client
            .artifact_content(&run_id, "planner-log", Some(7), Some(4096))
            .unwrap_err(),
        HostError::InvalidRequest { .. }
    ));
    assert!(matches!(
        client
            .artifact_content(&run_id, "missing", Some(0), Some(4096))
            .unwrap_err(),
        HostError::NotFound { .. }
    ));

    host.append_service_log("ready\n");
    let log = client
        .artifact_content(&run_id, SERVICE_LOG_ARTIFACT, Some(0), Some(4096))
        .unwrap();
    assert_eq!(log.classification, "service_log");
    assert_eq!(log.content.as_deref(), Some("ready\n"));
    assert!(log.eof);
}

#[test]
fn conversation_entries_collect_across_cursor_pages_and_cap_endless_paging() {
    let host = FixtureHost::start();
    let client = client(&host);
    let run_id = activate(&client);
    let page = client
        .all_conversation_entries(&run_id, "operator-conversation")
        .unwrap();
    assert_eq!(page.items.len(), 3);
    assert!(!page.truncated);
    assert!(matches!(
        client
            .conversation_entries(&run_id, "operator-conversation", Some("bogus"))
            .unwrap_err(),
        HostError::InvalidCursor { .. }
    ));
    host.enable_endless_evidence();
    let capped = client
        .all_conversation_entries(&run_id, "operator-conversation")
        .unwrap();
    assert!(capped.truncated);
    assert_eq!(capped.items.len(), 300);
}
