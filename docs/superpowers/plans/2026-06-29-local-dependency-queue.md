# Local Dependency Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local dependency graph so selected issues can either be analyzed by Codex CLI before entering the queue or explicitly bypass dependency checks and all become ready.

**Architecture:** Agent Desk stores dependency metadata on issue records and treats local filesystem state as the queue source of truth. A fixed dependency extractor prompt asks Codex CLI for structured JSON; Scheduler applies that graph, moves unblocked issues to `ready`, keeps blocked issues in `blocked`, and unlocks them when dependencies finish. Dashboard exposes two add actions for the same selection.

**Tech Stack:** Python 3.11+ stdlib only, existing `CommandRunner`, existing file-backed `Store`, existing dashboard HTML/JS.

## Global Constraints

- Runtime dependencies remain empty; use only Python standard library plus local `gh`, `git`, and `codex` tools.
- `config/repos.toml` remains machine-specific and ignored.
- `auto_start_ready` starts false on every Scheduler launch; runtime toggles can still enable auto-start for the session.
- GitHub labels are cosmetic; local filesystem state remains the queue source of truth.

---

### Task 1: Dependency Extraction Contract

**Files:**
- Create: `agent_desk/dependencies.py`
- Test: `tests/test_dependencies.py`

**Interfaces:**
- Produces: `Dependency`, `IssueDependencies`, `DependencyGraph`, `render_dependency_prompt(repo_name, issues)`, `parse_dependency_result(text)`.

- [ ] Write failing parser/prompt tests for the fixed JSON structure.
- [ ] Implement dataclasses plus strict-but-tolerant JSON parsing.
- [ ] Verify with `python3 -m unittest tests.test_dependencies -v`.

### Task 2: Scheduler Queue Modes

**Files:**
- Modify: `agent_desk/scheduler.py`
- Modify: `agent_desk/store.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `DependencyGraph`.
- Produces: `mark_issues_ready(repo_name, issue_numbers, dependency_mode="analyze")`.

- [ ] Write failing tests that `dependency_mode="direct"` moves all selected issues to `ready`.
- [ ] Write failing tests that `dependency_mode="analyze"` keeps dependent issues blocked and promotes roots to ready.
- [ ] Write failing tests that a completed dependency unlocks blocked local issues to ready.
- [ ] Implement metadata writes and unlock pass.
- [ ] Verify with `python3 -m unittest tests.test_scheduler -v`.

### Task 3: Codex CLI Extractor Runner

**Files:**
- Modify: `agent_desk/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `render_dependency_prompt`.
- Produces: injectable dependency extractor callable for tests.

- [ ] Write failing test proving analyze mode calls the extractor once with selected issue bodies.
- [ ] Implement default extractor using `codex exec --json --output-last-message`.
- [ ] On extractor failure or parse failure, keep selected issues blocked with `dependency_state="unknown"`.

### Task 4: Dashboard API and UI

**Files:**
- Modify: `agent_desk/dashboard.py`
- Modify: `agent_desk/static/dashboard.html`
- Modify: `agent_desk/static/dashboard.js`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `dependency_mode` in `/api/actions/include-issues`.
- Produces: two UI actions: analyze dependencies and add all directly.

- [ ] Write failing route tests for `dependency_mode="direct"` and default analyze mode.
- [ ] Add response payload with added/blocked/skipped summaries.
- [ ] Add two buttons in the Add Issues panel and pass the chosen mode.
- [ ] Display blocked-by metadata in issue list and run list.

### Task 5: Verification

**Files:**
- All touched files.

- [ ] Run `python3 -m unittest discover -s tests -v`.
- [ ] Run `git diff --cached --check`.
- [ ] Stage the completed implementation.
