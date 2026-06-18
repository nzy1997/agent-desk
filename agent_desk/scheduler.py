from __future__ import annotations

from dataclasses import dataclass
import threading
import time

from .config import AgentDeskConfig, RepoConfig
from .github_client import GitHubClient
from .store import Store
from .worker import Worker, slugify


@dataclass(frozen=True)
class RunNextResult:
    started: bool
    message: str
    run_id: int | None = None


class Scheduler:
    def __init__(
        self,
        config: AgentDeskConfig,
        store: Store,
        github: GitHubClient | None = None,
        worker: Worker | None = None,
    ):
        self.config = config
        self.store = store
        self.github = github or GitHubClient()
        self.worker = worker or Worker(config, store)
        self._paused = False
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stop.set()

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            if not self._paused:
                try:
                    self.run_next()
                except Exception:
                    pass
            self._stop.wait(self.config.poll_interval_seconds)

    def run_next(self) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused")
            if self._active_count() >= self.config.max_concurrent_runs:
                return RunNextResult(False, "Max concurrent runs reached")
            for repo in self.config.repos:
                issue = self._next_issue(repo)
                if issue:
                    return self._start_issue(repo, issue)
        return RunNextResult(False, "No agent:ready issues found")

    def _active_count(self) -> int:
        return len(self.store.list_runs({"running"}))

    def _next_issue(self, repo: RepoConfig) -> dict | None:
        issues = self.github.list_ready_issues(repo.name, repo.ready_label, limit=10)
        for issue in issues:
            if not self.store.find_open_run(repo.name, int(issue["number"])):
                return issue
        return None

    def _start_issue(self, repo: RepoConfig, issue: dict) -> RunNextResult:
        issue_number = int(issue["number"])
        title = str(issue.get("title") or f"Issue {issue_number}")
        branch = f"agent/issue-{issue_number}-{slugify(title)[:48]}"
        run_id = self.store.create_run(
            repo_name=repo.name,
            issue_number=issue_number,
            issue_title=title,
            issue_url=str(issue.get("url") or ""),
            branch_name=branch,
        )
        self.store.add_event(run_id, "info", "claim", "Claimed issue", {"repo": repo.name})
        if repo.mutate_github:
            self.github.add_label(repo.name, issue_number, repo.running_label)
            self.github.remove_label(repo.name, issue_number, repo.ready_label)
        thread = threading.Thread(
            target=self._run_worker_for_issue,
            kwargs={
                "run_id": run_id,
                "repo": repo,
                "issue_number": issue_number,
                "issue_title": title,
                "issue_body": str(issue.get("body") or ""),
                "issue_url": str(issue.get("url") or ""),
                "branch_name": branch,
            },
            daemon=True,
        )
        thread.start()
        return RunNextResult(True, f"Started issue #{issue_number}", run_id)

    def _run_worker_for_issue(
        self,
        *,
        run_id: int,
        repo: RepoConfig,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        issue_url: str,
        branch_name: str,
    ) -> None:
        try:
            self.worker.run_issue(
                run_id=run_id,
                repo=repo,
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                issue_url=issue_url,
                branch_name=branch_name,
            )
        finally:
            if repo.mutate_github:
                self._sync_terminal_labels(repo, issue_number, run_id)

    def _sync_terminal_labels(self, repo: RepoConfig, issue_number: int, run_id: int) -> None:
        run = self.store.get_run(run_id)
        self.github.remove_label(repo.name, issue_number, repo.running_label)
        if run["state"] == "pr_open":
            self.github.add_label(repo.name, issue_number, repo.pr_open_label)
        elif run["state"] == "done":
            self.github.add_label(repo.name, issue_number, repo.needs_review_label)
        elif run["state"] in {"blocked", "failed"}:
            self.github.add_label(repo.name, issue_number, repo.blocked_label)
