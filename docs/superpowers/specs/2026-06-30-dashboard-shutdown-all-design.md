# Dashboard Shutdown All With Resume

## Context

Agent Desk is used as a local desktop control plane. The normal dashboard
restart path only replaces the `agent-desk serve` process and intentionally
leaves detached `agent-desk run-job` supervisors alive. That is correct for a
service restart, but it does not solve the "I am closing the computer now" case.

For that case, the user wants Agent Desk to show exactly which CLIs it is about
to stop, record enough recovery information, stop the dashboard and all active
Agent Desk child work, and make the next startup offer a working manual
`Resume` button for interrupted runs.

## Goals

- Add a dashboard-level `Shutdown all` action for controlled computer shutdown.
- Before shutdown, show the user the active runs and CLI process groups that
  will be stopped.
- Persist a shutdown trail in the global data directory and each affected run
  directory before sending signals.
- Introduce an `interrupted` run state for intentional user shutdowns.
- Add a working per-run `Resume` button for interrupted runs that have enough
  Codex thread metadata to continue.
- Stop active detached `run-job` supervisors and their process groups, then stop
  the dashboard service itself.

## Non-Goals

- Do not add automatic "resume all" in the first version.
- Do not treat active shutdown interruptions as ordinary worker failures.
- Do not delete worktrees, logs, branches, run directories, or PR metadata.
- Do not attempt to resume a run that has no captured Codex thread id.

## User Flow

The dashboard header gains a `Shutdown all` button next to `Pause`, `Resume`,
and `Restart`.

When clicked, the browser asks the server for a shutdown preview. The preview
lists each active run that would be interrupted:

- run id, repo, issue number, issue title, state, and stage
- supervisor PID and process group id
- best-effort CLI process list for that process group
- run directory and worktree path
- captured Codex thread id, if available
- whether dashboard `Resume` will be available after restart

The browser then shows a destructive confirmation dialog with this summary. If
the user cancels, nothing is changed. If the user confirms, the browser posts to
the shutdown endpoint. The response includes the shutdown id, manifest path, and
per-run resume availability. After the response is delivered, the dashboard
service exits. The browser can show that the shutdown was recorded and it is
safe to close the computer.

## Shutdown Data

Each shutdown gets an id based on UTC timestamp, for example
`2026-06-30T12-34-56Z`. The server writes:

- global JSON manifest:
  `config.data_dir / "shutdowns" / "<shutdown-id>.json"`
- per-run JSON manifest:
  `<run_dir> / "shutdown-<shutdown-id>.json"`
- per-run human-readable note:
  `<run_dir> / "shutdown-resume-<shutdown-id>.md"`

The manifest records:

- shutdown id and timestamp
- dashboard PID and config path
- affected run ids
- run metadata, worktree path, run dir, stage, and last known supervisor PID
- captured Codex thread id and formatted resume command
- process group id and process command snapshot
- signals sent and final kill results
- warnings for any process that could not be verified or stopped

The first manifest write happens before any signal is sent. A final update is
written after signal attempts finish and before the dashboard exits.

## Run State

Confirmed shutdown changes each affected active run to:

- `state = "interrupted"`
- `stage = "interrupted by shutdown"`
- `last_error = "Interrupted by user shutdown; resume from dashboard"`

The store also records an event:

- level: `warning`
- type: `shutdown-interrupted`
- message: `Run interrupted by dashboard shutdown`
- payload: shutdown id, manifest path, supervisor PID, process group id,
  resume availability, and any warnings

Startup orphan reconciliation must not convert `interrupted` runs to `failed`.
They are already intentionally stopped and should remain visible as recoverable
attention items.

## Capturing Resume Metadata

Before marking a run interrupted, the shutdown action refreshes resume metadata:

- Prefer the stored `codex_thread_id`.
- If missing, scan the run directory logs, especially `stdout.jsonl` and
  continuation stdout logs, for a `thread.started` event.
- Store the recovered thread id back on the run record.
- Compute the dashboard resume command from `codex_thread_id` and
  `worktree_path`.

A run is resume-available only when both `codex_thread_id` and `worktree_path`
are present. Runs without a thread id stay interrupted, but the UI explains that
dashboard resume is unavailable because no Codex thread was captured.

## Process Discovery And Signals

Detached jobs are started in their own session with `start_new_session=True`, so
the run-job PID is normally the process group leader. Shutdown uses the stored
`supervisor_pid` to discover the process group.

For safety, the process group is killable only when the supervisor process can
be verified as the expected `agent_desk run-job --run-id <id>` process. If the
command cannot be verified, shutdown records a warning and skips killing that
PID rather than risking an unrelated process.

For a verified group:

1. Record the process snapshot.
2. Send `SIGTERM` to the process group.
3. Wait a short bounded interval.
4. Send `SIGKILL` to any still-live group.
5. Record the final result.

After run groups are handled, the dashboard sends the HTTP response and then
shuts down the server process.

## Manual Resume

Interrupted runs with resume metadata show a `Resume` button in their run card.
Clicking it posts to a new run action, for example:

`POST /api/run/<id>/resume-interrupted`

The scheduler validates:

- scheduler is not paused
- run state is `interrupted`
- `codex_thread_id` exists or can be recovered from run logs
- `worktree_path` exists
- no active supervisor is already recorded for the run

If valid, the scheduler updates the run to:

- `state = "running"`
- `stage = "resume-interrupted queued"`
- `last_error = ""`

Then it spawns a detached `run-job` kind such as `resume-interrupted`.

The detached job runs `codex exec resume` in the original worktree with a
purpose-built prompt. The prompt tells Codex that the prior run was
intentionally interrupted by dashboard shutdown, points it at the original
prompt and logs in the run directory, asks it to inspect the current worktree,
and instructs it to continue the original issue to the same worker result JSON
contract used by normal runs.

The result handling follows the normal worker path:

- if Codex returns a PR URL, mark `pr_open`
- if Codex returns done without a PR and the repo requires PR creation, run the
  existing open-PR continuation
- if Codex returns blocked, mark `blocked`
- if Codex fails, mark `failed`

The button is only shown when resume is expected to work. If metadata is
missing, the card shows a short unavailable message and keeps the raw resume
logs visible.

## API Surface

Add these dashboard endpoints:

- `GET /api/actions/shutdown-preview`
  Returns the current shutdown preview without changing state.
- `POST /api/actions/shutdown-all`
  Records shutdown artifacts, marks active runs interrupted, stops verified
  process groups, returns a shutdown summary, then exits the dashboard service.
- `POST /api/run/<id>/resume-interrupted`
  Starts a detached resume job for a resume-available interrupted run.

Existing restart and pause/resume endpoints remain separate. Restart is still a
service restart and must not kill detached jobs.

## UI

The header should expose `Shutdown all` as a destructive action. It must not be
visually confused with `Restart`.

The run card for an interrupted run should show:

- interrupted state and stage
- shutdown manifest/log links when present
- resume command
- `Resume` button when available
- a clear unavailable reason when resume metadata is missing

The attention column includes `interrupted` runs.

## Error Handling

- If preview fails, no state changes are made.
- If manifest writing fails, shutdown is blocked and no signals are sent.
- If a supervisor PID cannot be verified, record a warning and do not kill it.
- If a process group partially survives after `SIGKILL`, keep the run
  `interrupted`, record the warning, and surface it in the event payload.
- If resume validation fails, leave the run `interrupted` and return a clear
  message.
- If the resume job cannot spawn, mark the run `interrupted` with
  `last_error` explaining the spawn failure instead of converting it to
  `failed`.

## Testing

Use stdlib `unittest`.

- Unit-test shutdown preview from store records with running supervisors.
- Unit-test manifest content and per-run shutdown note generation.
- Unit-test that shutdown marks runs `interrupted`, records events, and does not
  touch non-running runs.
- Unit-test process-group signaling with mocked process inspection and signal
  calls.
- Unit-test that unsafe or unverifiable PIDs are skipped with warnings.
- Unit-test startup orphan reconciliation leaves `interrupted` runs unchanged.
- Unit-test dashboard state exposes resume availability and unavailable reasons.
- Unit-test interrupted run cards render `Resume` only when metadata exists.
- Unit-test `resume-interrupted` validation and detached job dispatch.
- Unit-test detached `resume-interrupted` job reconstructs the resume prompt,
  uses `codex exec resume`, and follows normal worker result handling.
- Keep the full test suite passing with
  `python3 -m unittest discover -s tests -v`.
