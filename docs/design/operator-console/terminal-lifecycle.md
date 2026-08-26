# Terminal Lifecycle: Cleanup and Panic Restoration

The console takes over the terminal (raw mode + alternate screen) and must
always hand it back, on normal exit and on panic. Two independent mechanisms
guarantee this, and both are idempotent.

## Normal exit: `TerminalGuard`

`terminal::TerminalGuard` owns the `Terminal<CrosstermBackend<Stdout>>`. Its
constructor enables raw mode and enters the alternate screen; `Drop` calls
`restore_terminal()`, which disables raw mode and leaves the alternate screen.
Because the run loop owns the guard, every `Ok`/`Err` return path from `main`
drops it. Restoration errors are swallowed (`let _ =`): a half-broken terminal
must not mask the real exit status.

## Normal errors during initialization: staged setup with unwind

Initialization itself can fail partway, before any guard exists. Setup is
therefore staged (`terminal::staged_setup`) so a normal error after raw mode
still restores:

- raw-mode failure - nothing was taken over; the error propagates.
- alternate-screen failure - raw mode is disabled before the error returns.
- terminal-construction failure - raw mode is disabled and the alternate
  screen is left (same order as `restore_terminal`) before the error returns.

The steps are injectable closures, so each failure path is unit-tested without
a TTY; once the guard exists, its `Drop` covers all later errors.

## Panic: chained hook

`terminal::install_panic_hook()` runs before the guard is created. It captures
the previous (default) panic hook and installs one that first calls the same
idempotent `restore_terminal()`, then delegates to the previous hook. Order
matters: the default hook prints the panic message to stderr, and that output
must land on the restored primary screen, not inside the alternate screen.

The two mechanisms overlap deliberately: a panic while the guard is alive
restores via the hook (message visible), then unwinding drops the guard
(restore is a cheap no-op). A panic before the guard exists still restores.

## Bootstrapped Runtime Host ownership

When the console starts its own Runtime Host, a separate guard owns that child
process. Dropping the guard stops the child on normal quit, event-loop error,
or panic unwinding. A healthy Runtime Host that already existed at startup is
never adopted or stopped by the console.

The bootstrapped Host's standard streams are detached from the console
terminal. This prevents a Host that is still shutting down from inheriting a
deleted pseudo-terminal and later failing while starting a Run Worker.

## Boundaries and known limits

- The host worker thread is a channel consumer with bounded per-request
  timeouts (`REQUEST_TIMEOUT`, 5 s); it exits when the command channel closes
  on shutdown, so it never wedges process exit or touches the terminal.
- `SIGKILL`/`SIGTERM` are out of scope for this slice: there is no signal
  handling beyond crossterm's Ctrl+C key event, which maps to a clean quit
  through the normal loop.
- The console never writes host-side state on exit; run records remain the
  Runtime Host's durable concern.
