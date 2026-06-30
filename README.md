# Agent Desk

Agent Desk is a local desktop manager for issue-to-Codex worker loops. The manager stays open, watches GitHub issues, starts isolated Codex workers, records transcripts, and shows current state in a local dashboard.

This MVP uses only the Python standard library plus local command-line tools:

- `gh` for GitHub issues and pull requests
- `git` for worktrees and branches
- `codex exec` for non-interactive worker runs
- JSON files on disk for local state: each issue/run is one file whose state is
  the folder it lives in (`state/<owner>__<repo>/<state>/<id>.json`), so the
  full pipeline is inspectable and greppable. See
  `docs/superpowers/specs/2026-06-23-filesystem-run-store-design.md`.

## MVP Scope

- Sync configured repositories' open issues into a local `available/` list.
- Queue selected local issues without starting workers automatically.
- Start workers only after a human clicks `Run`.
- Support multiple configured repositories in one manual queue with workspace-specific run settings.
- Create a Git worktree and branch for each run.
- Run `codex exec` non-interactively with the fixed Superpowers-to-PR protocol.
- Save `prompt.md`, `stdout.jsonl`, `stderr.log`, `result.json`, `codex-resume.txt`, and command logs per run.
- Serve a local dashboard at `http://127.0.0.1:8765` with per-run log links, Codex resume commands, PR feedback, and closeout controls.
- Keep GitHub mutation and PR creation disabled until configured.

## Quick Start

Generate a local config from the committed template
(`config/repos.example.toml`):

```bash
make init        # or: python3 -m agent_desk init-config
```

`config/repos.toml` is machine-specific and git-ignored. Edit it so the target
repository and local clone path are correct.

Start the dashboard:

```bash
make serve       # or: python3 -m agent_desk serve --config config/repos.toml
```

Run one issue manually:

```bash
python3 -m agent_desk run-next --config config/repos.toml
```

The `ready/` folder on disk is the queue. The scheduler does not start Codex
workers by itself; use the dashboard `Run` button, or `run-next`, to start one
ready task.

To put issues on the desk, select a project in the Tasks panel; the Add Issues
panel follows that selection. Click `Sync issues` to pull the repository's open
issues from GitHub onto local disk (the `available/` folder). The picker then
lists issues from disk; click an issue title to expand its full body. Tick the
ones you want and choose one of two add modes:

- `Analyze dependencies`: asks Codex CLI to extract an explicit dependency graph
  from the selected issue bodies. Issues whose dependencies are already done move
  to `ready/`; issues still blocked stay local in `blocked/` until dependencies
  finish.
- `Add all directly`: bypasses dependency analysis and moves every selected issue
  straight to `ready/`.

Issues already on the desk show an `on desk` badge with a disabled checkbox.
Use `Remove` to move a ready or dependency-waiting issue back to `available/`.
Adding and removing are local queue operations; Agent Desk does not mutate
GitHub issue labels.

## Adding Repositories

You can register a repository three ways:

- Dashboard clone: enter `OWNER/REPO` or a GitHub URL and click `Clone & add`.
  The repo is cloned with `gh repo clone` into `clone_root/OWNER/REPO` and
  registered automatically.
- Dashboard existing folder: click `Browse for local folder...` and pick a local
  folder from the built-in directory browser. Git repos are marked.
- CLI: `agent-desk add-repo --path /abs/path/to/clone` for an existing clone, or
  `agent-desk add-repo --clone OWNER/REPO` to clone then register.

Cloned repositories are stored under `clone_root` (default
`~/.agent-desk/repos`), configurable in the `[agent_desk]` section:

```toml
[agent_desk]
clone_root = "~/.agent-desk/repos"
```

## Multiple Repositories And Concurrency

Add one `[[repos]]` block per repository:

```toml
[agent_desk]

[[repos]]
name = "OWNER/FIRST"
local_path = "/absolute/path/to/first"
base_branch = "main"
test_command = "python -m unittest"
auto_start_ready = false
max_concurrent_runs = 1
requires_human_review = true

[[repos]]
name = "OWNER/SECOND"
local_path = "/absolute/path/to/second"
base_branch = "main"
test_command = "julia --project=. -e 'using Pkg; Pkg.test()'"
auto_start_ready = false
max_concurrent_runs = 1
requires_human_review = true
```

Run settings are workspace-specific. Each repository folder defaults to manual
start, one active worker, and human review before closeout:

```toml
auto_start_ready = false
max_concurrent_runs = 1
requires_human_review = true
single_closeout_per_workspace = true
```

Discovery can queue issues from every configured repository, but workers only
start when a human clicks `Run` or invokes `run-next` unless
`auto_start_ready` is enabled for that workspace. The dashboard Settings panel
edits the currently selected folder rather than a global scheduler pool.

Start low. Each active issue means one `codex exec` process plus whatever tests that worker runs.

## Safety Defaults

The generated config sets:

```toml
push_pr = false
closeout_sandbox = "workspace-write"
```

With those defaults, Agent Desk reads GitHub issues and runs local workers, but
it does not open PRs. Agent Desk does not mutate GitHub issue labels.

`push_pr` controls the worker finishing choice:

- `push_pr = true`: choose `Push and create a Pull Request`.
- `push_pr = false`: choose `Keep the branch as-is`.

When `push_pr = true`, Agent Desk also owns a fallback PR path. If the Codex
worker finishes the implementation but cannot create the PR from inside its own
environment, it should return `done` with an empty `pr_url`; the manager then
runs `git push` and `gh pr create` locally. Runs are marked `pr_open` only after
the manager receives a PR URL.

`closeout_sandbox` controls only the `Approve & finish` Codex resume. Keep the
default `workspace-write` for conservative local experiments. Set it to
`danger-full-access` for repositories where approved closeout should let Codex
sync the base checkout, merge, push, update issues, and remove worktrees.

See `docs/codex-cli-protocol.md` for the full fixed Codex CLI interaction policy.

## Inspecting Failures

Failed runs expose links in the dashboard for files such as `error.log`, `stderr.log`, `stdout.jsonl`, and `prompt.md`.

The files also live under the configured data directory:

```text
.agent-desk/runs/run-RUN_ID/
```

## Human Intervention

Agent Desk records the Codex CLI `thread_id` from `stdout.jsonl` and stores a
ready-to-copy resume command on each run. The same command is written to
`codex-resume.txt` when the thread id is available:

```bash
codex resume --include-non-interactive -C /path/to/worktree THREAD_ID
```

Use that command when a human wants to continue the worker conversation in an
interactive Codex CLI session. Agent Desk keeps the run worktree path in SQLite
and shows the command in the dashboard; old runs without `codex_thread_id` in
SQLite are backfilled from `stdout.jsonl` for display.

Do not remove a run worktree while it may still need human intervention. Cleanup
should happen after the related PR has been merged or closed and the issue has
been resolved.

## PR Review And Closeout

For a run in `pr_open`, the dashboard exposes two Codex-resume actions:

- `Request changes`: sends your review feedback to the original Codex thread
  with `codex exec resume THREAD_ID`, asking it to update and push the existing
  PR branch.
- `Approve & finish`: sends a generic closeout prompt to the original Codex
  thread. Codex checks PR status, refuses to merge if checks are pending or
  failing, merges when safe, syncs local state, cleans up the worktree, closes
  or updates the completed issue.

Agent Desk records the continuation logs on the same run. Closeout does not
inspect or modify follow-up issue labels. Local dependency metadata determines
when blocked desk issues unlock into `ready/`.

The dashboard defaults to `One closeout per workspace`, so only one PR in a
repository checkout can be in the merge/cleanup closeout flow at a time. A
second manual or automatic closeout stays `pr_open` and records a warning event
until the active closeout finishes. You can turn the runtime setting off from
the dashboard if a repository can safely close out multiple PRs concurrently.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
