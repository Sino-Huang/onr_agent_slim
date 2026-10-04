//! Control, evidence, and media workers: lane isolation, per-key coalescing
//! of identical polls, and ordered, never-dropped mutations.

mod common;

use std::collections::HashSet;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::time::Duration;

use operator_console::host::workers::Dispatch;
use operator_console::host::{
    ActivationAccepted, ActivationOutcome, ActivationRequest, ApiVersion, ArtifactContentPage,
    CancellationAccepted, CancellationOutcome, CancellationRequest, ConversationEntriesPage,
    CurrentRun, Fetched, FrameSource, Health, HostClient, HostCommand, HostError, HostMessage,
    MissionIntent, MissionRunsPage, OperatorSection, OperatorViewPage, PreflightQuery,
    ReceiptExportOutcome, StackPreflight, StackPresets, StackToggles, Workers, WorldFrame,
};
use parking_lot::{Condvar, Mutex};

/// Client whose `current_run` blocks until the gate opens, announcing entry.
struct GatedClient {
    open: Mutex<bool>,
    opened: Condvar,
    entered: Mutex<Sender<&'static str>>,
}

impl GatedClient {
    fn shared() -> (Shared, Receiver<&'static str>) {
        let (tx, rx) = channel();
        (
            Shared(std::sync::Arc::new(Self {
                open: Mutex::new(false),
                opened: Condvar::new(),
                entered: Mutex::new(tx),
            })),
            rx,
        )
    }
    fn release(&self) {
        *self.open.lock() = true;
        self.opened.notify_all();
    }

    fn wait_open(&self) {
        let mut open = self.open.lock();
        while !*open {
            self.opened.wait(&mut open);
        }
    }
}

fn unscripted<T>() -> Result<T, HostError> {
    Err(HostError::Transport("not scripted".to_string()))
}

/// Local handle so the test can keep the gate while a worker owns a clone.
#[derive(Clone)]
struct Shared(std::sync::Arc<GatedClient>);

impl std::ops::Deref for Shared {
    type Target = GatedClient;

    fn deref(&self) -> &GatedClient {
        &self.0
    }
}

impl HostClient for Shared {
    fn health(&self) -> Result<Health, HostError> {
        Ok(Health {
            status: "ok".to_string(),
            api_version: ApiVersion { major: 1, minor: 2 },
        })
    }

    fn activate(
        &self,
        request: &ActivationRequest,
        _credential: &str,
    ) -> Result<ActivationOutcome, HostError> {
        Ok(ActivationOutcome::Accepted(ActivationAccepted {
            activation_request_id: request.activation_request_id.clone(),
            mission_id: "mission-1".to_string(),
            mission_run_id: "run-1".to_string(),
            status: "queued".to_string(),
            created_at: "2026-08-24T12:00:00Z".to_string(),
        }))
    }

    fn current_run(&self, _credential: &str) -> Result<CurrentRun, HostError> {
        let _ = self.entered.lock().send("current");
        self.wait_open();
        Ok(CurrentRun { mission_run: None })
    }

    fn mission_runs(&self, _: Option<&str>, _: u32) -> Result<MissionRunsPage, HostError> {
        unscripted()
    }

    fn mission_intent(&self, _: &str, _: &str) -> Result<MissionIntent, HostError> {
        unscripted()
    }

    fn cancel(
        &self,
        mission_run_id: &str,
        request: &CancellationRequest,
        _credential: &str,
    ) -> Result<CancellationOutcome, HostError> {
        Ok(CancellationOutcome::Accepted(CancellationAccepted {
            mission_run_id: mission_run_id.to_string(),
            cancellation_request_id: request.cancellation_request_id.clone(),
            disposition: "cancellation_requested".to_string(),
            status: "running".to_string(),
            requested_at: "2026-08-24T12:00:05Z".to_string(),
        }))
    }

    fn export_receipt(&self, _: &str, _: &str) -> Result<ReceiptExportOutcome, HostError> {
        unscripted()
    }

    fn stack_presets(&self) -> Result<StackPresets, HostError> {
        unscripted()
    }

    fn stack_preflight(&self, _: &PreflightQuery) -> Result<StackPreflight, HostError> {
        unscripted()
    }

    fn artifact_content(
        &self,
        _: &str,
        _: &str,
        _: Option<u64>,
        _: Option<u64>,
    ) -> Result<ArtifactContentPage, HostError> {
        unscripted()
    }

    fn conversation_entries(
        &self,
        _: &str,
        _: &str,
        _: Option<&str>,
    ) -> Result<ConversationEntriesPage, HostError> {
        unscripted()
    }

    fn operator_view(
        &self,
        _: &str,
        _: OperatorSection,
        _: &operator_console::host::OperatorCursor,
        _: bool,
        _: Option<&str>,
    ) -> Result<Fetched<OperatorViewPage>, HostError> {
        Ok(Fetched::NotModified)
    }

    fn world_frame(
        &self,
        _: &str,
        _: FrameSource,
        _: Option<&str>,
    ) -> Result<Fetched<WorldFrame>, HostError> {
        Ok(Fetched::NotModified)
    }
}

fn recv(workers: &Workers) -> HostMessage {
    workers
        .recv_timeout(Duration::from_secs(5))
        .expect("worker should answer")
}

fn poll_current() -> HostCommand {
    HostCommand::PollCurrent {
        credential: "cred".to_string(),
    }
}

fn overview() -> HostCommand {
    HostCommand::FetchOperatorView {
        mission_run_id: "run-1".to_string(),
        section: OperatorSection::Overview,
        cursor: Default::default(),
        raw: false,
        etag: Some("\"etag\"".to_string()),
        request_id: 1,
    }
}

fn preflight(n: u64) -> HostCommand {
    HostCommand::Preflight {
        request_id: n,
        query: PreflightQuery {
            preset_id: format!("preset-{n}"),
            toggles: StackToggles {
                airsim: false,
                perception: "off".to_string(),
                update_ownership: "coordinator_driven".to_string(),
            },
        },
    }
}

fn cancel(id: &str) -> HostCommand {
    HostCommand::Cancel {
        mission_run_id: "run-1".to_string(),
        request: CancellationRequest {
            cancellation_request_id: id.to_string(),
        },
        credential: "cred".to_string(),
    }
}

#[test]
fn a_blocked_control_request_does_not_stall_evidence_or_media() {
    let (client, entered) = GatedClient::shared();
    let mut workers = Workers::spawn(client.clone());
    assert_eq!(workers.dispatch(poll_current()), Dispatch::Queued);
    assert_eq!(entered.recv().unwrap(), "current");

    assert_eq!(workers.dispatch(overview()), Dispatch::Queued);
    match recv(&workers) {
        HostMessage::OperatorView {
            section: OperatorSection::Overview,
            result: Ok(Fetched::NotModified),
            ..
        } => {}
        other => panic!("expected the evidence answer first, got {other:?}"),
    }
    workers.dispatch(HostCommand::FetchWorldFrame {
        mission_run_id: "run-1".to_string(),
        source: FrameSource::World,
        etag: None,
    });
    assert!(matches!(recv(&workers), HostMessage::WorldFrame { .. }));

    client.release();
    assert!(matches!(recv(&workers), HostMessage::Current(Ok(_))));
    workers.shutdown();
}

#[test]
fn identical_polls_coalesce_while_in_flight_and_mutations_never_do() {
    let (client, entered) = GatedClient::shared();
    let mut workers = Workers::spawn(client.clone());
    assert_eq!(workers.dispatch(poll_current()), Dispatch::Queued);
    entered.recv().unwrap();
    assert!(matches!(
        workers.dispatch(poll_current()),
        Dispatch::Coalesced { .. }
    ));
    // A different poll on the same lane is its own key.
    assert_eq!(workers.dispatch(preflight(1)), Dispatch::Queued);
    let mut repeated = preflight(1);
    if let HostCommand::Preflight { request_id, .. } = &mut repeated {
        *request_id = 999;
    }
    assert!(matches!(
        workers.dispatch(repeated),
        Dispatch::Coalesced {
            original_request_id: Some(1),
            ..
        }
    ));
    // Identical mutations are both sent.
    assert_eq!(workers.dispatch(cancel("c-1")), Dispatch::Queued);
    assert_eq!(workers.dispatch(cancel("c-1")), Dispatch::Queued);

    client.release();
    let mut cancels = 0;
    let mut currents = 0;
    for _ in 0..4 {
        match recv(&workers) {
            HostMessage::Current(_) => currents += 1,
            HostMessage::Cancelled(Ok(_)) => cancels += 1,
            HostMessage::Preflight { .. } => {}
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!((currents, cancels), (1, 2));
    // Once answered, the same poll is issued again.
    assert_eq!(workers.dispatch(poll_current()), Dispatch::Queued);
    assert!(matches!(recv(&workers), HostMessage::Current(_)));
    workers.shutdown();
}

#[test]
fn a_full_queue_drops_polls_but_defers_mutations_in_order() {
    let (client, entered) = GatedClient::shared();
    let mut workers = Workers::spawn(client.clone());
    workers.dispatch(poll_current());
    entered.recv().unwrap();
    let mut queued = 0;
    let mut n = 0;
    loop {
        n += 1;
        match workers.dispatch(preflight(n)) {
            Dispatch::Queued => queued += 1,
            Dispatch::Dropped(_) => break,
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(queued, 16, "control queue is bounded at 16");
    assert_eq!(workers.dispatch(cancel("first")), Dispatch::Deferred);
    assert_eq!(workers.dispatch(cancel("second")), Dispatch::Deferred);
    workers.flush_backlog();

    client.release();
    let mut order = Vec::new();
    while order.len() < 2 {
        workers.flush_backlog();
        if let HostMessage::Cancelled(Ok(CancellationOutcome::Accepted(accepted))) = recv(&workers)
        {
            order.push(accepted.cancellation_request_id);
        }
    }
    assert_eq!(order, ["first", "second"]);
    workers.shutdown();
}

#[test]
fn viewing_history_keeps_the_current_run_polled_through_the_workers() {
    let (client, entered) = GatedClient::shared();
    client.release();
    let mut workers = Workers::spawn(client.clone());
    let (mut app, _clock) = common::hydrated_run_app("worker-history", |_| {});
    app.health = Some(common::health_v1_5());
    common::open_history(&mut app);
    common::select_history_row(&mut app, common::HISTORICAL_RUN_ID);
    app.handle_key(common::key(crossterm::event::KeyCode::Enter));
    assert!(app.viewing_history());
    for command in app.take_commands() {
        app.handle_dispatch(workers.dispatch(command));
    }
    // The control worker serves `/current` for the parked current run ...
    assert_eq!(
        entered.recv_timeout(Duration::from_secs(5)).unwrap(),
        "current"
    );
    // ... and the evidence worker reads both runs.
    let mut runs = HashSet::new();
    let mut currents = 0;
    while let Some(message) = workers.recv_timeout(Duration::from_millis(500)) {
        match message {
            HostMessage::OperatorView { mission_run_id, .. } => {
                runs.insert(mission_run_id);
            }
            HostMessage::Current(_) => currents += 1,
            _ => {}
        }
    }
    assert_eq!(currents, 1);
    assert_eq!(
        runs,
        HashSet::from([
            common::RUN_ID.to_string(),
            common::HISTORICAL_RUN_ID.to_string()
        ])
    );
    workers.shutdown();
}
