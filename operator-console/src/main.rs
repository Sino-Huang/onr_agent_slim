//! Operator Console entry point: terminal lifecycle, event loop, and the
//! host worker thread. HTTP polling stays outside drawing; see
//! `docs/design/operator-console/terminal-lifecycle.md` for the cleanup and
//! panic restoration design.

use std::io;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event};
use operator_console::app::App;
use operator_console::host::{UreqHostClient, spawn_worker};
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

fn main() -> io::Result<()> {
    let host_addr = std::env::var("ONR_HOST")
        .ok()
        .or_else(|| std::env::args().nth(1))
        .unwrap_or_else(|| DEFAULT_HOST.to_string());

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
        while let Ok(message) = message_rx.try_recv() {
            app.handle_host_message(message);
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
