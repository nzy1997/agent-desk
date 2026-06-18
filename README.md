# Agent Desk

Agent Desk is a local desktop manager for issue-to-Codex worker loops. The manager stays open, watches GitHub issues, starts isolated Codex workers, records transcripts, and shows current state in a local dashboard.

This MVP uses only the Python standard library plus local command-line tools:

- `gh` for GitHub issues and pull requests
- `git` for worktrees and branches
- `codex exec` for non-interactive worker runs
- SQLite for local state

## MVP Scope

- Scan configured repositories for `agent:ready` issues.
- Start up to `max_concurrent_runs` workers at once.
- Support multiple configured repositories with round-robin scheduling.
- Create a Git worktree and branch for each run.
- Run `codex exec` non-interactively with the fixed Superpowers-to-PR protocol.
- Save `prompt.md`, `stdout.jsonl`, `stderr.log`, `result.json`, and command logs per run.
- Serve a local dashboard at `http://127.0.0.1:8765`.
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

## Multiple Repositories And Concurrency

Add one `[[repos]]` block per repository:

```toml
[agent_desk]
max_concurrent_runs = 3

[[repos]]
name = "OWNER/FIRST"
local_path = "/absolute/path/to/first"
base_branch = "main"
test_command = "python -m unittest"

[[repos]]
name = "OWNER/SECOND"
local_path = "/absolute/path/to/second"
base_branch = "main"
test_command = "julia --project=. -e 'using Pkg; Pkg.test()'"
```

The concurrency limit is global. If it is set to `3`, Agent Desk can run three Codex CLI workers at the same time across all repositories. Scheduling is round-robin across repositories so one busy repository does not monopolize every slot.

Start low. Each active issue means one `codex exec` process plus whatever tests that worker runs.

## Safety Defaults

The generated config sets:

```toml
mutate_github = false
push_pr = false
```

With those defaults, Agent Desk reads GitHub issues and runs local workers, but it does not change labels or open PRs. Flip them only after the local loop behaves the way you want.

`push_pr` controls the worker finishing choice:

- `push_pr = true`: choose `Push and create a Pull Request`.
- `push_pr = false`: choose `Keep the branch as-is`.

See `docs/codex-cli-protocol.md` for the full fixed Codex CLI interaction policy.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
