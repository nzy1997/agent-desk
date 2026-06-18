# Agent Desk MVP Design

## Goal

Build a local desktop manager that watches GitHub issues, starts non-interactive Codex workers, stores transcripts, and exposes current state in a local dashboard.

## Architecture

Agent Desk is a small local daemon with a browser dashboard. The daemon owns scheduling, SQLite state, GitHub issue discovery, Git worktree creation, and worker process execution. Codex remains a short-lived worker process: it receives a complete prompt, either completes the issue or returns a blocked/failed structured result, and exits.

## Components

- `agent_desk.config`: parse `config/repos.toml` with safe defaults.
- `agent_desk.store`: maintain runs, events, and dashboard state in SQLite.
- `agent_desk.scheduler`: claim one ready issue at a time and start worker threads.
- `agent_desk.worker`: create worktrees, run `codex exec`, capture transcripts, optionally push/open PRs.
- `agent_desk.dashboard`: serve a compact real-time operations dashboard with polling.
- `agent_desk.cli`: provide `init-config`, `serve`, and `run-next`.

## Data Flow

1. The scheduler lists open issues with the configured ready label.
2. It creates a local run record and, if configured, updates GitHub labels.
3. The worker creates a Git worktree and renders a complete prompt.
4. `codex exec` runs with `--json`, `--sandbox workspace-write`, and `--ask-for-approval never`.
5. The worker writes transcript files and stores a structured final status.
6. The dashboard polls SQLite state every two seconds.

## Error Handling

Command failures produce a failed run, an error event, and an `error.log` in the run directory. Ambiguous task state is represented as `blocked` with concrete questions, not as an interactive prompt.

## Testing

The MVP uses Python `unittest` and fake command runners for behavior that should not touch GitHub, Git, or Codex during tests.
