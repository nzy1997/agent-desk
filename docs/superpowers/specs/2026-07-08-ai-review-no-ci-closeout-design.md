# AI Review And No-CI Closeout Design

Date: 2026-07-08
Status: Approved for implementation planning

## Goal

Agent Desk should distinguish a pull request with no configured CI from a pull
request whose CI status is unknown. Repositories such as `nzy1997/AutoQEC` can
then close out automatically when human review is disabled, even when GitHub
reports no checks.

Agent Desk should also support an optional workspace setting that runs an
independent AI review before automatic closeout. The AI review uses Codex CLI
with a dedicated English prompt and must be separate from the original
implementation thread. If the review passes, Agent Desk continues to automatic
closeout. If the review requests changes, Agent Desk sends the review feedback
back to the original Codex thread through the existing request-changes flow.

## Non-Goals

- Do not add Python package dependencies.
- Do not integrate an external review service or GitHub App.
- Do not replace manual `Request changes` or `Approve & finish` controls.
- Do not make AI review mandatory for every workspace.
- Do not teach the reviewer worker to push, merge, or edit files.

## Current Behavior

`GitHubClient.pr_checks_status()` returns `state="unknown"` when `gh pr checks`
returns no JSON checks. The dashboard shows this as `CI unknown`. The scheduler
only starts automatic closeout when the state is `success` and human review is
disabled, so a repository with no checks remains in `pr_open` indefinitely.

The current closeout paths are:

- Manual: `pr_open` -> `approve-finish queued` -> `approve-finish` -> `done`.
- Automatic after CI success: `pr_open` -> `auto-finishing after ci success` ->
  `auto-finish` -> `done`.
- Failed CI: `pr_open` -> `auto-fixing ci (N/3)` -> `fix-ci-N` -> `pr_open`.

## Proposed Design

### CI State Model

Keep `pr_ci_status` as the dashboard-facing field, but add a real `no_ci`
value:

- `success`: GitHub reported checks and none are failing or pending.
- `pending`: GitHub reported checks and at least one is still pending.
- `failure`: GitHub reported checks or mergeability information that blocks the
  pull request.
- `no_ci`: GitHub reported that no checks exist for the PR or head branch.
- `unknown`: Agent Desk could not determine the status because of an error,
  missing PR URL, invalid JSON, or another ambiguous response.

`no_ci` is a concrete repository condition, not an error. `unknown` remains a
diagnostic state that should not trigger automatic closeout.

### Workspace Setting

Add `enable_ai_review: bool = false` to `RepoConfig` and `SchedulerSettings`.
Expose it in the Workspace Settings panel as `AI review before closeout`.

Generated config and appended repo config should include:

```toml
enable_ai_review = false
```

The setting is runtime-editable like `requires_human_review`. It is independent
from human review:

- `requires_human_review=true`: no automatic closeout is started; the user still
  controls PR closeout manually.
- `requires_human_review=false, enable_ai_review=false`: `success` or `no_ci`
  starts automatic closeout.
- `requires_human_review=false, enable_ai_review=true`: `success` or `no_ci`
  starts AI review first.

### New AI Review Worker

Add a focused `AIReviewRunner` component. It should be independent from
`ContinuationRunner` because it reviews the PR from a fresh perspective instead
of resuming the implementation thread.

Responsibilities:

- Write `ai-review-prompt.md`, `ai-review.stdout.jsonl`,
  `ai-review.stderr.log`, and `ai-review-result.json` into the run directory.
- Run `codex exec --json --output-last-message ai-review-result.json -` with a
  dedicated English reviewer prompt.
- Use the PR worktree as `cwd`; block with a clear error if the run has no
  worktree path.
- Parse a structured JSON result.
- Never push, merge, or modify repository files as part of review.

Expected review result shape:

```json
{
  "status": "approved | changes_requested | blocked",
  "summary": "Short reviewer summary.",
  "findings": ["Actionable finding or non-blocking note."],
  "feedback": "Text suitable for sending to the implementation Codex thread.",
  "risks": ["Residual risk."],
  "pr_url": "https://github.com/OWNER/REPO/pull/123"
}
```

Store lightweight review fields on the run record:

- `ai_review_status`
- `ai_review_summary`
- `ai_review_feedback`
- `ai_review_checked_at`
- `ai_review_head_sha`

Existing records default these fields to empty strings.

### Reviewer Prompt Contract

The prompt sent to Codex CLI must be in English. It should tell Codex:

- You are the reviewer, not the implementer.
- Do not edit files, commit, push, or merge.
- Review the PR against the issue objective and acceptance criteria.
- Inspect PR metadata and diff using GitHub and local git commands where useful.
- Treat `no_ci` as a real absence of CI and inspect the worker's recorded local
  verification more carefully.
- Prefer `changes_requested` when there is a clear, actionable fix.
- Use `blocked` when the review cannot be completed reliably.
- Return only JSON matching the review result shape.

The prompt should include:

- repository name
- issue number, title, URL, and body
- pull request URL
- branch name and base branch
- current CI status and summary
- worker summary, tests, questions, risks, and decision log from the run events
  by reading the latest `codex-done` or `worker-result` payload if one exists
- run directory and worktree path

### Scheduler Flow

When `monitor_prs()` sees a `pr_open` run:

1. Refresh PR checks.
2. Store `pr_ci_status`, `pr_ci_summary`, and `pr_ci_checked_at`.
3. If status is `failure`, use the existing CI-fix path.
4. If status is `pending`, leave the run in `pr_open`.
5. If status is `unknown`, leave the run in `pr_open` and show the diagnostic
   summary.
6. If status is `success` or `no_ci`:
   - If human review is required, leave the run in `pr_open`.
   - If AI review is disabled, start automatic closeout.
   - If AI review is enabled and `ai_review_status` is not `approved` for the
     current PR head SHA, start AI review.
   - If AI review is already approved for the current PR head SHA, start
     automatic closeout.

`ai_review_head_sha` is required so Agent Desk never reuses an approval after a
request-changes cycle pushes new commits.

New stages:

- `ai-review queued`
- `ai-review`
- `ai-review approved`
- `ai-review changes requested`
- `ai-review blocked`

AI review results drive follow-up actions:

- `approved`: update review fields, record an event visible in the panel, then
  start automatic closeout.
- `changes_requested`: update review fields, record an event, then use the
  existing request-changes continuation to send `feedback` to the original Codex
  thread. The run should end up back in `pr_open` after the worker pushes fixes.
- `blocked`: update review fields, move the run to `blocked`, and set
  `last_error` to the review summary.

The existing single-closeout-per-workspace guard should apply to automatic
closeout, not to the review itself. The review stage is a running job and will
still count against the workspace active-run limit naturally.

Detached jobs need a new job kind for AI review so server restarts behave like
the existing worker, request-changes, auto-finish, and CI-fix jobs.

### Dashboard

Workspace Settings adds one checkbox:

- `AI review before closeout`

PR cards should distinguish:

- `CI running`
- `CI passed`
- `CI failed`
- `No CI`
- `CI unknown`

PR cards should also show AI review status when present:

- `AI review running`
- `AI review approved - <summary>`
- `AI review changes requested - <summary>`
- `AI review blocked - <summary>`

Manual PR actions stay available while the run is `pr_open`.

### Error Handling

- If GitHub check refresh fails, keep `pr_ci_status="unknown"` and do not start
  AI review or closeout.
- If AI review cannot parse JSON, mark the run `blocked` with a clear parse
  error and link the logs through the existing log file list.
- If AI review returns `changes_requested` without feedback, mark the run
  `blocked`; do not send an empty request-changes prompt.
- If AI review returns an unexpected status, mark the run `blocked`.
- If request-changes dispatch fails after a review, preserve the review fields
  and store the dispatch failure as `last_error`.
- If no worktree path exists, the review worker should block with a clear
  message rather than review from an ambiguous location.

### Tests

Unit coverage should include:

- `GitHubClient.pr_checks_status()` returns `no_ci` when GitHub reports no
  checks for the PR or branch.
- Empty/ambiguous command output that is not an explicit no-CI response remains
  `unknown`.
- Dashboard state includes `enable_ai_review` in workspace settings.
- Settings API accepts and returns `enable_ai_review`.
- Dashboard HTML/JS contains the new checkbox and renders `No CI` plus AI review
  statuses.
- `requires_human_review=false, enable_ai_review=false`: `success` and `no_ci`
  both start automatic closeout.
- `requires_human_review=false, enable_ai_review=true`: `success` and `no_ci`
  start AI review instead of direct closeout.
- AI review `approved` starts automatic closeout.
- AI review `changes_requested` sends `feedback` through the existing
  request-changes continuation.
- AI review `blocked` moves the run to `blocked`.
- Detached `run-job` dispatch reconstructs and runs the AI review job.
- Existing CI failure auto-fix behavior is unchanged.

## Acceptance Criteria

- AutoQEC-like repositories with no GitHub checks show `No CI`, not
  `CI unknown`.
- With human review disabled and AI review disabled, a `No CI` PR can proceed to
  automatic closeout.
- With AI review enabled, `success` and `no_ci` PRs are reviewed by an
  independent Codex worker before automatic closeout.
- Passing AI reviews are visible in the dashboard panel.
- Failing AI reviews are visible in the dashboard panel and are sent back to the
  original implementation thread for changes.
- All new behavior is covered by stdlib `unittest` tests and keeps the project
  dependency-free.
