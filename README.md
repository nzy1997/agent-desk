# Agent Desk

Agent Desk is a local desktop manager for issue-to-Codex worker loops. The manager stays open, watches GitHub issues, starts isolated Codex workers, records transcripts, and shows current state in a local dashboard.

This MVP uses only the Python standard library plus local command-line tools:

- `gh` for GitHub issues and pull requests
- `git` for worktrees and branches
- `codex exec` for non-interactive worker runs
- SQLite for local state

## MVP Scope

- Scan configured repositories for `agent:ready` issues.
- Queue configured repositories' `agent:ready` issues without starting workers automatically.
- Start workers only after a human clicks `Run`.
- Support multiple configured repositories in one manual queue with workspace-specific run settings.
- Create a Git worktree and branch for each run.
- Run `codex exec` non-interactively with the fixed Superpowers-to-PR protocol.
- Save `prompt.md`, `stdout.jsonl`, `stderr.log`, `result.json`, `codex-resume.txt`, and command logs per run.
- Serve a local dashboard at `http://127.0.0.1:8765` with per-run log links, Codex resume commands, PR feedback, and closeout controls.
- Keep GitHub mutation and PR creation disabled until configured.

## Quick Start

```bash
python3 -m agent_desk init-config
```

Edit `config/repos.toml` so the target repository and local clone path are correct.

Start the dashboard:

```bash
python3 -m agent_desk serve --config config/repos.toml
```

Run one issue manually:

```bash
python3 -m agent_desk run-next --config config/repos.toml
```

The scheduler polls GitHub and queues ready issues as local `ready` runs. It
does not start Codex workers by itself. Use the dashboard `Run` button, or
`run-next`, to start one queued issue.

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
mutate_github = false
push_pr = false
closeout_sandbox = "workspace-write"
```

With those defaults, Agent Desk reads GitHub issues and runs local workers, but it does not change labels or open PRs. Flip them only after the local loop behaves the way you want.

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
.agent-desk/runs/issue-ISSUE_NUMBER/run-ATTEMPT/
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
  or updates the completed issue, and promotes newly unblocked `agent:blocked`
  issues to `agent:ready`.

Agent Desk records the continuation logs on the same run. It does not decide
which follow-up issues are ready; that judgment stays with the resumed Codex
thread. Unlabeled issues and issues without `agent:blocked` are ignored during
closeout so ordinary discussion threads or non-agent work are not picked up.

The dashboard defaults to `One closeout per workspace`, so only one PR in a
repository checkout can be in the merge/cleanup closeout flow at a time. A
second manual or automatic closeout stays `pr_open` and records a warning event
until the active closeout finishes. You can turn the runtime setting off from
the dashboard if a repository can safely close out multiple PRs concurrently.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
