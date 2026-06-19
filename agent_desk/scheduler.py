from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

from .config import AgentDeskConfig, RepoConfig
from .continuation import ContinuationRunner
from .github_client import GitHubClient
from .github_client import PullRequestChecksStatus
from .store import Store, utc_now
from .worker import Worker, slugify


MAX_CI_FIX_ATTEMPTS = 3
CLOSEOUT_STAGES = {
    "approve-finish queued",
    "approve-finish",
    "auto-finishing after ci success",
    "auto-finish",
}


@dataclass(frozen=True)
class RunNextResult:
    started: bool
    message: str
    run_id: int | None = None


@dataclass
class SchedulerSettings:
    auto_start_ready: bool = False
    max_concurrent_runs: int = 3
    requires_human_review: bool = True
    single_closeout_per_workspace: bool = True

    def as_payload(self) -> dict[str, bool | int]:
        return {
            "auto_start_ready": self.auto_start_ready,
            "max_concurrent_runs": self.max_concurrent_runs,
            "requires_human_review": self.requires_human_review,
            "single_closeout_per_workspace": self.single_closeout_per_workspace,
        }


class Scheduler:
    def __init__(
        self,
        config: AgentDeskConfig,
        store: Store,
        github: GitHubClient | None = None,
        worker: Worker | None = None,
        continuation_factory: Callable[[AgentDeskConfig, Store], ContinuationRunner] | None = None,
    ):
        self.config = config
        self.store = store
        self.github = github or GitHubClient()
        self.worker = worker or Worker(config, store)
        self.continuation_factory = continuation_factory or (lambda config, store: ContinuationRunner(config, store))
        self.settings = SchedulerSettings(max_concurrent_runs=max(1, config.max_concurrent_runs))
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

    def settings_payload(self) -> dict[str, bool | int]:
        with self._lock:
            return self.settings.as_payload()

    def update_settings(
        self,
        *,
        auto_start_ready: bool | None = None,
        max_concurrent_runs: int | None = None,
        requires_human_review: bool | None = None,
        single_closeout_per_workspace: bool | None = None,
    ) -> dict[str, bool | int]:
        with self._lock:
            if auto_start_ready is not None:
                self.settings.auto_start_ready = bool(auto_start_ready)
            if max_concurrent_runs is not None:
                value = int(max_concurrent_runs)
                if value < 1:
                    raise ValueError("max_concurrent_runs must be at least 1")
                self.settings.max_concurrent_runs = value
            if requires_human_review is not None:
                self.settings.requires_human_review = bool(requires_human_review)
            if single_closeout_per_workspace is not None:
                self.settings.single_closeout_per_workspace = bool(single_closeout_per_workspace)
            return self.settings.as_payload()

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            if not self._paused:
                try:
                    self.poll_once()
                except Exception:
                    pass
            self._stop.wait(self.config.poll_interval_seconds)

    def poll_once(self) -> list[RunNextResult]:
        results = []
        results.extend(self.discover_ready())
        results.extend(self.auto_start_ready_runs())
        results.extend(self.monitor_prs())
        return results

    def run_available(self) -> list[RunNextResult]:
        return self.discover_ready()

    def discover_ready(self) -> list[RunNextResult]:
        results: list[RunNextResult] = []
        with self._lock:
            if self._paused:
                return []
            for repo in self.config.repos:
                for issue in self._ready_issues(repo):
                    results.append(self._queue_issue(repo, issue))
        return results

    def run_next(self) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused")
            if self._active_count() >= self.settings.max_concurrent_runs:
                return RunNextResult(False, "Max concurrent runs reached")
            ready = self.store.list_runs({"ready"})
            if not ready:
                for repo in self.config.repos:
                    for issue in self._ready_issues(repo):
                        self._queue_issue(repo, issue)
                ready = self.store.list_runs({"ready"})
            if not ready:
                return RunNextResult(False, "No agent:ready issues found")
            return self._start_ready_run(int(ready[-1]["id"]))

    def start_run(self, run_id: int) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused", run_id)
            if self._active_count() >= self.settings.max_concurrent_runs:
                return RunNextResult(False, "Max concurrent runs reached", run_id)
            return self._start_ready_run(run_id)

    def approve_finish(self, run_id: int) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused", run_id)
            run = self.store.get_run(run_id)
            if run["state"] != "pr_open":
                return RunNextResult(False, f"Run #{run_id} is not open for PR closeout", run_id)
            try:
                repo = self._repo_for_run(run)
            except KeyError as exc:
                message = str(exc).strip("'")
                self.store.update_run(run_id, state="blocked", stage="blocked", last_error=message)
                self.store.add_event(run_id, "error", "configuration", message, {"repo": run["repo_name"]})
                return RunNextResult(False, message, run_id)
            if self.settings.single_closeout_per_workspace:
                conflict = self._closeout_in_progress(repo, exclude_run_id=run_id)
                if conflict:
                    return self._block_closeout_for_workspace(run_id, repo, conflict)
            self.store.update_run(run_id, state="running", stage="approve-finish queued", last_error="")
            self.store.add_event(run_id, "info", "approve-finish", "Starting approved closeout", {})
            self._start_daemon_thread(self._run_approve_finish, {"run_id": run_id})
            return RunNextResult(True, "Approve and finish started", run_id)

    def auto_start_ready_runs(self) -> list[RunNextResult]:
        results: list[RunNextResult] = []
        with self._lock:
            if self._paused or not self.settings.auto_start_ready:
                return []
            while self._active_count() < self.settings.max_concurrent_runs:
                ready = self.store.list_runs({"ready"})
                if not ready:
                    break
                result = self._start_ready_run(int(ready[-1]["id"]))
                results.append(result)
                if not result.started:
                    break
        return results

    def _active_count(self) -> int:
        return len(self.store.list_runs({"running"}))

    def _ready_issues(self, repo: RepoConfig) -> list[dict]:
        issues = self.github.list_ready_issues(repo.name, repo.ready_label, limit=10)
        return [
            issue
            for issue in issues
            if not self.store.find_open_run(repo.name, int(issue["number"]))
        ]

    def _queue_issue(self, repo: RepoConfig, issue: dict) -> RunNextResult:
        issue_number = int(issue["number"])
        title = str(issue.get("title") or f"Issue {issue_number}")
        attempt = self.store.next_attempt(repo.name, issue_number)
        branch = f"agent/issue-{issue_number}-{slugify(title)[:48]}-run-{attempt}"
        run_id = self.store.create_run(
            repo_name=repo.name,
            issue_number=issue_number,
            issue_title=title,
            issue_body=str(issue.get("body") or ""),
            issue_url=str(issue.get("url") or ""),
            branch_name=branch,
        )
        self.store.update_run(run_id, state="ready", stage="waiting for human run")
        self.store.add_event(run_id, "info", "ready", "Issue is ready to run", {"repo": repo.name})
        return RunNextResult(False, f"Queued issue #{issue_number}", run_id)

    def _start_ready_run(self, run_id: int) -> RunNextResult:
        run = self.store.get_run(run_id)
        if run["state"] != "ready":
            return RunNextResult(False, f"Run #{run_id} is not ready", run_id)
        try:
            repo = self._repo_for_run(run)
        except KeyError as exc:
            message = str(exc).strip("'")
            self.store.update_run(run_id, state="blocked", stage="blocked", last_error=message)
            self.store.add_event(run_id, "error", "configuration", message, {"repo": run["repo_name"]})
            return RunNextResult(False, message, run_id)
        issue_number = int(run["issue_number"])
        title = str(run["issue_title"])
        branch = str(run["branch_name"])
        issue_url = str(run["issue_url"])
        issue_body = str(run.get("issue_body") or "")
        self.store.add_event(run_id, "info", "claim", "Claimed issue", {"repo": repo.name})
        self.store.update_run(run_id, state="running", stage="claimed")
        if repo.mutate_github:
            self.github.add_label(repo.name, issue_number, repo.running_label)
            self.github.remove_label(repo.name, issue_number, repo.ready_label)
        self._start_daemon_thread(
            self._run_worker_for_issue,
            {
                "run_id": run_id,
                "repo": repo,
                "issue_number": issue_number,
                "issue_title": title,
                "issue_body": issue_body,
                "issue_url": issue_url,
                "branch_name": branch,
            },
        )
        return RunNextResult(True, f"Started issue #{issue_number}", run_id)

    def _start_daemon_thread(self, target, kwargs):
        thread = threading.Thread(target=target, kwargs=kwargs, daemon=True)
        thread.start()

    def _repo_for_run(self, run: dict) -> RepoConfig:
        for repo in self.config.repos:
            if repo.name == run["repo_name"]:
                return repo
        raise KeyError(f"repository {run['repo_name']} is not configured")

    def _workspace_key(self, repo: RepoConfig) -> Path:
        return repo.local_path.expanduser().resolve()

    def _closeout_in_progress(self, repo: RepoConfig, *, exclude_run_id: int | None = None) -> dict | None:
        workspace = self._workspace_key(repo)
        for run in self.store.list_runs({"running"}):
            if exclude_run_id is not None and int(run["id"]) == exclude_run_id:
                continue
            if str(run.get("stage") or "") not in CLOSEOUT_STAGES:
                continue
            try:
                other_repo = self._repo_for_run(run)
            except KeyError:
                continue
            if self._workspace_key(other_repo) == workspace:
                return run
        return None

    def _block_closeout_for_workspace(self, run_id: int, repo: RepoConfig, conflict: dict) -> RunNextResult:
        message = f"Closeout already in progress for workspace {self._workspace_key(repo)}: run #{conflict['id']}"
        payload = {
            "workspace": str(self._workspace_key(repo)),
            "active_run_id": int(conflict["id"]),
            "active_stage": str(conflict.get("stage") or ""),
            "active_pr_url": str(conflict.get("pr_url") or ""),
        }
        self.store.add_event(run_id, "warning", "closeout-blocked", message, payload)
        return RunNextResult(False, message, run_id)

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

    def monitor_prs(self) -> list[RunNextResult]:
        results: list[RunNextResult] = []
        with self._lock:
            if self._paused:
                return []
            for run in self.store.list_runs({"pr_open"}):
                pr_url = str(run.get("pr_url") or "")
                if not pr_url:
                    continue
                repo = self._repo_for_run(run)
                try:
                    pr_status = self.github.pr_checks_status(repo.name, pr_url)
                except Exception as exc:
                    self.store.update_run(
                        int(run["id"]),
                        pr_ci_status="unknown",
                        pr_ci_summary=str(exc),
                        pr_ci_checked_at=utc_now(),
                    )
                    self.store.add_event(int(run["id"]), "warning", "pr-ci", "Could not refresh PR checks", {"detail": str(exc)})
                    continue
                self.store.update_run(
                    int(run["id"]),
                    pr_ci_status=pr_status.state,
                    pr_ci_summary=pr_status.summary,
                    pr_ci_checked_at=utc_now(),
                )
                if pr_status.state == "failure":
                    results.append(self._handle_failed_ci(run, pr_status))
                elif pr_status.state == "success" and not self.settings.requires_human_review:
                    results.append(self._handle_successful_ci_without_review(run, pr_status))
        return results

    def _handle_successful_ci_without_review(
        self,
        run: dict,
        pr_status: PullRequestChecksStatus,
    ) -> RunNextResult:
        run_id = int(run["id"])
        repo = self._repo_for_run(run)
        if self.settings.single_closeout_per_workspace:
            conflict = self._closeout_in_progress(repo, exclude_run_id=run_id)
            if conflict:
                return self._block_closeout_for_workspace(run_id, repo, conflict)
        self.store.update_run(run_id, state="running", stage="auto-finishing after ci success", last_error="")
        self.store.add_event(
            run_id,
            "info",
            "auto-finish",
            "CI passed and human review is disabled; starting closeout",
            {"summary": pr_status.summary, "checks": pr_status.checks, "head_sha": pr_status.head_sha},
        )
        self._start_daemon_thread(self._run_auto_finish, {"run_id": run_id})
        return RunNextResult(True, "Started automatic closeout after successful CI", run_id)

    def _handle_failed_ci(self, run: dict, pr_status: PullRequestChecksStatus) -> RunNextResult:
        run_id = int(run["id"])
        attempts = int(run.get("ci_fix_attempts") or 0)
        if attempts >= MAX_CI_FIX_ATTEMPTS:
            message = f"CI failed after {MAX_CI_FIX_ATTEMPTS} automatic fix attempts: {pr_status.summary}"
            self.store.update_run(run_id, state="blocked", stage="ci failed after auto-fix limit", last_error=message)
            self.store.add_event(run_id, "error", "pr-ci", message, {"checks": pr_status.checks})
            return RunNextResult(False, message, run_id)

        next_attempt = attempts + 1
        self.store.update_run(
            run_id,
            state="running",
            stage=f"auto-fixing ci ({next_attempt}/{MAX_CI_FIX_ATTEMPTS})",
            ci_fix_attempts=next_attempt,
            ci_fix_last_sha=pr_status.head_sha,
            last_error="",
        )
        self.store.add_event(
            run_id,
            "warning",
            "pr-ci",
            f"CI failed; starting automatic fix attempt {next_attempt}/{MAX_CI_FIX_ATTEMPTS}",
            {"summary": pr_status.summary, "checks": pr_status.checks, "head_sha": pr_status.head_sha},
        )
        self._start_daemon_thread(
            self._run_ci_fix,
            {"run_id": run_id, "pr_status": pr_status, "attempt": next_attempt},
        )
        return RunNextResult(True, f"Started automatic CI fix attempt {next_attempt}", run_id)

    def _run_ci_fix(self, *, run_id: int, pr_status: PullRequestChecksStatus, attempt: int) -> None:
        try:
            self.continuation_factory(self.config, self.store).fix_ci(
                run_id,
                pr_status,
                attempt=attempt,
                max_attempts=MAX_CI_FIX_ATTEMPTS,
            )
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "fix-ci", "Automatic CI fix failed", {"detail": str(exc)})

    def _run_approve_finish(self, *, run_id: int) -> None:
        try:
            self.continuation_factory(self.config, self.store).approve_finish(run_id)
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "approve-finish", "Approved closeout failed", {"detail": str(exc)})

    def _run_auto_finish(self, *, run_id: int) -> None:
        try:
            self.continuation_factory(self.config, self.store).finish_after_ci_success(run_id)
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "auto-finish", "Automatic closeout failed", {"detail": str(exc)})
