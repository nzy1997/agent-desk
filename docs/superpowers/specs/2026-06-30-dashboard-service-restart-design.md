# Dashboard Service Restart

## Context

Agent Desk runs the dashboard and scheduler through `agent-desk serve`. In that
mode, issue work is already dispatched to detached `agent-desk run-job`
supervisors, so restarting the dashboard process should not kill active Codex
workers. Today a user still has to return to the terminal and press `Ctrl-C`,
then rerun the serve command.

## Goal

Add a dashboard button that restarts the Agent Desk service itself: dashboard
plus scheduler. The button must not restart active issue workers, kill detached
supervisors, or retry failed work automatically.

## Recommended Approach

Add a `/api/actions/restart` POST action handled by the dashboard server. The
handler returns a successful JSON response first, then starts a short background
thread that asks the HTTP server to shut down and re-executes the current
process with `os.execv(sys.executable, [sys.executable, *sys.argv])`.

This preserves the original command-line flags such as `--config`, `--host`, and
`--port`. Because the process is replaced instead of spawning a second long-lived
server, the same terminal/session continues to own the service.

## Alternatives Considered

1. Exit only and ask the terminal command to be rerun.
   This is simple but still requires manual terminal interaction, which is the
   thing the button is meant to remove.

2. Spawn a fresh server process and exit the old one.
   This can work, but it is easier to leave behind confusing parent/child
   process state. Re-execing the current process keeps ownership clearer.

3. Add a full external supervisor/watchdog.
   This is more robust for production-style daemon management, but it is too
   much machinery for a local stdlib-only desktop helper.

## UI

Add a `Restart` button to the header next to Pause and Resume. On click, the
existing dashboard JavaScript action helper posts to `/api/actions/restart`.
After the response, the page may briefly lose connection while the process
re-execs; the browser's existing polling can reconnect once the server binds
again.

## Server Behavior

- The restart action is a server-level action; it works even when the dashboard
  was started with `--no-scheduler`.
- The HTTP response is sent before shutdown begins.
- When a scheduler is present, it is stopped before process replacement so the
  old scheduler loop does not continue doing work during shutdown.
- Detached `run-job` supervisors are not touched.
- On startup, the existing orphan reconciliation decides whether any `running`
  runs still have live supervisors.

## Error Handling

If the process cannot re-exec, write the exception to stderr and terminate the
old process with a non-zero exit. The dashboard cannot reliably report that
failure after it has already begun restarting, so the terminal log remains the
source of truth.

## Testing

Use stdlib `unittest`.

- Unit-test the restart endpoint by injecting a fake restart callback into
  `serve_dashboard`, POSTing `/api/actions/restart`, and asserting the response
  is successful and the callback is invoked.
- Assert the dashboard HTML contains the restart button and target route.
- Keep existing dashboard and scheduler tests passing.
