//! Rust interoperability with production API v1.5 routes and a real Host worker lifecycle.

use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use operator_console::host::{
    ActivationOutcome, ActivationRequest, Fetched, HostClient, HostError, OperatorSection,
    OperatorViewPage, ReceiptExportOutcome, UreqHostClient,
};

struct PythonHost {
    child: Child,
    root: PathBuf,
}

impl Drop for PythonHost {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

#[test]
fn real_python_host_and_rust_client_interoperate_for_all_operator_sections() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let root = repo_root
        .join("var/tmp")
        .join(format!("operator-python-interop-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root).unwrap();
    let child = Command::new("python")
        .current_dir(&repo_root)
        .arg(repo_root.join("tests/support/operator_runtime_host.py"))
        .arg(port.to_string())
        .arg(&root)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("start Python Runtime Host fixture");
    let mut fixture = PythonHost { child, root };
    let client = UreqHostClient::new(&format!("http://127.0.0.1:{port}"), Duration::from_secs(2));
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(health) = client.health() {
            assert_eq!((health.api_version.major, health.api_version.minor), (1, 5));
            break;
        }
        assert!(
            Instant::now() < deadline,
            "Python Runtime Host did not become ready"
        );
        assert!(
            fixture.child.try_wait().unwrap().is_none(),
            "Python fixture exited early"
        );
        sleep(Duration::from_millis(25));
    }

    let activation = client
        .activate(
            &ActivationRequest {
                activation_request_id: "request-interop".to_string(),
                console_session_id: "session-interop".to_string(),
                mission_intent: "Survey the ridge".to_string(),
                source_authority: "operator_console".to_string(),
                stack: None,
            },
            "credential-interop",
        )
        .unwrap();
    let ActivationOutcome::Accepted(accepted) = activation else {
        panic!("fixture activation was rejected");
    };

    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let current = client.current_run("credential-interop").unwrap();
        if let Some(run) = current.mission_run
            && run.is_terminal()
        {
            assert_eq!(run.status, "succeeded");
            break;
        }
        assert!(Instant::now() < deadline, "stub worker did not finish");
        sleep(Duration::from_millis(25));
    }
    assert_eq!(
        std::fs::read_to_string(
            fixture
                .root
                .join("runs")
                .join(&accepted.mission_run_id)
                .join("agent-storage/interop-worker-ran")
        )
        .unwrap(),
        accepted.mission_run_id
    );

    for section in OperatorSection::ALL {
        let Fetched::Fresh { value: page, .. } = client
            .operator_view(
                &accepted.mission_run_id,
                section,
                &Default::default(),
                false,
                None,
            )
            .unwrap()
        else {
            panic!("an unconditional request is never 304");
        };
        assert_eq!(page.meta().mission_run_id, accepted.mission_run_id);
        assert_eq!(page.meta().section, section);
        match (section, page) {
            (OperatorSection::Overview, OperatorViewPage::Overview(page)) => {
                // A terminal run carries the v1.5 receipt; with no audit
                // artifact the verdict is `not_recorded`, never inferred.
                let receipt = page.overview.receipt.expect("terminal receipt");
                assert_eq!(receipt.status, "succeeded");
                assert_eq!(receipt.audit.status, "not_recorded");
            }
            (OperatorSection::Agents, OperatorViewPage::Agents(_))
            | (OperatorSection::Progress, OperatorViewPage::Progress(_))
            | (OperatorSection::Beliefs, OperatorViewPage::Beliefs(_))
            | (OperatorSection::Context, OperatorViewPage::Context(_))
            | (OperatorSection::World, OperatorViewPage::World(_))
            | (OperatorSection::Stack, OperatorViewPage::Stack(_))
            | (OperatorSection::Environment, OperatorViewPage::Environment(_))
            | (OperatorSection::Artifacts, OperatorViewPage::Artifacts(_)) => {}
            _ => panic!("operator section decoded into the wrong Rust DTO"),
        }
    }

    let ReceiptExportOutcome::Exported(exported) = client
        .export_receipt(&accepted.mission_run_id, "credential-interop")
        .unwrap()
    else {
        panic!("the owner's receipt export was rejected");
    };
    let path = fixture
        .root
        .join("runs")
        .join(&accepted.mission_run_id)
        .join("mission-run-receipt.json");
    assert_eq!(PathBuf::from(&exported.path), path);
    let document: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert_eq!(document["mission_run_id"], accepted.mission_run_id.as_str());
    assert!(matches!(
        client.export_receipt(&accepted.mission_run_id, "someone-else"),
        Err(HostError::AuthorizationFailed { .. })
    ));
}
