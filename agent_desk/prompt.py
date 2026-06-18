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
    if repo.push_pr:
        completion_instruction = (
            "After implementation and verification are complete, create a pull request for the worker branch before "
            "returning done. Do not stop at a local commit."
        )
        finish_instruction = (
            "After implementation and verification are complete, create a pull request.\n"
            'When finishing asks what to do with the branch, choose "Push and create a Pull Request".\n'
            "8. Stop after the pull request is created. Do not merge.\n"
            '9. If implementation and verification are complete but the only failed step is creating the pull request, '
            'return status "done" with pr_url set to an empty string. Agent Desk will retry PR creation from the '
            "manager process. Record the PR creation failure in risks or questions."
        )
    else:
        completion_instruction = (
            "After implementation and verification are complete, leave the worker branch ready for local review before "
            "returning done. Do not push or merge."
        )
        finish_instruction = (
            'When finishing asks what to do with the branch, choose "Keep the branch as-is".\n'
            "8. Stop after the local branch is ready. Do not push, open a pull request, or merge."
        )
    return f"""You are an autonomous Codex worker running under Agent Desk.

This is a non-interactive Agent Desk run. Do not wait for the user.
Do not ask interactive questions.
Use the repository's existing Codex/Superpowers workflows all the way to a pull request.
{completion_instruction}

Repository: {repo.name}
Base branch: {repo.base_branch}
Worker branch: {branch_name}
Issue: #{issue_number} {issue_title}
Issue URL: {issue_url}

Issue body:
---
{issue_body or "(empty issue body)"}
---

Standing Answer Policy:
1. For any Superpowers question with a recommended option, choose the option currently marked recommended.
2. If no option is marked recommended, choose the safest conservative option from the repository context, issue context, and sibling issues.
3. Record every automatic choice in decision_log with the choice and a short reason.
4. Return status "blocked" instead of guessing for irreversible data changes, credentials/secrets, unclear public API compatibility, mutually contradictory requirements, or unusually broad unrelated edits.

Required Superpowers workflow:
1. Read the repository instructions, especially AGENTS.md if present.
2. Use superpowers:brainstorming to explore and approve the design using the Standing Answer Policy.
3. When brainstorming transitions to implementation planning, use superpowers:writing-plans.
4. When asked to choose an execution approach, choose the option currently marked recommended. Do not hard-code a specific execution mode.
5. Execute the implementation plan using the chosen recommended Superpowers execution approach.
6. Run this verification command when applicable: {test_line}
7. {finish_instruction}

Final response contract:
Return JSON with these keys:
- status: one of "done", "blocked", or "failed"
- summary: short human-readable summary
- tests: list of verification commands and outcomes
- questions: list of questions when status is "blocked"; otherwise an empty list
- risks: list of residual risks or follow-up notes
- pr_url: pull request URL when one was created; otherwise an empty string
- decision_log: list of automatic Superpowers answers and brief reasons
"""
