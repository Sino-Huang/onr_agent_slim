//! Operator Console entry point: terminal lifecycle, event loop, and the
//! host worker thread. HTTP polling stays outside drawing; see
//! `docs/design/operator-console/terminal-lifecycle.md` for the cleanup and
//! panic restoration design.

use std::io;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use crossterm::event::{self, Event};
use operator_console::app::{App, CleanExitAction};
use operator_console::host::{HostClient, UreqHostClient, spawn_worker};
use operator_console::terminal::{TerminalGuard, install_panic_hook};
use operator_console::ui;

/// Default loopback Runtime Host address.
const DEFAULT_HOST: &str = "http://127.0.0.1:8787";
/// Event poll tick; drawing resumes at least this often.
const TICK: Duration = Duration::from_millis(50);
/// Mission Run polling cadence in the Run state.
const POLL_INTERVAL: Duration = Duration::from_millis(400);
/// Bound on any single host request so the worker never wedges the console.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
const BOOTSTRAP_READY_TIMEOUT: Duration = Duration::from_secs(5);

trait ChildHandle: std::fmt::Debug {
    fn is_live(&mut self) -> io::Result<bool>;
    fn force_stop(&mut self) -> io::Result<()>;
}

impl ChildHandle for Child {
    fn is_live(&mut self) -> io::Result<bool> {
        Ok(self.try_wait()?.is_none())
    }

    fn force_stop(&mut self) -> io::Result<()> {
        self.kill()?;
        let _ = self.wait()?;
        Ok(())
    }
}

trait HostProcessSpawner {
    fn spawn_host(&mut self, host: &str, port: u16) -> io::Result<Box<dyn ChildHandle>>;
}

trait HostReadiness {
    fn is_healthy(&mut self) -> bool;
}

struct UreqReadiness {
    client: UreqHostClient,
}

impl UreqReadiness {
    fn new(base_url: &str) -> Self {
        Self {
            client: UreqHostClient::new(base_url, Duration::from_millis(300)),
        }
    }
}

impl HostReadiness for UreqReadiness {
    fn is_healthy(&mut self) -> bool {
        self.client.health().is_ok()
    }
}

struct UvicornSpawner;

impl HostProcessSpawner for UvicornSpawner {
    fn spawn_host(&mut self, host: &str, port: u16) -> io::Result<Box<dyn ChildHandle>> {
        let python = std::env::var_os("ONR_PYTHON").unwrap_or_else(|| "python".into());
        let child = Command::new(python)
            .args([
                "-m",
                "uvicorn",
                "onr.runtime_host.app:create_app",
                "--factory",
                "--host",
                host,
                "--port",
                &port.to_string(),
            ])
            .spawn()?;
        Ok(Box::new(child))
    }
}

fn stop_bootstrapped_host(child: Option<&mut (dyn ChildHandle + '_)>) -> io::Result<()> {
    if let Some(child) = child
        && child.is_live()?
    {
        child.force_stop()?;
    }
    Ok(())
}

fn consume_clean_exit(
    action: Option<CleanExitAction>,
    child: Option<&mut (dyn ChildHandle + '_)>,
) -> io::Result<bool> {
    if action.is_none() {
        return Ok(false);
    }
    stop_bootstrapped_host(child)?;
    Ok(true)
}

struct Options {
    host_addr: String,
    bootstrap_host: bool,
}

fn options() -> io::Result<Options> {
    let mut bootstrap_host = false;
    let mut host_arg = None;
    for argument in std::env::args().skip(1) {
        if argument == "--bootstrap-host" {
            bootstrap_host = true;
        } else if argument.starts_with('-') || host_arg.replace(argument).is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "usage: operator-console [--bootstrap-host] [http://127.0.0.1:PORT]",
            ));
        }
    }
    let host_addr = std::env::var("ONR_HOST")
        .ok()
        .or(host_arg)
        .unwrap_or_else(|| DEFAULT_HOST.to_string());
    Ok(Options {
        host_addr,
        bootstrap_host,
    })
}

fn loopback_bind(base_url: &str) -> io::Result<(&str, u16)> {
    let authority = base_url
        .strip_prefix("http://")
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "Host URL must use http://"))?;
    let (host, port) = authority.rsplit_once(':').ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "Host URL must include a port")
    })?;
    if !matches!(host, "127.0.0.1" | "localhost" | "[::1]") {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Host bootstrap is loopback-only",
        ));
    }
    let port = port
        .parse()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "Host URL port is invalid"))?;
    Ok((host, port))
}

fn bootstrap_host(
    base_url: &str,
    readiness: &mut dyn HostReadiness,
    spawner: &mut dyn HostProcessSpawner,
) -> io::Result<Option<Box<dyn ChildHandle>>> {
    if readiness.is_healthy() {
        return Ok(None);
    }
    let (host, port) = loopback_bind(base_url)?;
    let mut child = spawner.spawn_host(host, port)?;
    let deadline = Instant::now() + BOOTSTRAP_READY_TIMEOUT;
    loop {
        if readiness.is_healthy() {
            return Ok(Some(child));
        }
        if !child.is_live()? {
            return Err(io::Error::other(
                "bootstrapped Runtime Host exited before becoming ready",
            ));
        }
        if Instant::now() >= deadline {
            child.force_stop()?;
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "bootstrapped Runtime Host did not become ready within 5 seconds",
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn main() -> io::Result<()> {
    let options = options()?;
    let host_addr = options.host_addr;
    let mut spawner = UvicornSpawner;
    let mut readiness = UreqReadiness::new(&host_addr);
    let mut bootstrapped_host = if options.bootstrap_host {
        bootstrap_host(&host_addr, &mut readiness, &mut spawner)?
    } else {
        None
    };

    install_panic_hook();
    let mut guard = TerminalGuard::new()?;

    let (command_tx, command_rx) = std::sync::mpsc::channel();
    let (message_tx, message_rx) = std::sync::mpsc::channel();
    let client = UreqHostClient::new(&host_addr, REQUEST_TIMEOUT);
    let _worker = spawn_worker(client, command_rx, message_tx);

    let mut app = App::new(host_addr);
    let size = guard.terminal().size()?;
    app.handle_resize(size.width, size.height);

    let mut last_poll = Instant::now() - POLL_INTERVAL;
    while !app.should_quit() {
        app.check_deadlines();
        while let Ok(message) = message_rx.try_recv() {
            app.handle_host_message(message);
        }
        if consume_clean_exit(
            app.take_clean_exit_action(),
            bootstrapped_host.as_deref_mut(),
        )? {
            break;
        }
        for command in app.take_commands() {
            if command_tx.send(command).is_err() {
                break;
            }
        }
        guard.terminal().draw(|frame| ui::draw(frame, &app))?;
        if event::poll(TICK)? {
            match event::read()? {
                Event::Key(key) => app.handle_key(key),
                Event::Resize(width, height) => app.handle_resize(width, height),
                _ => {}
            }
        }
        if matches!(app.logical_state_name(), "Run") && last_poll.elapsed() >= POLL_INTERVAL {
            app.request_poll();
            last_poll = Instant::now();
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        ChildHandle, HostProcessSpawner, HostReadiness, bootstrap_host, consume_clean_exit,
        stop_bootstrapped_host,
    };
    use operator_console::app::CleanExitAction;
    use std::io;

    #[derive(Debug, Default)]
    struct FakeChild {
        live: bool,
        stops: usize,
    }

    impl ChildHandle for FakeChild {
        fn is_live(&mut self) -> io::Result<bool> {
            Ok(self.live)
        }

        fn force_stop(&mut self) -> io::Result<()> {
            self.stops += 1;
            self.live = false;
            Ok(())
        }
    }

    #[derive(Debug, Default)]
    struct FakeSpawner {
        spawns: usize,
    }

    impl HostProcessSpawner for FakeSpawner {
        fn spawn_host(&mut self, _host: &str, _port: u16) -> io::Result<Box<dyn ChildHandle>> {
            self.spawns += 1;
            Ok(Box::new(FakeChild {
                live: true,
                stops: 0,
            }))
        }
    }

    struct FakeReadiness {
        healthy: bool,
        checks: usize,
    }

    impl HostReadiness for FakeReadiness {
        fn is_healthy(&mut self) -> bool {
            self.checks += 1;
            self.healthy
        }
    }

    #[test]
    fn clean_exit_stops_only_retained_live_bootstrapped_child() {
        stop_bootstrapped_host(None).unwrap();
        let mut exited = FakeChild::default();
        stop_bootstrapped_host(Some(&mut exited)).unwrap();
        assert_eq!(exited.stops, 0);
        let mut live = FakeChild {
            live: true,
            stops: 0,
        };
        stop_bootstrapped_host(Some(&mut live)).unwrap();
        assert_eq!(live.stops, 1);
    }

    #[test]
    fn process_spawner_is_an_explicit_bootstrap_boundary() {
        let mut spawner = FakeSpawner::default();
        let mut child = spawner.spawn_host("127.0.0.1", 8787).unwrap();
        assert_eq!(spawner.spawns, 1);
        stop_bootstrapped_host(Some(child.as_mut())).unwrap();
        assert!(!child.is_live().unwrap());
    }

    #[test]
    fn healthy_existing_host_is_never_owned_or_stopped_on_clean_q_exit() {
        let mut readiness = FakeReadiness {
            healthy: true,
            checks: 0,
        };
        let mut spawner = FakeSpawner::default();
        let owned = bootstrap_host("http://127.0.0.1:8787", &mut readiness, &mut spawner).unwrap();
        assert!(owned.is_none());
        assert_eq!(readiness.checks, 1);
        assert_eq!(spawner.spawns, 0);

        let independent = FakeChild {
            live: true,
            stops: 0,
        };
        assert!(consume_clean_exit(Some(CleanExitAction::Cancelled), None).unwrap());
        assert!(independent.live);
        assert_eq!(independent.stops, 0);
    }
}
