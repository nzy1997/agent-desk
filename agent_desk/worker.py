from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

from .config import AgentDeskConfig, RepoConfig
from .prompt import render_worker_prompt
from .store import Store


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CommandCall:
    argv: list[str]
    cwd: Path | None
    stdin: str
    timeout: int | None


@dataclass(frozen=True)
class WorkerResult:
    status: str
    summary: str
    tests: list[str]
    questions: list[str]
    risks: list[str]
    pr_url: str
    decision_log: list[str]
    run_dir: Path


class CommandRunner:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        stdin: str = "",
        timeout: int | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if stdout_path:
            stdout_path.write_text(completed.stdout, encoding="utf-8")
        if stderr_path:
            stderr_path.write_text(completed.stderr, encoding="utf-8")
        return CommandResult(argv, completed.returncode, completed.stdout, completed.stderr)


class FakeCommandRunner(CommandRunner):
    def __init__(self, results: list[CommandResult]):
        self.results = results
        self.calls: list[CommandCall] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        stdin: str = "",
        timeout: int | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> CommandResult:
        self.calls.append(CommandCall(argv, cwd, stdin, timeout))
        result = self.results.pop(0)
        if stdout_path:
            stdout_path.write_text(result.stdout, encoding="utf-8")
        if stderr_path:
            stderr_path.write_text(result.stderr, encoding="utf-8")
        return CommandResult(argv, result.returncode, result.stdout, result.stderr)


class Worker:
    def __init__(self, config: AgentDeskConfig, store: Store, runner: CommandRunner | None = None):
        self.config = config
        self.store = store
        self.runner = runner or CommandRunner()

    def run_issue(
        self,
        *,
        run_id: int,
        repo: RepoConfig,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        issue_url: str,
        branch_name: str,
    ) -> WorkerResult:
        run = self.store.get_run(run_id)
        run_dir = self.config.data_dir / "runs" / f"issue-{issue_number}" / f"run-{run['attempt']}"
        run_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = self._worktree_path(repo, issue_number, branch_name, run["attempt"])
        prompt = render_worker_prompt(
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_url=issue_url,
            branch_name=branch_name,
        )
        (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        self.store.update_run(
            run_id,
            state="running",
            stage="preparing worktree",
            started_at=run["started_at"] or self.store.get_run(run_id)["updated_at"],
            run_dir=str(run_dir),
            worktree_path=str(worktree_path),
        )
        self.store.add_event(run_id, "info", "worktree", "Fetching base branch", {})

        fetch = self.runner.run(
            ["git", "-C", str(repo.local_path), "fetch", "origin", repo.base_branch],
            stdout_path=run_dir / "git-fetch.stdout.log",
            stderr_path=run_dir / "git-fetch.stderr.log",
        )
        if fetch.returncode != 0:
            return self._fail(run_id, run_dir, "failed", "git fetch failed", fetch.stderr)

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.add_event(run_id, "info", "worktree", "Creating worktree", {"path": str(worktree_path)})
        add = self.runner.run(
            [
                "git",
                "-C",
                str(repo.local_path),
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_path),
                f"origin/{repo.base_branch}",
            ],
            stdout_path=run_dir / "git-worktree.stdout.log",
            stderr_path=run_dir / "git-worktree.stderr.log",
        )
        if add.returncode != 0:
            return self._fail(run_id, run_dir, "failed", "git worktree add failed", add.stderr)

        self.store.update_run(run_id, stage="running codex")
        self.store.add_event(run_id, "info", "codex", "Starting codex exec", {})
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "worker-result.schema.json"
        result_path = run_dir / "result.json"
        argv = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "-C",
            str(worktree_path),
        ]
        if schema_path.exists():
            argv.extend(["--output-schema", str(schema_path)])
        argv.extend(["--output-last-message", str(result_path), "-"])
        codex = self.runner.run(
            argv,
            cwd=worktree_path,
            stdin=prompt,
            timeout=self.config.worker_timeout_seconds,
            stdout_path=run_dir / "stdout.jsonl",
            stderr_path=run_dir / "stderr.log",
        )
        if codex.returncode != 0:
            return self._fail(run_id, run_dir, "failed", "codex exec failed", codex.stderr)

        thread_id = extract_thread_id(codex.stdout)
        if thread_id:
            self.store.update_run(run_id, codex_thread_id=thread_id)
            self._write_resume_log(run_dir, thread_id, worktree_path)

        payload = self._parse_worker_result(result_path, codex.stdout)
        status = str(payload.get("status", "failed"))
        result = WorkerResult(
            status=status,
            summary=str(payload.get("summary", "")),
            tests=[str(item) for item in payload.get("tests", [])],
            questions=[str(item) for item in payload.get("questions", [])],
            risks=[str(item) for item in payload.get("risks", [])],
            pr_url=str(payload.get("pr_url", "")),
            decision_log=[str(item) for item in payload.get("decision_log", [])],
            run_dir=run_dir,
        )
        if result.pr_url:
            final_state = "pr_open"
        elif status in {"done", "blocked", "failed"}:
            final_state = status
        else:
            final_state = "failed"
        update_fields = {"state": final_state, "stage": final_state}
        if result.pr_url:
            update_fields["pr_url"] = result.pr_url
        self.store.update_run(run_id, **update_fields)
        self.store.add_event(
            run_id,
            "info" if final_state in {"done", "pr_open"} else "warning",
            "worker-result",
            f"Worker finished with status {final_state}",
            {
                "summary": result.summary,
                "tests": result.tests,
                "questions": result.questions,
                "risks": result.risks,
                "pr_url": result.pr_url,
                "decision_log": result.decision_log,
            },
        )

        if final_state == "done" and repo.push_pr:
            self._push_and_open_pr(run_id, repo, issue_number, issue_title, branch_name, worktree_path, run_dir)
        return result

    def _worktree_path(self, repo: RepoConfig, issue_number: int, branch_name: str, attempt: int) -> Path:
        repo_slug = slugify(repo.name)
        branch_slug = slugify(branch_name)
        return self.config.data_dir / "worktrees" / repo_slug / f"issue-{issue_number}-run-{attempt}-{branch_slug}"

    def _push_and_open_pr(
        self,
        run_id: int,
        repo: RepoConfig,
        issue_number: int,
        issue_title: str,
        branch_name: str,
        worktree_path: Path,
        run_dir: Path,
    ) -> None:
        self.store.update_run(run_id, stage="opening pull request")
        push = self.runner.run(
            ["git", "-C", str(worktree_path), "push", "-u", "origin", branch_name],
            stdout_path=run_dir / "git-push.stdout.log",
            stderr_path=run_dir / "git-push.stderr.log",
        )
        if push.returncode != 0:
            self._block_pr_open(run_id, "git push failed", push.stderr)
            return

        body_path = run_dir / "pr-body.md"
        body_path.write_text(
            f"Fixes #{issue_number}\n\nCreated by Agent Desk from issue #{issue_number}.\n",
            encoding="utf-8",
        )
        pr = self.runner.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo.name,
                "--head",
                branch_name,
                "--base",
                repo.base_branch,
                "--draft",
                "--title",
                f"Fix #{issue_number}: {issue_title}",
                "--body-file",
                str(body_path),
            ],
            stdout_path=run_dir / "gh-pr-create.stdout.log",
            stderr_path=run_dir / "gh-pr-create.stderr.log",
        )
        pr_url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
        if pr.returncode != 0 or not pr_url:
            detail = pr.stderr or pr.stdout or "gh pr create did not return a pull request URL"
            self._block_pr_open(run_id, "gh pr create failed", detail)
            return

        self.store.update_run(run_id, state="pr_open", stage="pull request opened", pr_url=pr_url)
        self.store.add_event(run_id, "info", "pr", "Opened draft pull request", {"url": pr_url})

    def _block_pr_open(self, run_id: int, summary: str, detail: str) -> None:
        self.store.update_run(run_id, state="blocked", stage="blocked", last_error=summary)
        self.store.add_event(run_id, "error", "pr", summary, {"detail": detail[-4000:]})

    def _write_resume_log(self, run_dir: Path, thread_id: str, worktree_path: Path) -> None:
        command = format_resume_command(thread_id, str(worktree_path))
        if not command:
            return
        (run_dir / "codex-resume.txt").write_text(
            f"thread_id: {thread_id}\nworktree: {worktree_path}\n\n{command}\n",
            encoding="utf-8",
        )

    def _fail(self, run_id: int, run_dir: Path, state: str, summary: str, detail: str) -> WorkerResult:
        (run_dir / "error.log").write_text(detail, encoding="utf-8")
        self.store.update_run(run_id, state=state, stage=state, last_error=summary)
        self.store.add_event(run_id, "error", state, summary, {"detail": detail[-4000:]})
        return WorkerResult(state, summary, [], [], [detail], "", [], run_dir)

    def _parse_worker_result(self, result_path: Path, stdout: str) -> dict[str, Any]:
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
            "summary": "Could not parse worker result JSON",
            "tests": [],
            "questions": [],
            "risks": [],
        }


def parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            if isinstance(value.get("message"), dict):
                return value["message"]
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def extract_thread_id(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "thread.started" and isinstance(payload.get("thread_id"), str):
            return payload["thread_id"]
    return ""


def format_resume_command(thread_id: str, worktree_path: str) -> str:
    if not thread_id or not worktree_path:
        return ""
    return " ".join(
        [
            "codex",
            "resume",
            "--include-non-interactive",
            "-C",
            shlex.quote(worktree_path),
            shlex.quote(thread_id),
        ]
    )


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-").lower()
    return slug or "item"
