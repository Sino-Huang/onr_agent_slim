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

## Panic: chained hook

`terminal::install_panic_hook()` runs before the guard is created. It captures
the previous (default) panic hook and installs one that first calls the same
idempotent `restore_terminal()`, then delegates to the previous hook. Order
matters: the default hook prints the panic message to stderr, and that output
must land on the restored primary screen, not inside the alternate screen.

The two mechanisms overlap deliberately: a panic while the guard is alive
restores via the hook (message visible), then unwinding drops the guard
(restore is a cheap no-op). A panic before the guard exists still restores.

## Boundaries and known limits

- The host worker thread is a channel consumer with bounded per-request
  timeouts (`REQUEST_TIMEOUT`, 5 s); it exits when the command channel closes
  on shutdown, so it never wedges process exit or touches the terminal.
- `SIGKILL`/`SIGTERM` are out of scope for this slice: there is no signal
  handling beyond crossterm's Ctrl+C key event, which maps to a clean quit
  through the normal loop.
- The console never writes host-side state on exit; run records remain the
  Runtime Host's durable concern.
