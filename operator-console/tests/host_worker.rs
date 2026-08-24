//! Slice D: the host worker thread executes commands off the UI thread and
//! returns typed messages.

use std::sync::Mutex;
use std::sync::mpsc::channel;
use std::time::Duration;

use operator_console::app::{HostCommand, HostMessage};
use operator_console::host::{
    ActivationOutcome, ActivationRequest, ApiVersion, CurrentRun, Health, HostClient, HostError,
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

    drop(command_tx);
    handle.join().expect("worker exits when the channel closes");
}
