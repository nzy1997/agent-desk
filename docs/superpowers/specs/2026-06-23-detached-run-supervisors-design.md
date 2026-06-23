# Detached Run Supervisors

## Context

Today every run executes inside the `agent-desk serve` process. The scheduler
starts work in a **daemon thread** (`Scheduler._start_daemon_thread`), and that
thread drives the whole pipeline synchronously: `git fetch` → `worktree add` →
`codex exec` → parse `result.json` → open PR → fix CI. Codex itself is a
non-detached `subprocess.Popen` child whose stdout/stderr are piped back into
in-process reader threads.

Consequence: stopping or restarting the server kills both the orchestration
thread and the Codex child. This actually happened — issue #31 (yao-rs) froze
mid-run when the server was restarted to ship a feature, leaving an orphaned run
directory with no `result.json`.

Goal: a run survives a server restart. Detaching only Codex is insufficient —
the orchestration (parse result, open PR, fix CI, state updates) must also live
outside the server.

## Approach

Run each job in its own **detached OS process** ("supervisor"), not a server
thread. The server becomes an observer that reads the filesystem store and
run directories, plus a reconciler that cleans up jobs whose supervisor died.

This fits the existing filesystem state machine (`Store`): each run owns its own
atomically-written JSON record, so multiple processes writing different runs is
already safe. The only shared cross-process file is `counters.json`, hardened
below.

### Dispatch seam

All four dispatch sites already funnel through `Scheduler._start_daemon_thread`,
which every test overrides via `NoopScheduler` to run synchronously. We keep that
seam and change only its production body, so **no existing test changes**.

A job is fully described by `(run_id, kind)`; everything else is reconstructable
from the run record + config:

| kind | target | reconstructed args |
|------|--------|--------------------|
| `issue` | `_run_worker_for_issue` | repo (by name), issue fields, branch — all in the record |
| `approve-finish` | `_run_approve_finish` | run_id only |
| `auto-finish` | `_run_auto_finish` | run_id only |
| `ci-fix` | `_run_ci_fix` | `attempt` from record; `pr_status` re-fetched from GitHub by `pr_url` |

### Components

1. **`Scheduler.run_job(run_id, kind)`** — public entry that reconstructs kwargs
   from the record and calls the matching existing `_run_*` method. Used by the
   detached child and by tests.

2. **`Scheduler._spawn_detached_job(run_id, kind)`** — spawns
   `python -m agent_desk run-job --config <path> --run-id N --kind <kind>` with
   `start_new_session=True`, `stdin=DEVNULL`, and stdout/stderr redirected to
   `runs/issue-<n>/run-<attempt>/supervisor.log`. Records the child PID in the
   run record (`supervisor_pid`). Run dir is derived deterministically from
   `issue_number`/`attempt` (same formula `run_issue` uses).

3. **Production `_start_daemon_thread`** — when `detach_jobs` is set, maps
   `target.__name__` → kind and calls `_spawn_detached_job`; otherwise falls back
   to the current daemon thread. Default `detach_jobs=False` preserves existing
   behavior; `cli serve` and the `run-job` child both set it `True`, so nested
   dispatch (e.g. closeout → ci-fix) also becomes its own supervised process.

4. **`run-job` CLI subcommand** — private; builds config + store +
   `Scheduler(config, store, config_path=..., detach_jobs=True)` and calls
   `run_job`, then exits. Failures are already caught inside each `_run_*`.

5. **`Scheduler.reconcile_orphans()`** — at the start of `serve_forever`, before
   any polling: for each run in state `running`, if `supervisor_pid` is missing
   or not alive, mark it `failed` with
   `last_error="Run orphaned: supervisor not running after server restart"` and
   add an `orphan` event. A live supervisor (server restarted while the job kept
   running) is left untouched. **Policy: mark failed** (chosen over auto-relaunch
   to avoid surprise token spend; the user relaunches manually).

6. **`Store._next` hardening** — wrap the counters.json read-modify-write in an
   `fcntl.flock` on a sibling lock file so concurrent supervisors don't collide
   on the shared `id`/`event` sequence.

### Liveness check

`_pid_alive(pid)`: `os.kill(pid, 0)` → `True` unless `ProcessLookupError`. Best
effort; PID reuse is not specially defended (acceptable at this scale).

## Data flow

```
serve (detach_jobs=True)
  └─ reconcile_orphans()           # fail dead orphans from previous run
  └─ poll loop
       └─ _start_ready_run → _start_daemon_thread → _spawn_detached_job
                                                       │ start_new_session
                                                       ▼
                                          run-job process (own session)
                                            run_job → _run_worker_for_issue
                                              → Worker.run_issue (codex exec)
                                              → writes store record + run dir
```

Server restart: the `run-job` process is in its own session, writing to the
store/run dir, unaffected. On next startup `reconcile_orphans` sees its
`supervisor_pid` alive and leaves it running.

## Error handling

- Each `_run_*` already wraps its body and writes `failed` state on exception.
- `_spawn_detached_job` failures (e.g. cannot fork) surface as a `failed` run
  with the error recorded.
- Orphan with no `supervisor_pid` (e.g. started by old code) → marked failed.

## Testing

- `run_job` reconstructs kwargs and calls the right `_run_*` (via a fake worker /
  continuation runner), for each kind including `ci-fix` re-fetch.
- `reconcile_orphans` fails a `running` run with a dead/missing pid and leaves a
  run with a live pid alone (inject a fake `_pid_alive`).
- `Store._next` stays correct and monotonic under the file lock.
- `run-job` CLI subcommand parses and dispatches (with a stubbed scheduler).
- Existing scheduler/CLI/store tests stay green unchanged.

## Out of scope

- Auto-relaunch / auto-resume of orphans.
- Cross-host supervision; PID-reuse defense beyond `os.kill(pid, 0)`.
- Replacing daemon threads in the default (`detach_jobs=False`) path.
