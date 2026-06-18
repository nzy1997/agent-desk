from __future__ import annotations

from .config import RepoConfig


def render_worker_prompt(
    *,
    repo: RepoConfig,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_url: str,
    branch_name: str,
) -> str:
    test_line = repo.test_command or "Use the repository's documented test command."
    return f"""You are an autonomous Codex worker running under Agent Desk.

Do not ask interactive questions. If you can make a conservative assumption, continue.
If you cannot proceed safely, stop and return status "blocked" with at most three concrete questions.

Repository: {repo.name}
Base branch: {repo.base_branch}
Worker branch: {branch_name}
Issue: #{issue_number} {issue_title}
Issue URL: {issue_url}

Issue body:
---
{issue_body or "(empty issue body)"}
---

Required workflow:
1. Read the repository instructions, especially AGENTS.md if present.
2. Inspect the issue context and relevant files.
3. Make the smallest focused change that satisfies the issue.
4. Run this verification command when applicable: {test_line}
5. Commit your changes on the current branch.
6. Do not push, open a pull request, or wait for human input.

Final response contract:
Return JSON with these keys:
- status: one of "done", "blocked", or "failed"
- summary: short human-readable summary
- tests: list of verification commands and outcomes
- questions: list of questions when status is "blocked"; otherwise an empty list
- risks: list of residual risks or follow-up notes
"""
