//! Operator Console entry point: optional Runtime Host bootstrap, terminal
//! lifecycle, the event loop, and the host workers. HTTP stays outside
//! drawing; see `docs/design/operator-console/terminal-lifecycle.md` for the
//! cleanup and panic restoration design.

use std::fs::File;
use std::io::{self, BufWriter, Write};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use crossterm::event::{self, Event};
use operator_console::app::world::ImageProtocol;
use operator_console::app::{App, CleanExitAction};
use operator_console::host::{HostClient, UreqHostClient, Workers};
use operator_console::terminal::{TerminalGuard, install_panic_hook};
use operator_console::ui;

/// Default loopback Runtime Host address.
const DEFAULT_HOST: &str = "http://127.0.0.1:8787";
/// Event poll tick; drawing resumes at least this often.
const TICK: Duration = Duration::from_millis(50);
/// Mission Run polling cadence in the Run state.
const POLL_INTERVAL: Duration = Duration::from_millis(400);
/// Bound on any single host request so a worker never wedges.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);
/// Default wait for a bootstrapped Host: importing it alone takes ~4 s warm.
const DEFAULT_READY_TIMEOUT: Duration = Duration::from_secs(60);
/// Host log lines echoed when the bootstrap fails.
const LOG_TAIL_LINES: usize = 20;
const SPINNER: [char; 10] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
const USAGE: &str = "usage: operator-console [--bootstrap-host] [--host-ready-timeout SECS] [--image-protocol auto|kitty|sixel|iterm2|halfblocks|off] [http://127.0.0.1:PORT]";

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

/// Repository checkout the console was built from.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map_or_else(|| PathBuf::from("."), Path::to_path_buf)
}

/// Bootstrapped Host stdout+stderr: `var/runtime-host/host.log`.
fn host_log_path() -> PathBuf {
    repo_root().join("var/runtime-host/host.log")
}

struct UvicornSpawner {
    log: PathBuf,
}

impl HostProcessSpawner for UvicornSpawner {
    fn spawn_host(&mut self, host: &str, port: u16) -> io::Result<Box<dyn ChildHandle>> {
        if let Some(parent) = self.log.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let stdout = File::create(&self.log)?;
        let stderr = stdout.try_clone()?;
        let python = std::env::var_os("ONR_PYTHON").unwrap_or_else(|| "python".into());
        let mut command = Command::new(python);
        command
            .current_dir(repo_root())
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
            .stdin(Stdio::null())
            .stdout(stdout)
            .stderr(stderr);
        // A console's PTY hangup must not kill its recoverable Runtime Host.
        #[cfg(unix)]
        command.process_group(0);
        let child = command.spawn()?;
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

#[derive(Debug)]
struct BootstrappedHostGuard {
    child: Option<Box<dyn ChildHandle>>,
}

impl BootstrappedHostGuard {
    fn new(child: Option<Box<dyn ChildHandle>>) -> Self {
        Self { child }
    }

    fn stop(&mut self) -> io::Result<()> {
        stop_bootstrapped_host(self.child.as_deref_mut())?;
        self.child = None;
        Ok(())
    }
}

impl Drop for BootstrappedHostGuard {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn consume_clean_exit(
    action: Option<CleanExitAction>,
    host: &mut BootstrappedHostGuard,
) -> io::Result<bool> {
    match action {
        None => Ok(false),
        Some(CleanExitAction::Detached) => {
            host.child = None;
            Ok(true)
        }
        Some(_) => {
            host.stop()?;
            Ok(true)
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
struct Options {
    host_addr: Option<String>,
    bootstrap_host: bool,
    ready_timeout: Duration,
    image_protocol: Option<ImageProtocol>,
}

fn parse_options(arguments: impl IntoIterator<Item = String>) -> io::Result<Options> {
    let usage = || io::Error::new(io::ErrorKind::InvalidInput, USAGE);
    let mut options = Options {
        host_addr: None,
        bootstrap_host: false,
        ready_timeout: DEFAULT_READY_TIMEOUT,
        image_protocol: None,
    };
    let mut arguments = arguments.into_iter();
    while let Some(argument) = arguments.next() {
        if argument == "--bootstrap-host" {
            options.bootstrap_host = true;
        } else if let Some(value) = argument
            .strip_prefix("--host-ready-timeout=")
            .map(str::to_string)
            .or_else(|| {
                (argument == "--host-ready-timeout")
                    .then(|| arguments.next())
                    .flatten()
            })
        {
            let seconds: f64 = value.parse().map_err(|_| usage())?;
            if !seconds.is_finite() || seconds <= 0.0 {
                return Err(usage());
            }
            options.ready_timeout = Duration::from_secs_f64(seconds);
        } else if let Some(value) = argument
            .strip_prefix("--image-protocol=")
            .map(str::to_string)
            .or_else(|| {
                (argument == "--image-protocol")
                    .then(|| arguments.next())
                    .flatten()
            })
        {
            options.image_protocol = Some(
                value
                    .parse()
                    .map_err(|error: String| io::Error::new(io::ErrorKind::InvalidInput, error))?,
            );
        } else if argument.starts_with('-') || options.host_addr.replace(argument).is_some() {
            return Err(usage());
        }
    }
    Ok(options)
}

fn resolve_image_protocol(
    cli: Option<ImageProtocol>,
    environment: Option<&str>,
) -> io::Result<ImageProtocol> {
    match cli {
        Some(protocol) => Ok(protocol),
        None => environment
            .unwrap_or("auto")
            .parse()
            .map_err(|error: String| io::Error::new(io::ErrorKind::InvalidInput, error)),
    }
}

/// Optional local draw timing; no runtime telemetry is sent to the Host.
fn draw_log() -> io::Result<Option<BufWriter<File>>> {
    let Some(path) = std::env::var_os("ONR_CONSOLE_DRAW_LOG") else {
        return Ok(None);
    };
    let path = PathBuf::from(path);
    let path = if path.is_absolute() {
        path
    } else {
        repo_root().join(path)
    };
    if !path.starts_with(repo_root().join("var/tmp")) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "ONR_CONSOLE_DRAW_LOG must be under var/tmp",
        ));
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    Ok(Some(BufWriter::new(File::create(path)?)))
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

/// Start a Host if none answers, then wait up to `timeout` for health,
/// reporting progress through `on_wait(elapsed)`.
fn bootstrap_host(
    base_url: &str,
    timeout: Duration,
    readiness: &mut dyn HostReadiness,
    spawner: &mut dyn HostProcessSpawner,
    on_wait: &mut dyn FnMut(Duration),
) -> io::Result<Option<Box<dyn ChildHandle>>> {
    if readiness.is_healthy() {
        return Ok(None);
    }
    let (host, port) = loopback_bind(base_url)?;
    let mut child = spawner.spawn_host(host, port)?;
    let started = Instant::now();
    loop {
        if readiness.is_healthy() {
            return Ok(Some(child));
        }
        if !child.is_live()? {
            return Err(io::Error::other(
                "bootstrapped Runtime Host exited before becoming ready",
            ));
        }
        let elapsed = started.elapsed();
        if elapsed >= timeout {
            child.force_stop()?;
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!(
                    "bootstrapped Runtime Host did not become ready within {:.0} seconds (raise --host-ready-timeout)",
                    timeout.as_secs_f64()
                ),
            ));
        }
        on_wait(elapsed);
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// Last `n` lines of a text file (lossy UTF-8); empty if unreadable.
fn tail_lines(path: &Path, n: usize) -> Vec<String> {
    let Ok(bytes) = std::fs::read(path) else {
        return Vec::new();
    };
    let text = String::from_utf8_lossy(&bytes);
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(n)..]
        .iter()
        .map(|line| (*line).to_string())
        .collect()
}

fn bootstrap_with_progress(
    host_addr: &str,
    timeout: Duration,
) -> io::Result<Option<Box<dyn ChildHandle>>> {
    let log = host_log_path();
    let mut readiness = UreqReadiness::new(host_addr);
    let mut spawner = UvicornSpawner { log: log.clone() };
    let mut stderr = io::stderr();
    let mut frame = 0usize;
    let mut on_wait = |elapsed: Duration| {
        frame = (frame + 1) % SPINNER.len();
        let _ = write!(
            stderr,
            "\r{} Starting Runtime Host at {host_addr} … {:>4.1} s / {:.0} s (log: {})\x1b[K",
            SPINNER[frame],
            elapsed.as_secs_f64(),
            timeout.as_secs_f64(),
            log.display()
        );
        let _ = stderr.flush();
    };
    let result = bootstrap_host(
        host_addr,
        timeout,
        &mut readiness,
        &mut spawner,
        &mut on_wait,
    );
    let mut stderr = io::stderr();
    match &result {
        Ok(Some(_)) => {
            let _ = writeln!(stderr, "\r✔ Runtime Host ready at {host_addr}\x1b[K");
        }
        Ok(None) => {}
        Err(_) => {
            let _ = writeln!(stderr, "\r✖ Runtime Host bootstrap failed\x1b[K");
            let lines = tail_lines(&log, LOG_TAIL_LINES);
            if lines.is_empty() {
                let _ = writeln!(stderr, "(no output in {})", log.display());
            } else {
                let _ = writeln!(stderr, "Last {} lines of {}:", lines.len(), log.display());
                for line in lines {
                    let _ = writeln!(stderr, "  {line}");
                }
            }
        }
    }
    result
}

fn main() -> io::Result<()> {
    let options = parse_options(std::env::args().skip(1))?;
    let image_protocol = resolve_image_protocol(
        options.image_protocol,
        std::env::var("ONR_CONSOLE_IMAGE_PROTOCOL").ok().as_deref(),
    )?;
    let mut draw_log = draw_log()?;
    let host_addr = std::env::var("ONR_HOST")
        .ok()
        .or(options.host_addr)
        .unwrap_or_else(|| DEFAULT_HOST.to_string());
    let mut bootstrapped_host = BootstrappedHostGuard::new(if options.bootstrap_host {
        bootstrap_with_progress(&host_addr, options.ready_timeout)?
    } else {
        None
    });

    install_panic_hook();
    let mut guard = TerminalGuard::new()?;
    let mut workers = Workers::spawn(UreqHostClient::new(&host_addr, REQUEST_TIMEOUT));

    let mut app = App::new(host_addr);
    app.configure_images(image_protocol.picker());
    let size = guard.terminal().size()?;
    app.handle_resize(size.width, size.height);

    let mut last_poll = Instant::now() - POLL_INTERVAL;
    while !app.should_quit() {
        app.check_deadlines();
        while let Some(message) = workers.try_recv() {
            app.handle_host_message(message);
        }
        if let Some(result) = app.view.media.poll() {
            match result {
                Ok(()) => app.view.frame_error = None,
                Err(error) => app.view.frame_error = Some(error),
            }
        }
        app.request_visible_frame();
        if consume_clean_exit(app.take_clean_exit_action(), &mut bootstrapped_host)? {
            break;
        }
        workers.flush_backlog();
        for command in app.take_commands() {
            app.handle_dispatch(workers.dispatch(command));
        }
        let draw_started = Instant::now();
        guard.terminal().draw(|frame| ui::draw(frame, &mut app))?;
        if let Some(log) = draw_log.as_mut() {
            writeln!(log, "{}", draw_started.elapsed().as_micros())?;
        }
        if event::poll(TICK)? {
            match event::read()? {
                Event::Key(key) => app.handle_key(key),
                Event::Resize(width, height) => app.handle_resize(width, height),
                _ => {}
            }
        }
        if app.logical_state_name() == "Run" && last_poll.elapsed() >= POLL_INTERVAL {
            app.request_poll();
            last_poll = Instant::now();
        }
    }
    consume_clean_exit(app.take_clean_exit_action(), &mut bootstrapped_host)?;
    if let Some(log) = draw_log.as_mut() {
        log.flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        BootstrappedHostGuard, ChildHandle, DEFAULT_READY_TIMEOUT, HostProcessSpawner,
        HostReadiness, Options, bootstrap_host, consume_clean_exit, parse_options,
        stop_bootstrapped_host, tail_lines,
    };
    use operator_console::app::CleanExitAction;
    use std::cell::Cell;
    use std::io;
    use std::rc::Rc;
    use std::time::Duration;

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

    /// Healthy after `healthy_after` checks.
    struct FakeReadiness {
        healthy_after: Option<usize>,
        checks: usize,
    }

    impl HostReadiness for FakeReadiness {
        fn is_healthy(&mut self) -> bool {
            self.checks += 1;
            self.healthy_after.is_some_and(|after| self.checks > after)
        }
    }

    #[derive(Debug)]
    struct ObservedChild {
        live: bool,
        stops: Rc<Cell<usize>>,
    }

    impl ChildHandle for ObservedChild {
        fn is_live(&mut self) -> io::Result<bool> {
            Ok(self.live)
        }

        fn force_stop(&mut self) -> io::Result<()> {
            self.stops.set(self.stops.get() + 1);
            self.live = false;
            Ok(())
        }
    }

    fn args(values: &[&str]) -> io::Result<Options> {
        parse_options(values.iter().map(|value| (*value).to_string()))
    }

    #[test]
    fn options_default_to_a_sixty_second_ready_timeout_and_accept_overrides() {
        let defaults = args(&[]).unwrap();
        assert_eq!(defaults.ready_timeout, DEFAULT_READY_TIMEOUT);
        assert_eq!(DEFAULT_READY_TIMEOUT, Duration::from_secs(60));
        let parsed = args(&[
            "--bootstrap-host",
            "--host-ready-timeout",
            "90",
            "http://127.0.0.1:9000",
        ])
        .unwrap();
        assert_eq!(
            parsed,
            Options {
                host_addr: Some("http://127.0.0.1:9000".to_string()),
                bootstrap_host: true,
                ready_timeout: Duration::from_secs(90),
                image_protocol: None,
            }
        );
        assert_eq!(
            args(&["--host-ready-timeout=2.5"]).unwrap().ready_timeout,
            Duration::from_millis(2500)
        );
        for invalid in [
            &["--host-ready-timeout"][..],
            &["--host-ready-timeout", "0"],
            &["--host-ready-timeout", "soon"],
            &["--unknown"],
            &["http://127.0.0.1:1", "http://127.0.0.1:2"],
        ] {
            assert!(args(invalid).is_err(), "{invalid:?} must be rejected");
        }
    }

    #[test]
    fn image_protocol_cli_precedes_environment_and_rejects_unknown_values() {
        use operator_console::app::world::ImageProtocol;
        assert_eq!(
            super::resolve_image_protocol(None, None).unwrap(),
            ImageProtocol::Auto
        );
        assert_eq!(
            super::resolve_image_protocol(None, Some("off")).unwrap(),
            ImageProtocol::Off
        );
        for (value, protocol) in [
            ("auto", ImageProtocol::Auto),
            ("kitty", ImageProtocol::Kitty),
            ("sixel", ImageProtocol::Sixel),
            ("iterm2", ImageProtocol::Iterm2),
            ("halfblocks", ImageProtocol::Halfblocks),
            ("off", ImageProtocol::Off),
        ] {
            let cli = args(&["--image-protocol", value]).unwrap().image_protocol;
            assert_eq!(
                super::resolve_image_protocol(cli, Some("invalid")).unwrap(),
                protocol
            );
            assert_eq!(
                args(&[&format!("--image-protocol={value}")])
                    .unwrap()
                    .image_protocol,
                cli
            );
        }
        assert!(args(&["--image-protocol"]).is_err());
        assert!(args(&["--image-protocol=unknown"]).is_err());
        assert!(super::resolve_image_protocol(None, Some("unknown")).is_err());
    }

    #[test]
    fn bootstrap_waits_for_readiness_reporting_progress_and_stops_at_the_deadline() {
        let mut readiness = FakeReadiness {
            healthy_after: Some(3),
            checks: 0,
        };
        let mut spawner = FakeSpawner::default();
        let mut waits = 0;
        let owned = bootstrap_host(
            "http://127.0.0.1:8787",
            Duration::from_secs(60),
            &mut readiness,
            &mut spawner,
            &mut |_| waits += 1,
        )
        .unwrap();
        assert!(owned.is_some());
        assert_eq!(spawner.spawns, 1);
        assert_eq!(waits, 2);

        let mut never = FakeReadiness {
            healthy_after: None,
            checks: 0,
        };
        let error = bootstrap_host(
            "http://127.0.0.1:8787",
            Duration::ZERO,
            &mut never,
            &mut FakeSpawner::default(),
            &mut |_| {},
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert!(error.to_string().contains("--host-ready-timeout"));
    }

    #[test]
    fn host_log_tail_returns_the_last_lines_only() {
        let dir = super::repo_root()
            .join("var/tmp")
            .join(format!("host-log-tail-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("host.log");
        let text: String = (1..=25).map(|line| format!("line {line}\n")).collect();
        std::fs::write(&path, text).unwrap();
        let tail = tail_lines(&path, 20);
        assert_eq!(tail.len(), 20);
        assert_eq!(tail.first().map(String::as_str), Some("line 6"));
        assert_eq!(tail.last().map(String::as_str), Some("line 25"));
        assert!(tail_lines(&dir.join("missing.log"), 20).is_empty());
        std::fs::remove_dir_all(dir).unwrap();
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
    fn bootstrapped_host_guard_stops_owned_child_when_scope_exits() {
        let stops = Rc::new(Cell::new(0));
        {
            let _host = BootstrappedHostGuard::new(Some(Box::new(ObservedChild {
                live: true,
                stops: Rc::clone(&stops),
            })));
        }
        assert_eq!(stops.get(), 1);
    }

    #[test]
    fn healthy_existing_host_is_never_owned_or_stopped_on_clean_q_exit() {
        let mut readiness = FakeReadiness {
            healthy_after: Some(0),
            checks: 0,
        };
        let mut spawner = FakeSpawner::default();
        let owned = bootstrap_host(
            "http://127.0.0.1:8787",
            Duration::from_secs(60),
            &mut readiness,
            &mut spawner,
            &mut |_| {},
        )
        .unwrap();
        assert!(owned.is_none());
        assert_eq!(readiness.checks, 1);
        assert_eq!(spawner.spawns, 0);

        let mut owned = BootstrappedHostGuard::new(None);
        assert!(consume_clean_exit(Some(CleanExitAction::Cancelled), &mut owned).unwrap());
    }
}
