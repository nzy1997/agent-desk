from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ai_settings import codex_ai_args
from .config import AgentDeskConfig
from .github_client import PullRequestChecksStatus
from .store import Store, utc_now
from .worker import CommandRunner, parse_json_object, run_directory


@dataclass(frozen=True)
class AIReviewPayload:
    status: str
    summary: str
    findings: list[str]
    feedback: str
    risks: list[str]
    pr_url: str


@dataclass(frozen=True)
class AIReviewRunResult:
    ok: bool
    status: str
    message: str
    run_id: int


class AIReviewRunner:
    def __init__(
        self,
        config: AgentDeskConfig,
        store: Store,
        runner: CommandRunner | None = None,
    ):
        self.config = config
        self.store = store
        self.runner = runner or CommandRunner()

    def review(self, run_id: int, pr_status: PullRequestChecksStatus) -> AIReviewRunResult:
        run = self.store.get_run(run_id)
        worktree_raw = str(run.get("worktree_path") or "")
        if not worktree_raw:
            return self._block(run_id, "ai-review requires worktree_path")
        worktree_path = Path(worktree_raw)
        run_dir_raw = str(run.get("run_dir") or "")
        run_dir = Path(run_dir_raw) if run_dir_raw else run_directory(self.config.data_dir, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = render_ai_review_prompt(run, pr_status)
        prompt_path = run_dir / "ai-review-prompt.md"
        result_path = run_dir / "ai-review-result.json"
        prompt_path.write_text(prompt, encoding="utf-8")
        self.store.update_run(run_id, state="running", stage="ai-review", last_error="")
        self.store.add_event(
            run_id,
            "info",
            "ai-review",
            "Starting AI review",
            {"summary": pr_status.summary, "state": pr_status.state, "head_sha": pr_status.head_sha},
        )
        argv = [
            "codex",
            *codex_ai_args(run),
            "--ask-for-approval",
            "never",
            "--sandbox",
            "read-only",
            "-C",
            str(worktree_path),
            "exec",
            "--json",
        ]
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "ai-review-result.schema.json"
        if schema_path.exists():
            argv.extend(["--output-schema", str(schema_path)])
        argv.extend(["--output-last-message", str(result_path), "-"])
        completed = self.runner.run(
            argv,
            cwd=worktree_path,
            stdin=prompt,
            timeout=self.config.worker_timeout_seconds,
            idle_timeout=self.config.worker_idle_timeout_seconds,
            stdout_path=run_dir / "ai-review.stdout.jsonl",
            stderr_path=run_dir / "ai-review.stderr.log",
        )
        if completed.returncode != 0:
            message = "AI review failed"
            if completed.timeout_reason == "idle":
                message = "AI review idle timeout"
            elif completed.timeout_reason == "timeout":
                message = "AI review timeout"
            return self._block(run_id, message, {"detail": completed.stderr[-4000:]})
        try:
            payload = parse_ai_review_result(result_path, completed.stdout)
        except ValueError as exc:
            return self._block(run_id, str(exc))
        if not result_path.exists():
            result_path.write_text(json.dumps(payload.__dict__), encoding="utf-8")
        return self._record_payload(run_id, payload, pr_status)

    def _record_payload(
        self,
        run_id: int,
        payload: AIReviewPayload,
        pr_status: PullRequestChecksStatus,
    ) -> AIReviewRunResult:
        status = payload.status
        if status == "changes_requested" and not payload.feedback.strip():
            return self._block(run_id, "AI review changes_requested feedback is required")
        if status not in {"approved", "changes_requested", "blocked"}:
            return self._block(run_id, f"AI review returned unexpected status: {status}")
        fields = {
            "ai_review_status": status,
            "ai_review_summary": payload.summary,
            "ai_review_feedback": payload.feedback,
            "ai_review_checked_at": utc_now(),
            "ai_review_head_sha": pr_status.head_sha,
        }
        if status == "blocked":
            self.store.update_run(
                run_id,
                state="blocked",
                stage="ai-review blocked",
                last_error=payload.summary,
                **fields,
            )
            self.store.add_event(run_id, "warning", "ai-review", payload.summary, payload.__dict__)
            return AIReviewRunResult(False, "blocked", payload.summary, run_id)
        stage = "ai-review approved" if status == "approved" else "ai-review changes requested"
        self.store.update_run(run_id, state="pr_open", stage=stage, last_error="", **fields)
        self.store.add_event(run_id, "info", "ai-review", payload.summary, payload.__dict__)
        return AIReviewRunResult(True, status, payload.summary, run_id)

    def _block(
        self,
        run_id: int,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> AIReviewRunResult:
        self.store.update_run(
            run_id,
            state="blocked",
            stage="ai-review blocked",
            ai_review_status="blocked",
            ai_review_summary=message,
            ai_review_checked_at=utc_now(),
            last_error=message,
        )
        self.store.add_event(run_id, "error", "ai-review", message, payload or {})
        return AIReviewRunResult(False, "blocked", message, run_id)


def render_ai_review_prompt(run: dict[str, Any], pr_status: PullRequestChecksStatus) -> str:
    worker = latest_worker_payload(run)
    return f"""You are an independent AI reviewer for an Agent Desk pull request.

You are not the implementation worker. Do not edit files, commit, push, or merge.
Review the pull request and return a structured review decision.

Repository: {run['repo_name']}
Issue: #{run['issue_number']} {run.get('issue_title') or ''}
Issue URL: {run.get('issue_url') or ''}
Pull request: {run.get('pr_url') or '(missing PR URL)'}
Branch: {run.get('branch_name') or ''}
Run directory: {run.get('run_dir') or ''}
Worktree: {run.get('worktree_path') or ''}

Issue body:
---
{run.get('issue_body') or ''}
---

PR gate status: {pr_status.state}
PR gate summary: {pr_status.summary}
PR head SHA: {pr_status.head_sha or '(unknown)'}

Worker summary: {worker.get('summary') or '(missing)'}
Worker tests:
{format_string_list(worker.get('tests') or [])}
Worker questions:
{format_string_list(worker.get('questions') or [])}
Worker risks:
{format_string_list(worker.get('risks') or [])}
Worker decision log:
{format_string_list(worker.get('decision_log') or [])}

Review instructions:
1. Inspect the PR metadata and diff using gh and local git commands where useful.
2. Check whether the PR satisfies the issue objective and stated acceptance criteria.
3. Check whether the worker's verification evidence is credible and scoped to the change.
4. Check for obvious regressions, unrelated changes, missing tests, or unsafe closeout risk.
5. Treat no_ci as a real absence of GitHub CI. When PR gate status is no_ci, inspect the recorded local verification especially carefully.
6. Do not make changes. Do not push. Do not merge.

Decision rules:
- Use approved when there are no blocking findings.
- Use changes_requested when there is a clear, actionable fix. Put the exact request in feedback so Agent Desk can send it to the implementation worker.
- Use blocked when you cannot complete a reliable review, cannot inspect the diff, or find a high-risk problem without a clear repair instruction.

Return only JSON with this shape:
{{
  "status": "approved | changes_requested | blocked",
  "summary": "Short reviewer summary.",
  "findings": ["Actionable finding or non-blocking note."],
  "feedback": "Text suitable for sending to the implementation Codex thread.",
  "risks": ["Residual risk."],
  "pr_url": "{run.get('pr_url') or ''}"
}}
"""


def latest_worker_payload(run: dict[str, Any]) -> dict[str, Any]:
    for event in reversed(run.get("events") or []):
        if str(event.get("event_type") or "") not in {"codex-done", "worker-result"}:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            return payload
    return {}


def format_string_list(values: list[Any]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {str(value)}" for value in values)


def parse_ai_review_result(result_path: Path, stdout: str) -> AIReviewPayload:
    candidates = []
    if result_path.exists():
        candidates.append(result_path.read_text(encoding="utf-8"))
    candidates.extend(line for line in stdout.splitlines() if line.strip())
    for candidate in candidates:
        parsed = parse_json_object(candidate)
        if parsed and parsed.get("status") in {"approved", "changes_requested", "blocked"}:
            return normalize_ai_review_payload(parsed)
    raise ValueError("Could not parse AI review result JSON")


def normalize_ai_review_payload(payload: dict[str, Any]) -> AIReviewPayload:
    findings_raw = payload.get("findings") or []
    risks_raw = payload.get("risks") or []
    findings = [str(item) for item in findings_raw] if isinstance(findings_raw, list) else [str(findings_raw)]
    risks = [str(item) for item in risks_raw] if isinstance(risks_raw, list) else [str(risks_raw)]
    return AIReviewPayload(
        status=str(payload.get("status") or "blocked"),
        summary=str(payload.get("summary") or payload.get("status") or "AI review returned no summary"),
        findings=findings,
        feedback=str(payload.get("feedback") or ""),
        risks=risks,
        pr_url=str(payload.get("pr_url") or ""),
    )
