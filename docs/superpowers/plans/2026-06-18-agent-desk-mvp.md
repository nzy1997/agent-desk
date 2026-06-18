# Agent Desk MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local desktop manager for GitHub issue-to-Codex worker runs.

**Architecture:** A Python standard-library daemon owns GitHub polling, SQLite state, worker execution, and a compact local dashboard. Codex workers are non-interactive subprocesses whose prompts and outputs are saved per run.

**Tech Stack:** Python 3.11+, SQLite, `http.server`, `gh`, `git`, `codex exec`.

## Global Constraints

- No package dependencies in the MVP runtime.
- GitHub mutation and PR creation stay disabled in generated config.
- Workers must not wait for interactive user input.
- Every run must save prompt, stdout, stderr, and final result files.

---

### Task 1: Core State And Prompt

**Files:**
- Create: `agent_desk/config.py`
- Create: `agent_desk/store.py`
- Create: `agent_desk/prompt.py`
- Test: `tests/test_config.py`
- Test: `tests/test_store.py`
- Test: `tests/test_prompt.py`

**Interfaces:**
- Produces: `load_config(path) -> AgentDeskConfig`
- Produces: `Store.create_run(...) -> int`
- Produces: `Store.dashboard_state() -> dict`
- Produces: `render_worker_prompt(...) -> str`

- [x] Write failing tests for config, store, and prompt behavior.
- [x] Run `python3 -m unittest discover -s tests -v` and verify imports fail.
- [x] Implement config dataclasses and TOML parsing.
- [x] Implement SQLite migration, run creation, events, and dashboard aggregation.
- [x] Implement non-interactive worker prompt rendering.
- [x] Re-run the tests.

### Task 2: Worker Execution

**Files:**
- Create: `agent_desk/worker.py`
- Create: `schemas/worker-result.schema.json`
- Test: `tests/test_worker.py`

**Interfaces:**
- Produces: `Worker.run_issue(...) -> WorkerResult`
- Produces: `CommandRunner.run(...) -> CommandResult`
- Produces: `FakeCommandRunner`

- [x] Write a failing test for non-interactive `codex exec` invocation and transcript files.
- [x] Implement command runner and fake runner.
- [x] Implement worktree creation, prompt writing, Codex execution, and result parsing.
- [x] Re-run the tests.

### Task 3: Dashboard And Scheduler

**Files:**
- Create: `agent_desk/dashboard.py`
- Create: `agent_desk/github_client.py`
- Create: `agent_desk/scheduler.py`
- Create: `agent_desk/cli.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `build_state_payload(store, scheduler=None) -> dict`
- Produces: `Scheduler.run_next() -> RunNextResult`
- Produces: CLI commands `init-config`, `serve`, and `run-next`

- [x] Write a failing dashboard serialization test.
- [x] Implement GitHub issue listing and label helpers.
- [x] Implement scheduler controls and one-issue run start.
- [x] Implement dashboard HTML and JSON endpoints.
- [x] Implement CLI entry point.
- [x] Re-run the tests.

### Task 4: Packaging And Repository

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `docs/superpowers/specs/2026-06-18-agent-desk-mvp-design.md`
- Create: `docs/superpowers/plans/2026-06-18-agent-desk-mvp.md`

**Interfaces:**
- Produces: private GitHub repository `nzy1997/agent-desk`

- [x] Add package metadata and docs.
- [x] Run the complete test suite.
- [x] Initialize local git repository and commit.
- [x] Create private GitHub repository.
- [x] Push `main`.
