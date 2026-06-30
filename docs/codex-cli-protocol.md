# Codex CLI Interaction Protocol

Agent Desk runs Codex CLI as a non-interactive worker. The worker should follow the same Superpowers flow the human owner already uses manually, with Agent Desk supplying standing answers for repetitive prompts.

Agent Desk captures the full `codex exec --json` stream in `stdout.jsonl`. The
first `thread.started` event contains the Codex CLI `thread_id`; Agent Desk
stores that id on the run and writes `codex-resume.txt` with the command a human
can use to reopen the same conversation:

```bash
codex resume --include-non-interactive -C /path/to/worktree THREAD_ID
```

For automation-to-automation continuation, use `codex exec resume THREAD_ID
"prompt"`. For human intervention, prefer the interactive `codex resume`
command shown in the dashboard.

Agent Desk uses `codex exec resume` for PR review follow-up and closeout. The
manager supplies a generic prompt and records logs; the resumed Codex thread
decides the repository-specific steps.

`Request changes` uses the normal `workspace-write` sandbox. `Approve & finish`
uses each repository's `closeout_sandbox` setting, defaulting to
`workspace-write`. Repositories that expect Codex to sync the base checkout,
merge, push, update issues, and remove worktrees after human approval can set
`closeout_sandbox = "danger-full-access"`.

## Standing Answer Policy

Every worker prompt includes these rules:

- Do not wait for the user or ask interactive questions.
- For any Superpowers question with a recommended option, choose the option currently marked recommended.
- If no option is marked recommended, choose the safest conservative option from repository context, issue context, and sibling issues.
- Record every automatic choice in `decision_log` with the chosen answer and a short reason.
- Return `blocked` instead of guessing for irreversible data changes, credentials or secrets, unclear public API compatibility, mutually contradictory requirements, or unusually broad unrelated edits.

## Required Workflow

1. Read repository instructions, especially `AGENTS.md` if present.
2. Use `superpowers:brainstorming` to explore and approve the design under the Standing Answer Policy.
3. When brainstorming transitions to implementation planning, use `superpowers:writing-plans`.
4. When asked to choose an execution approach, choose the option currently marked recommended. This is intentionally not hard-coded to Subagent-Driven.
5. Execute the implementation plan using the chosen recommended Superpowers execution approach.
6. Run the configured repository test command when applicable.
7. If `push_pr = true`, choose `Push and create a Pull Request` at the finishing prompt.
8. If `push_pr = false`, choose `Keep the branch as-is` at the finishing prompt.
9. Stop after PR creation or local branch completion. Do not merge.

When `push_pr = true`, PR creation has a manager fallback. If implementation and
verification are complete but Codex CLI cannot create the pull request because
GitHub tools, network access, or credentials are unavailable inside the worker
environment, Codex should return `status: "done"` with an empty `pr_url` and
record the PR creation failure in `risks` or `questions`. Agent Desk will then
run `git push` and `gh pr create` from the manager process.

## Result Contract

Codex must return JSON matching `schemas/worker-result.schema.json`:

```json
{
  "status": "done",
  "summary": "Implemented the requested change and opened a PR.",
  "tests": ["julia --project=. -e 'using Pkg; Pkg.test()' passed"],
  "questions": [],
  "risks": [],
  "pr_url": "https://github.com/OWNER/REPO/pull/123",
  "decision_log": [
    "Execution approach: chose the option marked recommended.",
    "Finishing action: chose Push and create a Pull Request because push_pr=true."
  ]
}
```

When `pr_url` is present, Agent Desk marks the run as `pr_open` and records the URL for dashboard monitoring.
When `pr_url` is empty and `status` is `done` with `push_pr = true`, Agent Desk
attempts to open the PR itself. The run is marked `pr_open` only if the manager
gets a PR URL back; failed push or PR creation leaves the run `blocked` with the
command logs attached to the run directory.

## Worktree Retention

The worktree is part of the resume context. Agent Desk should not remove it
while a run is blocked, failed, awaiting review, or attached to an open PR.
Cleanup belongs after the PR is merged or closed and the issue is resolved.

## Manual Run Gate

Issue discovery is separate from execution. The scheduler may scan GitHub and
create local runs in state `ready`, but it must not start Codex workers until a
human clicks `Run` or invokes `run-next`.

## Resume Closeout Prompt

When a human approves a PR, Agent Desk resumes the original Codex thread with a
generic closeout prompt. The prompt instructs Codex to:

1. Inspect the PR status and checks.
2. Return `blocked` without merging if checks are pending or failing.
3. Merge only after checks are successful.
4. Sync the local base branch.
5. Remove the local worktree and stale worktree metadata when safe.
6. Close or update the completed issue.
7. Do not inspect or modify follow-up issue labels. Agent Desk manages local
   dependency unlocking from its dependency graph.

When a human requests changes, Agent Desk resumes the same thread with the
feedback and asks Codex to update and push the existing PR branch without
merging.
