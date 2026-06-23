# Filesystem Run Store — Design

Date: 2026-06-23

## Goal

Replace the SQLite store with a filesystem state machine. Each issue/run is a
single JSON file; its **state is the folder it lives in**; a **state transition
is an atomic file move**. State is fully inspectable and greppable on disk. The
GitHub API is read only when the user clicks **Sync issues**; every other
read/operation happens on local disk.

## Why

GitHub's label-filtered issue listing uses a search index that lags seconds
behind a write, which produced inconsistent desk state. Driving state from disk
removes GitHub from the hot path: sync pulls a snapshot to disk, and listing,
body display, adding to the desk, and run lifecycle all operate on that snapshot.

## Architecture

A new `FileStore` replaces `Store` (SQLite). It keeps the **same public
interface** so the scheduler, worker, and dashboard change minimally — "state =
folder" is an implementation detail of `FileStore`.

Public interface preserved (signatures unchanged):
`create_run`, `next_attempt`, `get_run`, `find_open_run`, `list_runs`,
`update_run`, `add_event`, `dashboard_state`. New methods are added for the
issue-intake layer (see below).

### Directory layout

Under `data_dir` (default `.agent-desk/`):

```
state/<owner>__<repo>/
  available/        synced GitHub issues not yet on the desk (pre-run)
  ready/            on the desk, awaiting a human Run
  running/
  pr_open/
  needs_review/
  blocked/
  done/
  failed/
counter             monotonic integer id source (single global file)
runs/issue-<n>/run-<attempt>/   existing per-run transcripts (unchanged)
```

- State records live under `state/`, kept separate from the existing run
  transcript tree under `runs/` to avoid any path collision.
- `counter` is a single global file (ids are unique across all repos).
- `<owner>__<repo>` slugifies `OWNER/REPO` (slash → `__`).
- Folder names equal the record's `state` string, so mapping is trivial.
- One file per issue: `<issue_number>.json`. A retry moves the same file back
  to `ready/` and bumps its `attempt`.

### Record format (`<issue_number>.json`)

```json
{"id":7,"repo_name":"OWNER/REPO","issue_number":42,"issue_title":"...",
 "issue_body":"...","issue_url":"...","branch_name":"","state":"available",
 "stage":"","attempt":1,"run_dir":"","worktree_path":"","codex_thread_id":"",
 "pr_url":"","pr_ci_status":"","pr_ci_summary":"","pr_ci_checked_at":"",
 "ci_fix_attempts":0,"ci_fix_last_sha":"","last_error":"",
 "created_at":"...","updated_at":"...","started_at":"","ended_at":"",
 "events":[{"level":"info","event_type":"queued","message":"...",
            "payload":{},"created_at":"..."}]}
```

Same fields as the SQLite `runs` row, plus an embedded `events` array (replaces
the `events` table). `id` is a process-wide monotonic integer from the `counter`
file, so existing `/api/run/<id>/…` routes keep working.

### State as folder

- `state` inside the JSON always equals the folder name (kept in sync).
- `update_run(state=X)` writes the JSON to a temp file, then `os.rename`s it into
  the `X/` folder and removes the old-folder copy — atomic on one filesystem.
- `list_runs()` scans the relevant folders; `find_open_run` = a file for that
  issue exists in any non-terminal folder; `next_attempt` reads the issue's file
  (or 1 if absent).
- Terminal/"open" semantics unchanged: `find_open_run` ignores
  `done`/`failed`/`blocked` (same as today).

### Concurrency & atomicity

- `FileStore` holds a single `threading.Lock` around every mutation (the app is
  one process; scheduler + worker threads share it).
- Writes are temp-file + `os.replace`; moves are `os.rename`. No partial files.
- `id` allocation reads/increments the `counter` file under the lock.

## Issue intake (Sync) layer

New `FileStore` + scheduler methods, and dashboard endpoints:

- **Sync** (`scheduler.sync_repo_issues(repo)`): one GitHub call
  (`gh issue list … --json number,title,body,url,labels`, client-side, no
  `--label`). For each open issue with no existing record, write an
  `available/<n>.json`. Existing records are left in place. Returns the repo's
  records.
- **List** (`scheduler.list_repo_issues(repo)` → `GET /api/issues?repo=`): reads
  records from disk; each carries `on_desk` (true when not in `available/`) and
  its `body`. No GitHub.
- **Add to desk** (`POST /api/actions/include-issues`): for each issue, move
  `available/ → ready/`, assign `branch_name`, and best-effort write the
  `agent:ready` label to GitHub for visibility. Title/body come from disk.
- The dashboard issue picker shows a **Sync issues** button (replaces "Add
  issues"); clicking an issue expands its **full body** (from the record).

## Scheduler / worker changes

- `discover_ready` no longer queries GitHub by label. The `ready/` folder *is*
  the queue. `poll_once` keeps `auto_start_ready_runs` (starts `ready/` records
  when enabled) and `monitor_prs` (still queries GitHub for CI status of
  `pr_open/` records — unavoidable).
- `start_run`, `approve_finish`, PR/CI handling, and the worker are unchanged
  except that they persist through `FileStore` (same `update_run`/`add_event`
  calls), so files move between folders as states change.
- The `agent:ready` label is now cosmetic (desk state is folder-driven); it is
  still written on add-to-desk for GitHub visibility, gated as an explicit
  manual action.

## Dashboard UI (folded-in tweaks)

- Rename the middle panel heading **Current Runs → Tasks** (queued items are not
  "running").
- Issue rows: clicking the title toggles an inline **full body** panel.
- Project-row summary already reads by state ("4 ready").

## Testing

- `test_store.py` is rewritten against `FileStore` (same assertions: create,
  next_attempt, find_open_run, list_runs by state, update_run moves state,
  events recorded, dashboard_state shape). Uses a `tmp_path` data dir.
- New tests: `sync_repo_issues` writes `available/` files; `list_repo_issues`
  reports `on_desk` and `body`; add-to-desk moves `available → ready`.
- Scheduler/worker/dashboard tests keep their fakes; they exercise `FileStore`
  via the unchanged interface. Update the few that assumed SQLite internals.

## Migration

No data migration: `.agent-desk/` is git-ignored local state. On first run with
the new code, `FileStore` creates the folder tree; any existing SQLite file is
ignored (left untouched, optionally deleted by the user).

## Out of scope

- Multi-process / multi-host access (single local process only).
- Changing the Codex worker protocol, PR closeout flow, or run-directory
  transcript layout under `.agent-desk/runs/issue-*/run-*/`.
