# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Desk is a local desktop manager for issue-to-Codex worker loops. The manager
process stays open, watches GitHub issues, starts isolated `codex exec` workers in
git worktrees, records transcripts to disk, and shows state in a local dashboard at
`http://127.0.0.1:8765`.

**Zero runtime dependencies by design**: only the Python standard library plus the
`gh`, `git`, and `codex` command-line tools. Do not add Python dependencies —
`pyproject.toml` keeps `dependencies = []` deliberately. Python ≥ 3.11 (uses
`tomllib`, `datetime.UTC`).

## Commands

```bash
make init        # write config/repos.toml from the committed example (no-op if it exists)
make serve       # start dashboard + scheduler (auto-increments port if 8765 is busy)
make test        # python3 -m unittest discover -s tests -v
make help        # list targets

# Single test / module / case:
python3 -m unittest tests.test_scheduler -v
python3 -m unittest tests.test_scheduler.SchedulerTest.test_run_next -v

# Coverage (no pytest — stdlib unittest only):
python3 -m coverage run -m unittest discover -s tests && python3 -m coverage report -m

# Manual single-issue run without the dashboard:
python3 -m agent_desk run-next --config config/repos.toml
```

CLI entry points (`agent_desk/cli.py`): `init-config`, `add-repo` (`--path` or
`--clone OWNER/REPO`), `serve`, `run-next`, `open-pr --run-id N`.

Tests use `unittest` (not pytest) so the project stays dependency-free; this overrides
the global uv/pytest preference. Lint/format with `ruff check --fix . && ruff format .`
(line length 100).

`config/repos.toml` is machine-specific and git-ignored. The committed template is
`config/repos.example.toml` and `example_config()` in `config.py` — keep the two in sync
when adding config keys.

## Architecture

The pipeline is **issue → ready → running → pr_open → done**, driven entirely by where a
JSON file lives on disk. Layers (each its own module, wired together in `cli.py`):

- **`store.py` — filesystem state machine.** Each issue/run is one JSON file at
  `<data_dir>/state/<owner>__<repo>/<state>/<id>.json`. **The folder name *is* the
  state** — a transition rewrites the file into the new folder (atomic temp-write +
  `os.replace`) and unlinks the old copy, under a process-wide `RLock`. There is no
  database; ignore any "SQLite" mentions left in `README.md` — the store is fully
  filesystem-backed. States: `available` (synced issue, not yet a run) → `ready`/
  `queued` → `running` → `pr_open` → `done`/`failed`/`blocked`/`needs_review`.
  `available` is excluded from `list_runs()` and the active-run views.

- **`scheduler.py` — orchestration & lifecycle.** Owns per-workspace `SchedulerSettings`
  (auto-start, max-concurrent, human-review, single-closeout), the background poll loop
  (`serve_forever` → `poll_once` → `auto_start_ready_runs` + `monitor_prs`), and starts
  workers in daemon threads. `sync_repo_issues` is the **only** GitHub read for intake;
  `mark_issue_ready` is a pure on-disk move (`available/ → ready/`) with no GitHub call.
  `monitor_prs` polls open PRs, records `success`/`pending`/`failure`/`no_ci`/`unknown`,
  drives auto-CI-fix (up to `MAX_CI_FIX_ATTEMPTS=3`), optionally gates automatic
  closeout through an independent AI review worker, and auto-closes out when
  human review is disabled.

- **`worker.py` — one issue → one Codex run.** Creates the worktree
  (`git worktree add -b <branch> origin/<base>`), renders the prompt, runs `codex exec
  --json --output-schema schemas/worker-result.schema.json`, and parses the structured
  result (`status`/`summary`/`tests`/`questions`/`risks`/`pr_url`/`decision_log`).
  `CommandRunner` streams stdout/stderr to files with both total and **idle** timeouts;
  `FakeCommandRunner` is the test seam. Per-run artifacts land in
  `<data_dir>/runs/issue-N/run-M/` (`prompt.md`, `stdout.jsonl`, `stderr.log`,
  `result.json`, `error.log`, `codex-resume.txt`, git logs).

- **`continuation.py` — resuming an existing Codex thread.** Captures the Codex
  `thread_id` from `stdout.jsonl` and resumes the same thread for `open_pull_request`,
  `request_changes`, `approve_finish`, `finish_after_ci_success`, and `fix_ci`. The
  closeout sandbox is `repo.closeout_sandbox` (`workspace-write` by default).

- **`ai_review.py` - independent PR review worker.** Runs a fresh `codex exec`
  reviewer prompt against the PR worktree, never resumes the implementation
  thread, and records `ai_review_*` fields on the run. Approved reviews let the
  scheduler continue to automatic closeout; requested changes are sent back to
  the original thread through `request_changes`.

- **`prompt.py`** renders the fixed worker prompt (Superpowers-to-PR protocol, Standing
  Answer Policy). Branches on `repo.push_pr` for the "create a PR" vs "keep branch" path.

- **`github_client.py`** wraps `gh` for issue reads and PR check status.

- **`dashboard.py`** is a stdlib `http.server` app. HTML/JS live in `agent_desk/static/`
  (`dashboard.*`, `viewer.*` for the auto-refreshing `.jsonl` log viewer) and are loaded
  by `_load_page`. JSON API under `/api/*`; per-run files served via `/api/run/<id>/file`
  and `/api/run/<id>/view`.

## Safety model (important)

One per-repo flag gates PR publishing and defaults to **false** in generated config:

- `push_pr = false` → worker keeps the branch local; `true` → worker (or the manager as
  a fallback when Codex can't) pushes and opens a PR. A run only becomes `pr_open` once a
  PR URL exists.

Agent Desk does not mutate GitHub issue labels; desk state is folder-driven.

`closeout_sandbox` controls only the `Approve & finish` resume. Workers always start
manually (`auto_start_ready = false`) unless a workspace opts in; `single_closeout_per_workspace`
limits one merge/cleanup flow per checkout at a time. Each active issue is a real
`codex exec` process plus its tests — keep concurrency low.

When changing the worker/closeout contract, also read `docs/codex-cli-protocol.md`
(fixed Codex CLI interaction policy) and `docs/superpowers/specs/` (run-store and MVP
design specs).
