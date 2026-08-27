//! Real Rust-client/Python-Host interoperability for the additive v1.1 view.

use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use operator_console::host::{
    ActivationOutcome, ActivationRequest, HostClient, OperatorSection, OperatorViewPage,
    UreqHostClient,
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
    let root =
        std::env::temp_dir().join(format!("operator-python-interop-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root).unwrap();
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let child = Command::new("python")
        .current_dir(&repo_root)
        .arg(repo_root.join("tests/support/operator_host_fixture.py"))
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
            assert_eq!((health.api_version.major, health.api_version.minor), (1, 1));
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
            },
            "credential-interop",
        )
        .unwrap();
    let ActivationOutcome::Accepted(accepted) = activation else {
        panic!("fixture activation was rejected");
    };

    for section in [
        OperatorSection::Overview,
        OperatorSection::Agents,
        OperatorSection::Environment,
        OperatorSection::Artifacts,
    ] {
        let page = client
            .operator_view(&accepted.mission_run_id, section, None, false)
            .unwrap();
        assert_eq!(page.meta().mission_run_id, accepted.mission_run_id);
        assert_eq!(page.meta().section, section);
        match (section, page) {
            (OperatorSection::Overview, OperatorViewPage::Overview(_))
            | (OperatorSection::Agents, OperatorViewPage::Agents(_))
            | (OperatorSection::Environment, OperatorViewPage::Environment(_))
            | (OperatorSection::Artifacts, OperatorViewPage::Artifacts(_)) => {}
            _ => panic!("operator section decoded into the wrong Rust DTO"),
        }
    }
}
