//! Slice D: the host worker thread executes commands off the UI thread and
//! returns typed messages.

use std::sync::Mutex;
use std::sync::mpsc::channel;
use std::time::Duration;

use operator_console::app::{HostCommand, HostMessage};
use operator_console::host::{
    ActivationOutcome, ActivationRequest, ActivitiesPage, ApiVersion, ArtifactContentPage,
    ArtifactsPage, CancellationAccepted, CancellationOutcome, CancellationRequest,
    ConversationEntriesPage, CurrentRun, Health, HostClient, HostError, MissionIntent,
    NarrativeResponse, ObservationsPage, OperatorSection, OperatorViewPage, RunNarrative,
    RunRecord, spawn_worker,
};

#[derive(Default)]
struct ScriptedClient {
    calls: Mutex<Vec<String>>,
}

impl HostClient for ScriptedClient {
    fn health(&self) -> Result<Health, HostError> {
        self.calls.lock().unwrap().push("health".to_string());
        Ok(Health {
            status: "ok".to_string(),
            api_version: ApiVersion { major: 1, minor: 0 },
        })
    }

    fn activate(
        &self,
        request: &ActivationRequest,
        _credential: &str,
    ) -> Result<ActivationOutcome, HostError> {
        self.calls.lock().unwrap().push("activate".to_string());
        Ok(ActivationOutcome::Accepted(
            operator_console::host::ActivationAccepted {
                activation_request_id: request.activation_request_id.clone(),
                mission_id: "mission-1".to_string(),
                mission_run_id: "run-1".to_string(),
                status: "queued".to_string(),
                created_at: "2026-08-24T12:00:00Z".to_string(),
            },
        ))
    }

    fn current_run(&self, _credential: &str) -> Result<CurrentRun, HostError> {
        self.calls.lock().unwrap().push("current".to_string());
        Ok(CurrentRun {
            mission_run: Some(RunRecord {
                mission_id: "mission-1".to_string(),
                mission_run_id: "run-1".to_string(),
                status: "running".to_string(),
                created_at: Some("2026-08-24T12:00:00Z".to_string()),
                started_at: Some("2026-08-24T12:00:03Z".to_string()),
                finished_at: None,
                terminal_classification: None,
            }),
        })
    }

    fn mission_intent(
        &self,
        mission_run_id: &str,
        _credential: &str,
    ) -> Result<MissionIntent, HostError> {
        self.calls.lock().unwrap().push("intent".to_string());
        Ok(MissionIntent {
            mission_run_id: mission_run_id.to_string(),
            mission_intent: "survey the ridge".to_string(),
            source_authority: "operator_console".to_string(),
        })
    }

    fn cancel(
        &self,
        mission_run_id: &str,
        request: &CancellationRequest,
        _credential: &str,
    ) -> Result<CancellationOutcome, HostError> {
        self.calls.lock().unwrap().push("cancel".to_string());
        Ok(CancellationOutcome::Accepted(CancellationAccepted {
            mission_run_id: mission_run_id.to_string(),
            cancellation_request_id: request.cancellation_request_id.clone(),
            disposition: "cancellation_requested".to_string(),
            status: "running".to_string(),
            requested_at: "2026-08-24T12:05:00Z".to_string(),
        }))
    }

    fn observations(
        &self,
        mission_run_id: &str,
        _cursor: Option<&str>,
    ) -> Result<ObservationsPage, HostError> {
        self.calls.lock().unwrap().push("observations".to_string());
        Ok(ObservationsPage {
            schema_version: 1,
            mission_id: "mission-1".to_string(),
            mission_run_id: mission_run_id.to_string(),
            observations: Vec::new(),
            next_cursor: None,
        })
    }

    fn fetch_narrative(&self, mission_run_id: &str) -> Result<NarrativeResponse, HostError> {
        self.calls.lock().unwrap().push("narrative".to_string());
        Ok(NarrativeResponse {
            schema_version: 1,
            mission_id: "mission-1".to_string(),
            mission_run_id: mission_run_id.to_string(),
            narrative: RunNarrative {
                status: "none".to_string(),
                text: None,
                generated_at: None,
                source_watermark: 0,
                terminal: false,
                evidence: None,
            },
        })
    }

    fn activities(
        &self,
        mission_run_id: &str,
        _cursor: Option<&str>,
    ) -> Result<ActivitiesPage, HostError> {
        self.calls.lock().unwrap().push("activities".to_string());
        Ok(ActivitiesPage {
            schema_version: 1,
            mission_id: "mission-1".to_string(),
            mission_run_id: mission_run_id.to_string(),
            mapping_version: 1,
            activities: Vec::new(),
            next_cursor: None,
        })
    }

    fn artifacts(
        &self,
        mission_run_id: &str,
        _cursor: Option<&str>,
    ) -> Result<ArtifactsPage, HostError> {
        self.calls.lock().unwrap().push("artifacts".to_string());
        Ok(ArtifactsPage {
            schema_version: 1,
            mission_id: "mission-1".to_string(),
            mission_run_id: mission_run_id.to_string(),
            artifacts: Vec::new(),
            next_cursor: None,
        })
    }

    fn artifact_content(
        &self,
        mission_run_id: &str,
        artifact_id: &str,
        offset: Option<u64>,
        _limit: Option<u64>,
    ) -> Result<ArtifactContentPage, HostError> {
        self.calls.lock().unwrap().push("content".to_string());
        Ok(ArtifactContentPage {
            schema_version: 1,
            mission_id: "mission-1".to_string(),
            mission_run_id: mission_run_id.to_string(),
            artifact_id: artifact_id.to_string(),
            classification: "text".to_string(),
            media_type: "text/plain".to_string(),
            byte_size: Some(0),
            offset: offset.unwrap_or(0),
            next_offset: None,
            eof: true,
            truncated: false,
            content: Some(String::new()),
        })
    }

    fn conversation_entries(
        &self,
        mission_run_id: &str,
        artifact_id: &str,
        _cursor: Option<&str>,
    ) -> Result<ConversationEntriesPage, HostError> {
        self.calls.lock().unwrap().push("entries".to_string());
        Ok(ConversationEntriesPage {
            schema_version: 1,
            mission_id: "mission-1".to_string(),
            mission_run_id: mission_run_id.to_string(),
            artifact_id: artifact_id.to_string(),
            entries: Vec::new(),
            next_cursor: None,
        })
    }

    fn operator_view(
        &self,
        _mission_run_id: &str,
        _section: OperatorSection,
        _cursor: Option<&str>,
        _raw: bool,
    ) -> Result<OperatorViewPage, HostError> {
        self.calls.lock().unwrap().push("operator-view".to_string());
        Err(HostError::Transport("not scripted".to_string()))
    }
}

fn recv_timeout(rx: &std::sync::mpsc::Receiver<HostMessage>) -> HostMessage {
    rx.recv_timeout(Duration::from_secs(2))
        .expect("worker should answer within 2s")
}

#[test]
fn worker_executes_commands_and_reports_in_order() {
    let (command_tx, command_rx) = channel();
    let (message_tx, message_rx) = channel();
    let client = ScriptedClient::default();
    let handle = spawn_worker(client, command_rx, message_tx);

    command_tx.send(HostCommand::Connect).unwrap();
    match recv_timeout(&message_rx) {
        HostMessage::Connected(Ok(health)) => {
            assert_eq!((health.api_version.major, health.api_version.minor), (1, 0));
        }
        other => panic!("expected Connected, got {other:?}"),
    }

    command_tx
        .send(HostCommand::FetchIntent {
            mission_run_id: "run-1".to_string(),
            credential: "cred".to_string(),
        })
        .unwrap();
    assert!(matches!(
        recv_timeout(&message_rx),
        HostMessage::Intent(Ok(_))
    ));

    command_tx
        .send(HostCommand::Cancel {
            mission_run_id: "run-1".to_string(),
            request: CancellationRequest {
                cancellation_request_id: "cancel-1".to_string(),
            },
            credential: "cred".to_string(),
        })
        .unwrap();
    assert!(matches!(
        recv_timeout(&message_rx),
        HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(_)))
    ));

    command_tx
        .send(HostCommand::Submit {
            request: Box::new(ActivationRequest {
                activation_request_id: "req-1".to_string(),
                console_session_id: "session-1".to_string(),
                mission_intent: "survey the ridge".to_string(),
                source_authority: "operator_console".to_string(),
            }),
            credential: "cred".to_string(),
        })
        .unwrap();
    match recv_timeout(&message_rx) {
        HostMessage::Activated(Ok(ActivationOutcome::Accepted(accepted))) => {
            assert_eq!(accepted.mission_run_id, "run-1");
        }
        other => panic!("expected Activated, got {other:?}"),
    }

    command_tx
        .send(HostCommand::PollCurrent {
            credential: "cred".to_string(),
        })
        .unwrap();
    match recv_timeout(&message_rx) {
        HostMessage::Current(Ok(current)) => {
            assert_eq!(current.mission_run.unwrap().status, "running");
        }
        other => panic!("expected Current, got {other:?}"),
    }

    command_tx
        .send(HostCommand::FetchNarrative {
            mission_run_id: "run-1".to_string(),
        })
        .unwrap();
    match recv_timeout(&message_rx) {
        HostMessage::Narrative {
            mission_run_id,
            result: Ok(_),
        } => assert_eq!(mission_run_id, "run-1"),
        other => panic!("expected Narrative, got {other:?}"),
    }

    command_tx
        .send(HostCommand::FetchConversationEntries {
            mission_run_id: "run-1".to_string(),
            artifact_id: "conversation-1".to_string(),
        })
        .unwrap();
    match recv_timeout(&message_rx) {
        HostMessage::ConversationEntries {
            mission_run_id,
            artifact_id,
            result: Ok(page),
        } => {
            assert_eq!(mission_run_id, "run-1");
            assert_eq!(artifact_id, "conversation-1");
            assert!(page.items.is_empty());
        }
        other => panic!("expected ConversationEntries, got {other:?}"),
    }

    drop(command_tx);
    handle.join().expect("worker exits when the channel closes");
}
