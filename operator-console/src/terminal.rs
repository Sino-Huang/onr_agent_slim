//! Terminal setup, restoration, and panic safety.
//!
//! Design: see `docs/design/operator-console/terminal-lifecycle.md`.
//!
//! Setup is staged: every step after raw mode unwinds the steps that already
//! completed, so a normal (non-panic) error during initialization never leaves
//! the terminal in raw mode or inside the alternate screen.

use std::io::{self, Stdout};

use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::{Backend, CrosstermBackend};

/// Restores the terminal on drop; pair with [`install_panic_hook`].
pub struct TerminalGuard {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TerminalGuard {
    /// Enter raw mode and the alternate screen, unwinding on any error.
    pub fn new() -> io::Result<Self> {
        let terminal = staged_setup(
            enable_raw_mode,
            disable_raw_mode,
            || execute!(io::stdout(), EnterAlternateScreen).map(|_| ()),
            || execute!(io::stdout(), LeaveAlternateScreen).map(|_| ()),
            || Terminal::new(CrosstermBackend::new(io::stdout())),
        )?;
        Ok(TerminalGuard { terminal })
    }

    /// Access the wrapped terminal.
    pub fn terminal(&mut self) -> &mut Terminal<CrosstermBackend<Stdout>> {
        &mut self.terminal
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        restore_terminal();
    }
}

/// Idempotent best-effort terminal restoration: raw mode off, then leave the
/// alternate screen. Errors are ignored so restoration never masks the
/// original failure.
pub fn restore_terminal() {
    let _ = disable_raw_mode();
    let _ = execute!(io::stdout(), LeaveAlternateScreen);
}

/// Chain terminal restoration ahead of the default panic hook so a panic
/// never strands the operator inside the alternate screen.
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore_terminal();
        previous(info);
    }));
}

/// Staged terminal setup with error unwinding, in the same restore order as
/// [`restore_terminal`] (raw mode off before leaving the alternate screen).
///
/// - raw-mode failure: nothing to unwind.
/// - alternate-screen failure: raw mode is disabled before returning.
/// - terminal-construction failure: raw mode is disabled and the alternate
///   screen is left before returning.
fn staged_setup<B: Backend>(
    enable_raw: impl FnOnce() -> io::Result<()>,
    disable_raw: impl FnOnce() -> io::Result<()>,
    enter_alternate: impl FnOnce() -> io::Result<()>,
    leave_alternate: impl FnOnce() -> io::Result<()>,
    make_terminal: impl FnOnce() -> io::Result<Terminal<B>>,
) -> io::Result<Terminal<B>> {
    enable_raw()?;
    if let Err(error) = enter_alternate() {
        let _ = disable_raw();
        return Err(error);
    }
    match make_terminal() {
        Ok(terminal) => Ok(terminal),
        Err(error) => {
            let _ = disable_raw();
            let _ = leave_alternate();
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::io::ErrorKind;

    use ratatui::backend::TestBackend;

    use super::*;

    fn failure() -> io::Error {
        io::Error::new(ErrorKind::NotConnected, "injected failure")
    }

    fn setup_with(
        fail_at: Option<&'static str>,
    ) -> (io::Result<Terminal<TestBackend>>, Vec<&'static str>) {
        let calls = RefCell::new(Vec::new());
        let record = |name: &'static str, calls: &RefCell<Vec<&'static str>>| {
            calls.borrow_mut().push(name);
            if fail_at == Some(name) {
                Err(failure())
            } else {
                Ok(())
            }
        };
        let result = staged_setup(
            || record("enable_raw", &calls),
            || record("disable_raw", &calls),
            || record("enter_alternate", &calls),
            || record("leave_alternate", &calls),
            || {
                if fail_at == Some("make_terminal") {
                    calls.borrow_mut().push("make_terminal");
                    Err(failure())
                } else {
                    Ok(Terminal::new(TestBackend::new(100, 30)).expect("test backend"))
                }
            },
        );
        (result, calls.into_inner())
    }

    #[test]
    fn raw_mode_failure_unwinds_nothing() {
        let (result, calls) = setup_with(Some("enable_raw"));
        assert!(result.is_err());
        assert_eq!(calls, ["enable_raw"]);
    }

    #[test]
    fn alternate_screen_failure_disables_raw_mode() {
        let (result, calls) = setup_with(Some("enter_alternate"));
        assert!(result.is_err());
        assert_eq!(calls, ["enable_raw", "enter_alternate", "disable_raw"]);
    }

    #[test]
    fn terminal_construction_failure_restores_raw_mode_and_alternate_screen() {
        let (result, calls) = setup_with(Some("make_terminal"));
        assert!(result.is_err());
        assert_eq!(
            calls,
            [
                "enable_raw",
                "enter_alternate",
                "make_terminal",
                "disable_raw",
                "leave_alternate"
            ]
        );
    }

    #[test]
    fn successful_setup_leaves_terminal_active_without_unwind() {
        let (result, calls) = setup_with(None);
        assert!(result.is_ok());
        assert_eq!(calls, ["enable_raw", "enter_alternate"]);
    }
}
