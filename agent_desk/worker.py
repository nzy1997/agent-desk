from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
from typing import Any

from .ai_settings import codex_ai_args
from .codex_activity import CodexThreadActivityMonitor
from .codex_executable import resolve_codex_argv
from .config import AgentDeskConfig, RepoConfig
from .prompt import render_worker_prompt
from .repository_setup import repository_setup_lock
from .store import Store


GIT_FETCH_MAX_ATTEMPTS = 3
GIT_FETCH_RETRY_DELAYS = (0.1, 0.25)


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timeout_reason: str = ""


@dataclass(frozen=True)
class CommandCall:
    argv: list[str]
    cwd: Path | None
    stdin: str
    timeout: int | None
    idle_timeout: float | None = None


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


def is_retryable_ref_lock_error(stderr: str) -> bool:
    detail = str(stderr or "").lower()
    return "cannot lock ref" in detail and (
        "but expected" in detail or "unable to update local ref" in detail
    )


def is_codex_json_command(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    if Path(argv[0]).name != "codex":
        return False

    options_with_values = {
        "--ask-for-approval",
        "--output-last-message",
        "--output-schema",
        "--sandbox",
        "-C",
    }

    def next_token_index(tokens: Sequence[str], index: int) -> int:
        token = tokens[index]
        if token == "--":
            return len(tokens)
        if token.startswith("--") and "=" in token:
            return index + 1
        if token in options_with_values:
            return index + 2
        if token.startswith("-"):
            return index + 1
        return index + 1

    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "exec":
            break
        if not token.startswith("-"):
            return False
        index = next_token_index(argv, index)

    if index >= len(argv) or argv[index] != "exec":
        return False

    exec_args = argv[index + 1 :]
    if "--json" not in exec_args:
        return False

    position = 0
    first_positional: str | None = None
    while position < len(exec_args):
        token = exec_args[position]
        if token == "--":
            break
        if token.startswith("--") and "=" in token:
            position += 1
            continue
        if token in options_with_values:
            position += 2
            continue
        if token.startswith("-"):
            position += 1
            continue
        first_positional = token
        break

    return first_positional in {None, "resume"}


class CommandRunner:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        stdin: str = "",
        timeout: int | None = None,
        idle_timeout: float | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        activity_monitor: CodexThreadActivityMonitor | None = None,
        activity_monitor_poll_interval: float = 5.0,
    ) -> CommandResult:
        codex_json_command = is_codex_json_command(argv)
        argv = resolve_codex_argv(argv)
        started_at = time.monotonic()
        last_activity_at = started_at
        last_activity_source = "process start"
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        lock = threading.Lock()
        stdout_handle = stdout_path.open("w", encoding="utf-8") if stdout_path else None
        stderr_handle = stderr_path.open("w", encoding="utf-8") if stderr_path else None

        def mark_activity(source: str) -> None:
            nonlocal last_activity_at, last_activity_source
            with lock:
                last_activity_at = time.monotonic()
                last_activity_source = source

        def append_output(
            chunks: list[str], handle: Any, text: str, *, counts_as_activity: bool = True
        ) -> None:
            nonlocal last_activity_at, last_activity_source
            with lock:
                chunks.append(text)
                if counts_as_activity:
                    last_activity_at = time.monotonic()
                    last_activity_source = "parent output"
                if handle:
                    handle.write(text)
                    handle.flush()

        def read_stream(stream: Any, chunks: list[str], handle: Any) -> None:
            try:
                for line in stream:
                    append_output(chunks, handle, line, counts_as_activity=True)
            finally:
                stream.close()

        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, stdout_chunks, stdout_handle), daemon=True)
        stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, stderr_chunks, stderr_handle), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        if process.stdin is not None:
            try:
                process.stdin.write(stdin)
                process.stdin.close()
            except BrokenPipeError:
                pass

        if activity_monitor is None and stdout_path is not None and codex_json_command:
            activity_monitor = CodexThreadActivityMonitor(
                stdout_path,
                poll_interval_seconds=activity_monitor_poll_interval,
            )

        timeout_reason = ""
        while process.poll() is None:
            now = time.monotonic()
            if activity_monitor is not None:
                signal = activity_monitor.poll(now=now)
                if signal.active:
                    mark_activity(f"{signal.source} {signal.detail}".strip())
            if timeout is not None and now - started_at >= timeout:
                timeout_reason = "timeout"
                break
            if idle_timeout is not None and now - last_activity_at >= idle_timeout:
                timeout_reason = "idle"
                break
            time.sleep(0.05)

        if timeout_reason:
            process.kill()

        returncode = process.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)

        if timeout_reason:
            elapsed = time.monotonic() - started_at
            idle_for = time.monotonic() - last_activity_at
            append_output(
                stderr_chunks,
                stderr_handle,
                f"\nagent-desk: {timeout_reason} timeout killed process after {elapsed:.1f}s"
                f" (idle for {idle_for:.1f}s; last activity: {last_activity_source})\n",
                counts_as_activity=False,
            )

        if stdout_handle:
            stdout_handle.close()
        if stderr_handle:
            stderr_handle.close()

        return CommandResult(argv, returncode, "".join(stdout_chunks), "".join(stderr_chunks), timeout_reason)


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
        idle_timeout: float | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        activity_monitor: CodexThreadActivityMonitor | None = None,
        activity_monitor_poll_interval: float = 5.0,
    ) -> CommandResult:
        self.calls.append(CommandCall(argv, cwd, stdin, timeout, idle_timeout))
        result = self.results.pop(0)
        if stdout_path:
            stdout_path.write_text(result.stdout, encoding="utf-8")
        if stderr_path:
            stderr_path.write_text(result.stderr, encoding="utf-8")
        return CommandResult(argv, result.returncode, result.stdout, result.stderr, result.timeout_reason)


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
        run_dir = run_directory(self.config.data_dir, run_id)
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
        self.store.add_event(
            run_id,
            "info",
            "repository-setup-lock",
            "Waiting for repository setup lock",
            {"path": str(repo.local_path)},
        )
        with repository_setup_lock(self.config.data_dir, repo.local_path):
            self.store.add_event(
                run_id,
                "info",
                "repository-setup-lock",
                "Acquired repository setup lock",
                {"path": str(repo.local_path)},
            )
            self.store.add_event(run_id, "info", "worktree", "Fetching base branch", {})
            fetch = self._fetch_base(repo, run_id, run_dir)
            if fetch.returncode != 0:
                return self._fail(run_id, run_dir, "failed", "git fetch failed", fetch.stderr)

            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            self.store.add_event(
                run_id,
                "info",
                "worktree",
                "Creating worktree",
                {"path": str(worktree_path)},
            )
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
                return self._fail(
                    run_id, run_dir, "failed", "git worktree add failed", add.stderr
                )

        self.store.update_run(run_id, stage="running codex")
        self.store.add_event(run_id, "info", "codex", "Starting codex exec", {})
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "worker-result.schema.json"
        result_path = run_dir / "result.json"
        argv = [
            "codex",
            *codex_ai_args(self.store.get_run(run_id)),
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
            idle_timeout=self.config.worker_idle_timeout_seconds,
            stdout_path=run_dir / "stdout.jsonl",
            stderr_path=run_dir / "stderr.log",
        )
        thread_id = extract_thread_id(codex.stdout)
        if thread_id:
            self.store.update_run(run_id, codex_thread_id=thread_id)
            self._write_resume_log(run_dir, thread_id, worktree_path)
        if codex.returncode != 0:
            if codex.timeout_reason in {"timeout", "idle"}:
                return self._interrupt_for_timeout(run_id, run_dir, codex.timeout_reason, codex.stderr)
            summary = "codex exec failed"
            return self._fail(run_id, run_dir, "failed", summary, codex.stderr)

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
        result_payload = {
            "summary": result.summary,
            "tests": result.tests,
            "questions": result.questions,
            "risks": result.risks,
            "pr_url": result.pr_url,
            "decision_log": result.decision_log,
        }
        if status == "done":
            if result.pr_url:
                codex_done_message = "Codex returned done with pull request"
            elif repo.push_pr:
                codex_done_message = "Codex returned done; resuming to open pull request"
            else:
                codex_done_message = "Codex returned done"
            self.store.add_event(run_id, "info", "codex-done", codex_done_message, result_payload)

        if status == "done" and repo.push_pr and not result.pr_url:
            self.store.update_run(run_id, state="running", stage="codex done; resuming to open pull request", last_error="")
            self.store.add_event(
                run_id,
                "info",
                "worker-result",
                "Worker finished with status done; resuming Codex to open pull request",
                result_payload,
            )
            from .continuation import ContinuationRunner

            ContinuationRunner(self.config, self.store, self.runner).open_pull_request(run_id)
            return result

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
            result_payload,
        )

        return result

    def _fetch_base(
        self, repo: RepoConfig, run_id: int, run_dir: Path
    ) -> CommandResult:
        for attempt in range(1, GIT_FETCH_MAX_ATTEMPTS + 1):
            fetch = self.runner.run(
                ["git", "-C", str(repo.local_path), "fetch", "origin", repo.base_branch],
                stdout_path=run_dir / "git-fetch.stdout.log",
                stderr_path=run_dir / "git-fetch.stderr.log",
            )
            if (
                fetch.returncode == 0
                or not is_retryable_ref_lock_error(fetch.stderr)
                or attempt == GIT_FETCH_MAX_ATTEMPTS
            ):
                return fetch
            delay = GIT_FETCH_RETRY_DELAYS[attempt - 1]
            self.store.add_event(
                run_id,
                "warning",
                "git-fetch-retry",
                "Retrying git fetch after reference lock conflict",
                {
                    "attempt": attempt,
                    "next_attempt": attempt + 1,
                    "max_attempts": GIT_FETCH_MAX_ATTEMPTS,
                    "delay_seconds": delay,
                    "detail": fetch.stderr[-4000:],
                },
            )
            time.sleep(delay)
        raise AssertionError("unreachable")

    def _worktree_path(self, repo: RepoConfig, issue_number: int, branch_name: str, attempt: int) -> Path:
        repo_slug = slugify(repo.name)
        branch_slug = slugify(branch_name)
        return self.config.data_dir / "worktrees" / repo_slug / f"issue-{issue_number}-run-{attempt}-{branch_slug}"

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

    def _interrupt_for_timeout(
        self, run_id: int, run_dir: Path, timeout_reason: str, detail: str
    ) -> WorkerResult:
        label = "idle timeout" if timeout_reason == "idle" else "timeout"
        stage = f"interrupted by {label}"
        summary = f"Timed out by Agent Desk {label}; resume from dashboard"
        (run_dir / "error.log").write_text(detail, encoding="utf-8")
        self.store.update_run(run_id, state="interrupted", stage=stage, last_error=summary)
        self.store.add_event(
            run_id,
            "warning",
            "timeout-interrupted",
            summary,
            {"timeout_reason": timeout_reason, "detail": detail[-4000:]},
        )
        return WorkerResult("interrupted", summary, [], [], [detail], "", [], run_dir)

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


def run_directory(data_dir: Path, run_id: int) -> Path:
    return data_dir / "runs" / f"run-{run_id}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-").lower()
    return slug or "item"
