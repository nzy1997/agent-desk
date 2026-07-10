# Provided Dependency Issue Intake Design

## Goal

Let an agent working in any managed repository add existing GitHub issues to Agent Desk with a dependency graph it already knows, without briefly exposing dependency-blocked issues as `ready`.

## Scope

This change covers existing GitHub issues only. It does not create issues, start workers, open pull requests, or mutate GitHub labels. It adds a safe API path for queueing issues and a global skill template that calls that path from outside the Agent Desk repository.

## API

`POST /api/actions/include-issues` accepts a new `dependency_mode` value:

```json
{
  "repo": "OWNER/REPO",
  "issues": [12, 15, 18],
  "dependency_mode": "provided",
  "dependencies": [
    {
      "issue": 18,
      "dependency_repo": "OWNER/REPO",
      "dependency": 12,
      "evidence": "provided by issue-authoring agent"
    }
  ]
}
```

The request's `issues` list remains the authoritative set of issues to add. Dependency edges whose `issue` is not in that list are ignored. Missing `dependency_repo` defaults to the target repo. Evidence defaults to `provided dependency`.

## Scheduler Behavior

The scheduler converts the provided dependency edges into the same internal `Dependency` objects used by dependency analysis. It then reuses the existing analyze-style state decision:

- If an issue has no unsatisfied dependencies, move it directly to `ready`.
- If an issue has unsatisfied dependencies, move it directly to `waiting_dependencies`.
- Never move a dependency-blocked issue through `ready`.

The final state decision runs under the scheduler lock, so `auto_start_ready` cannot race with dependency insertion.

## Global Skill

The repository ships a template skill for global installation. Onboarding installs a local copy that bakes in the user's Agent Desk checkout path and default dashboard URL.

At runtime, the skill:

1. Infers the current repository's git root and `OWNER/REPO`.
2. Checks the running Agent Desk dashboard at `AGENT_DESK_URL` or the installed default URL.
3. Asks the user to start Agent Desk if the dashboard is unreachable.
4. Confirms the current repo is registered in `/api/state`.
5. Calls `sync-issues`.
6. Calls `include-issues` with `dependency_mode: "provided"`.
7. Verifies states with `GET /api/issues?repo=...`.

The skill must not start the dashboard, run issues, or call `run-next`.
