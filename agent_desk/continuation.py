from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AgentDeskConfig, RepoConfig
from .store import Store
from .worker import CommandRunner, extract_thread_id, parse_json_object


@dataclass(frozen=True)
class ContinuationResult:
    ok: bool
    message: str
    run_id: int


class ContinuationRunner:
    def __init__(self, config: AgentDeskConfig, store: Store, runner: CommandRunner | None = None):
        self.config = config
        self.store = store
        self.runner = runner or CommandRunner()

    def request_changes(self, run_id: int, feedback: str) -> ContinuationResult:
        run = self.store.get_run(run_id)
        prompt = render_request_changes_prompt(run, feedback)
        return self._resume(run_id, "request-changes", prompt, success_state="pr_open", success_stage="changes addressed")

    def approve_finish(self, run_id: int) -> ContinuationResult:
        run = self.store.get_run(run_id)
        repo = self._repo_for_run(run)
        prompt = render_approve_finish_prompt(run, ready_label=repo.ready_label)
        return self._resume(
            run_id,
            "approve-finish",
            prompt,
            success_state="done",
            success_stage="finished",
            sandbox=repo.closeout_sandbox,
        )

    def _repo_for_run(self, run: dict[str, Any]) -> RepoConfig:
        for repo in self.config.repos:
            if repo.name == run["repo_name"]:
                return repo
        raise KeyError(f"repository {run['repo_name']} is not configured")

    def _resume(
        self,
        run_id: int,
        action: str,
        prompt: str,
        *,
        success_state: str,
        success_stage: str,
        sandbox: str = "workspace-write",
    ) -> ContinuationResult:
        run = self.store.get_run(run_id)
        worktree_raw = str(run.get("worktree_path") or "")
        worktree_path = Path(worktree_raw)
        run_dir_raw = str(run.get("run_dir") or "")
        run_dir = Path(run_dir_raw) if run_dir_raw else self.config.data_dir / "runs" / f"issue-{run['issue_number']}" / f"run-{run['attempt']}"
        run_dir.mkdir(parents=True, exist_ok=True)
        thread_id = self._thread_id_for_run(run_id, run, run_dir)
        if not thread_id:
            return self._block(run_id, f"{action} requires codex_thread_id")
        if not worktree_raw:
            return self._block(run_id, f"{action} requires worktree_path")

        prompt_path = run_dir / f"{action}-prompt.md"
        result_path = run_dir / f"{action}-result.json"
        prompt_path.write_text(prompt, encoding="utf-8")
        self.store.update_run(run_id, state="running", stage=action)
        self.store.add_event(run_id, "info", action, f"Starting {action}", {})
        argv = [
            "codex",
            "--ask-for-approval",
            "never",
            "--sandbox",
            sandbox,
            "-C",
            str(worktree_path),
            "exec",
            "resume",
            "--json",
        ]
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "worker-result.schema.json"
        if schema_path.exists():
            argv.extend(["--output-schema", str(schema_path)])
        argv.extend(["--output-last-message", str(result_path), thread_id, "-"])
        completed = self.runner.run(
            argv,
            cwd=worktree_path,
            stdin=prompt,
            timeout=self.config.worker_timeout_seconds,
            stdout_path=run_dir / f"{action}.stdout.jsonl",
            stderr_path=run_dir / f"{action}.stderr.log",
        )
        if completed.returncode != 0:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=f"{action} failed")
            self.store.add_event(run_id, "error", action, f"{action} failed", {"detail": completed.stderr[-4000:]})
            return ContinuationResult(False, f"{action} failed", run_id)

        payload = parse_resume_result(result_path, completed.stdout)
        status = str(payload.get("status") or "failed")
        summary = str(payload.get("summary") or status)
        if status == "done":
            pr_url = str(payload.get("pr_url") or run.get("pr_url") or "")
            self.store.update_run(run_id, state=success_state, stage=success_stage, pr_url=pr_url, last_error="")
            self.store.add_event(run_id, "info", action, summary, payload)
            return ContinuationResult(True, summary, run_id)
        state = "blocked" if status == "blocked" else "failed"
        self.store.update_run(run_id, state=state, stage=state, last_error=summary)
        self.store.add_event(run_id, "warning", action, summary, payload)
        return ContinuationResult(False, summary, run_id)

    def _block(self, run_id: int, message: str) -> ContinuationResult:
        self.store.update_run(run_id, state="blocked", stage="blocked", last_error=message)
        self.store.add_event(run_id, "error", "continuation", message, {})
        return ContinuationResult(False, message, run_id)

    def _thread_id_for_run(self, run_id: int, run: dict[str, Any], run_dir: Path) -> str:
        thread_id = str(run.get("codex_thread_id") or "")
        if thread_id:
            return thread_id
        stdout_path = run_dir / "stdout.jsonl"
        if not stdout_path.exists():
            return ""
        thread_id = extract_thread_id(stdout_path.read_text(encoding="utf-8", errors="replace"))
        if thread_id:
            self.store.update_run(run_id, codex_thread_id=thread_id)
        return thread_id


def parse_resume_result(result_path: Path, stdout: str) -> dict[str, Any]:
    candidates = []
    if result_path.exists():
        candidates.append(result_path.read_text(encoding="utf-8"))
    candidates.extend(line for line in stdout.splitlines() if line.strip())
    for candidate in candidates:
        parsed = parse_json_object(candidate)
        if parsed and "status" in parsed:
            return parsed
    return {
        "status": "failed",
        "summary": "Could not parse continuation result JSON",
        "tests": [],
        "questions": [],
        "risks": [],
        "pr_url": "",
        "decision_log": [],
    }


def render_request_changes_prompt(run: dict[str, Any], feedback: str) -> str:
    return f"""Human requested changes on the existing Agent Desk pull request.

Repository: {run['repo_name']}
Issue: #{run['issue_number']} {run['issue_title']}
Issue URL: {run['issue_url']}
Pull request: {run.get('pr_url') or '(missing PR URL)'}
Branch: {run['branch_name']}

Human feedback:
---
{feedback}
---

Continue from the existing Codex thread context. Address the feedback with the smallest appropriate change, run relevant verification, and push the updates to the existing PR branch. Do not merge the PR.

Return JSON with status, summary, tests, questions, risks, pr_url, and decision_log.
"""


def render_approve_finish_prompt(run: dict[str, Any], *, ready_label: str) -> str:
    return f"""Human approval has been granted for this Agent Desk pull request.

Repository: {run['repo_name']}
Issue: #{run['issue_number']} {run['issue_title']}
Issue URL: {run['issue_url']}
Pull request: {run.get('pr_url') or '(missing PR URL)'}
Branch: {run['branch_name']}

Continue from the existing Codex thread context and perform the closeout workflow:
1. Inspect the pull request status and checks. Do not merge while checks are pending or failing.
2. If checks are not all successful, return status "blocked" with the concrete reason.
3. If checks are successful, merge the PR using the repository's normal merge method.
4. Sync the local base branch with origin.
5. Remove the local worktree and prune stale worktree metadata when it is safe.
6. Close or update the completed issue if GitHub did not do it automatically.
7. Inspect related open issues and determine which open issues are now unblocked and ready for an agent. Add the configured ready label {ready_label} to issues that can now be run. Do not start those issues.
8. Report exactly which PR, worktree, branch, issue, and follow-up issue labels were changed.

Return JSON with status, summary, tests, questions, risks, pr_url, and decision_log.
"""
