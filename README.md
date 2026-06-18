# Agent Desk

Agent Desk is a local desktop manager for issue-to-Codex worker loops. The manager stays open, watches GitHub issues, starts isolated Codex workers, records transcripts, and shows current state in a local dashboard.

This MVP uses only the Python standard library plus local command-line tools:

- `gh` for GitHub issues and pull requests
- `git` for worktrees and branches
- `codex exec` for non-interactive worker runs
- SQLite for local state

## MVP Scope

- Scan configured repositories for `agent:ready` issues.
- Start one worker at a time by default.
- Create a Git worktree and branch for each run.
- Run `codex exec` non-interactively with a structured result contract.
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

## Safety Defaults

The generated config sets:

```toml
mutate_github = false
push_pr = false
```

With those defaults, Agent Desk reads GitHub issues and runs local workers, but it does not change labels or open PRs. Flip them only after the local loop behaves the way you want.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
