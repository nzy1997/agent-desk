# Provided Dependency Issue Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an atomic provided-dependency issue intake path and a global skill template that uses it.

**Architecture:** Extend the existing `include-issues` flow with a third dependency mode, `provided`, that converts caller-supplied edges into `Dependency` objects and reuses the analyze-mode state transition logic. Ship a repository template skill and update onboarding to install a machine-local global copy.

**Tech Stack:** Python 3.11+ standard library only; stdlib `unittest`; no runtime dependencies.

## Global Constraints

- Do not add Python dependencies.
- Do not mutate GitHub labels for issue intake.
- Do not start workers from the global skill.
- Keep dependency-blocked issues out of `ready` at all times.

---

### Task 1: Scheduler Provided Mode

**Files:**
- Modify: `agent_desk/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `Scheduler.mark_issues_ready(repo_name, issue_numbers, dependency_mode="provided", provided_dependencies=[...])`
- Consumes: existing `_mark_issue_blocked`, `_mark_issue_ready_direct`, and `_unsatisfied_dependencies`

- [ ] Write failing tests for provided dependencies placing blocked issues in `waiting_dependencies`.
- [ ] Write failing tests for satisfied provided dependencies placing issues in `ready`.
- [ ] Implement `provided_dependencies` parsing and state decisions.
- [ ] Run `python3 -m unittest tests.test_scheduler -v`.

### Task 2: Dashboard API

**Files:**
- Modify: `agent_desk/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `dependencies` payload items with `issue`, `dependency_repo`, `dependency`, and optional `evidence`
- Produces: calls to scheduler with `dependency_mode="provided"`

- [ ] Write a failing API test for `dependency_mode: "provided"`.
- [ ] Write a failing API test that rejects malformed provided dependency edges.
- [ ] Implement payload parsing and validation.
- [ ] Run `python3 -m unittest tests.test_dashboard -v`.

### Task 3: Global Skill Template And Onboard Guidance

**Files:**
- Create: `.claude/skills/agent-desk-add-issues/SKILL.md`
- Modify: `.claude/skills/onboard/SKILL.md`
- Test: inspect skill text for installed path placeholders and command accuracy

**Interfaces:**
- Produces: global skill instructions for existing issue intake via dashboard API
- Consumes: installed constants `AGENT_DESK_ROOT` and `DEFAULT_AGENT_DESK_URL`

- [ ] Add a concise global skill template with exact dashboard API calls.
- [ ] Update onboarding to copy/install the skill globally and replace local constants.
- [ ] Verify no placeholder values remain in the checked-in template except explicit replacement tokens.

### Task 4: Verification

**Files:**
- Test: all modified tests

- [ ] Run `make test`.
- [ ] Review `git diff`.
