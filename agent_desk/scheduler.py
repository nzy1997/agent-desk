from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable

from .config import AgentDeskConfig, RepoConfig
from .continuation import ContinuationRunner
from .github_client import GitHubClient
from .github_client import PullRequestChecksStatus
from .store import Store, utc_now
from .worker import Worker, slugify


MAX_CI_FIX_ATTEMPTS = 3

# Maps an internal dispatch target to the detached job kind run-job understands.
JOB_KIND_BY_TARGET = {
    "_run_worker_for_issue": "issue",
    "_run_approve_finish": "approve-finish",
    "_run_auto_finish": "auto-finish",
    "_run_ci_fix": "ci-fix",
}
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
    max_concurrent_runs: int = 1
    requires_human_review: bool = True
    single_closeout_per_workspace: bool = True

    @classmethod
    def from_repo(cls, repo: RepoConfig) -> "SchedulerSettings":
        return cls(
            auto_start_ready=repo.auto_start_ready,
            max_concurrent_runs=max(1, repo.max_concurrent_runs),
            requires_human_review=repo.requires_human_review,
            single_closeout_per_workspace=repo.single_closeout_per_workspace,
        )

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
        config_path: Path | None = None,
        detach_jobs: bool = False,
    ):
        self.config = config
        self.store = store
        self.github = github or GitHubClient()
        self.worker = worker or Worker(config, store)
        self.continuation_factory = continuation_factory or (lambda config, store: ContinuationRunner(config, store))
        # When True (server + run-job child), jobs run as detached processes so a
        # server restart cannot kill them; otherwise they run in a daemon thread.
        self.config_path = Path(config_path) if config_path else None
        self.detach_jobs = detach_jobs
        self._paused = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._settings_by_workspace = {
            self._workspace_key(repo): SchedulerSettings.from_repo(repo)
            for repo in config.repos
        }

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stop.set()

    def settings_payload(self, workspace_path: str | Path | None = None) -> dict[str, bool | int] | None:
        with self._lock:
            repo = self._repo_for_settings(workspace_path)
            if repo is None:
                return None
            return self._settings_for_repo(repo).as_payload()

    def update_settings(
        self,
        *,
        workspace_path: str | Path | None = None,
        auto_start_ready: bool | None = None,
        max_concurrent_runs: int | None = None,
        requires_human_review: bool | None = None,
        single_closeout_per_workspace: bool | None = None,
    ) -> dict[str, bool | int]:
        with self._lock:
            repo = self._repo_for_settings(workspace_path)
            if repo is None:
                raise ValueError("workspace_path is required when multiple workspaces are configured")
            settings = self._settings_for_repo(repo)
            if auto_start_ready is not None:
                settings.auto_start_ready = bool(auto_start_ready)
            if max_concurrent_runs is not None:
                value = int(max_concurrent_runs)
                if value < 1:
                    raise ValueError("max_concurrent_runs must be at least 1")
                settings.max_concurrent_runs = value
            if requires_human_review is not None:
                settings.requires_human_review = bool(requires_human_review)
            if single_closeout_per_workspace is not None:
                settings.single_closeout_per_workspace = bool(single_closeout_per_workspace)
            return settings.as_payload()

    def serve_forever(self) -> None:
        try:
            self.reconcile_orphans()
        except Exception:
            pass
        while not self._stop.is_set():
            if not self._paused:
                try:
                    self.poll_once()
                except Exception:
                    pass
            self._stop.wait(self.config.poll_interval_seconds)

    def poll_once(self) -> list[RunNextResult]:
        results = []
        results.extend(self.auto_start_ready_runs())
        results.extend(self.monitor_prs())
        return results

    def sync_repo_issues(self, repo_name: str, limit: int = 200) -> list[dict]:
        """Pull open issues from GitHub into the on-disk ``available`` folder.

        This is the only GitHub read for issue intake. Issues that already have a
        record (available or a run, in any state) are left untouched. Returns the
        repo's issue picker view (read from disk).
        """
        repo = self._repo_by_name(repo_name)
        if repo is None:
            raise KeyError(f"repository {repo_name} is not configured")
        issues = self.github.list_open_issues(repo.name, limit=limit)
        with self._lock:
            for issue in issues:
                number = int(issue["number"])
                if self.store.get_record(repo.name, number) is None:
                    self.store.create_available(
                        repo_name=repo.name,
                        issue_number=number,
                        issue_title=str(issue.get("title") or ""),
                        issue_url=str(issue.get("url") or ""),
                        issue_body=str(issue.get("body") or ""),
                    )
        return self.list_repo_issues(repo_name)

    def list_repo_issues(self, repo_name: str) -> list[dict]:
        """Return the repo's synced issues from disk (no GitHub call).

        ``on_desk`` is true for any record that is no longer ``available`` —
        i.e. it has been moved onto the desk as a run.
        """
        repo = self._repo_by_name(repo_name)
        if repo is None:
            raise KeyError(f"repository {repo_name} is not configured")
        return [
            {
                "number": record["issue_number"],
                "title": str(record.get("issue_title") or ""),
                "body": str(record.get("issue_body") or ""),
                "url": str(record.get("issue_url") or ""),
                "on_desk": record["state"] != "available",
            }
            for record in self.store.list_records(repo.name)
        ]

    def mark_issue_ready(self, repo_name: str, issue_number: int) -> RunNextResult:
        """Move a synced issue onto the desk (available -> ready) on disk.

        Desk state is folder-driven, so this is a pure local file move with no
        GitHub call — the ``agent:ready`` label is no longer written.
        """
        with self._lock:
            repo = self._repo_by_name(repo_name)
            if repo is None:
                return RunNextResult(False, f"{repo_name} is not a configured repository")
            open_run = self.store.find_open_run(repo.name, issue_number)
            if open_run is not None:
                return RunNextResult(True, f"{repo.name}#{issue_number} is already on the desk", open_run["id"])
            record = self.store.get_record(repo.name, issue_number)
            if record is not None and record["state"] == "available":
                run_id = self._promote_to_ready(repo, record)
            else:
                issue = record or self._fetch_issue(repo, issue_number)
                run_id = self._create_ready_run(repo, issue_number, issue)
            return RunNextResult(True, f"Added {repo.name}#{issue_number} to the desk", run_id)

    def _fetch_issue(self, repo: RepoConfig, issue_number: int) -> dict:
        try:
            return self.github.get_issue(repo.name, issue_number)
        except RuntimeError:
            return {"number": issue_number}

    def _promote_to_ready(self, repo: RepoConfig, record: dict) -> int:
        number = int(record["issue_number"])
        title = str(record.get("issue_title") or f"Issue {number}")
        branch = f"agent/issue-{number}-{slugify(title)[:48]}-run-{int(record.get('attempt', 1))}"
        self.store.update_run(
            record["id"], state="ready", stage="waiting for human run", branch_name=branch
        )
        self.store.add_event(record["id"], "info", "ready", "Issue is ready to run", {"repo": repo.name})
        return record["id"]

    def _create_ready_run(self, repo: RepoConfig, issue_number: int, issue: dict) -> int:
        title = str(issue.get("issue_title") or issue.get("title") or f"Issue {issue_number}")
        body = str(issue.get("issue_body") or issue.get("body") or "")
        url = str(issue.get("issue_url") or issue.get("url") or "")
        attempt = self.store.next_attempt(repo.name, issue_number)
        branch = f"agent/issue-{issue_number}-{slugify(title)[:48]}-run-{attempt}"
        run_id = self.store.create_run(
            repo_name=repo.name,
            issue_number=issue_number,
            issue_title=title,
            issue_url=url,
            branch_name=branch,
            issue_body=body,
        )
        self.store.update_run(run_id, state="ready", stage="waiting for human run")
        self.store.add_event(run_id, "info", "ready", "Issue is ready to run", {"repo": repo.name})
        return run_id

    def run_next(self) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused")
            ready = self.store.list_runs({"ready"})
            if not ready:
                return RunNextResult(False, "No issues on the desk are ready")
            blocked_by_limit = False
            for run in reversed(ready):
                try:
                    repo = self._repo_for_run(run)
                except KeyError:
                    return self._start_ready_run(int(run["id"]))
                if self._active_count(repo) >= self._settings_for_repo(repo).max_concurrent_runs:
                    blocked_by_limit = True
                    continue
                return self._start_ready_run(int(run["id"]))
            if blocked_by_limit:
                return RunNextResult(False, "Max concurrent runs reached for workspace")
            return RunNextResult(False, "No agent:ready issues found")

    def start_run(self, run_id: int) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused", run_id)
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
            settings = self._settings_for_repo(repo)
            if settings.single_closeout_per_workspace:
                conflict = self._closeout_in_progress(repo, exclude_run_id=run_id)
                if conflict:
                    return self._block_closeout_for_workspace(run_id, repo, conflict)
            self.store.update_run(run_id, state="running", stage="approve-finish queued", last_error="")
            self.store.add_event(run_id, "info", "approve-finish", "Starting approved closeout", {})
            self._start_daemon_thread(self._run_approve_finish, {"run_id": run_id})
            return RunNextResult(True, "Approve and finish started", run_id)

    def auto_start_ready_runs(self, workspace_path: str | Path | None = None) -> list[RunNextResult]:
        results: list[RunNextResult] = []
        with self._lock:
            if self._paused:
                return []
            repos = self._repos_for_auto_start(workspace_path)
            any_auto_start = False
            for repo in repos:
                settings = self._settings_for_repo(repo)
                if not settings.auto_start_ready:
                    continue
                any_auto_start = True
                while self._active_count(repo) < settings.max_concurrent_runs:
                    ready = self._ready_runs_for_repo(repo)
                    if not ready:
                        break
                    result = self._start_ready_run(int(ready[-1]["id"]))
                    results.append(result)
                    if not result.started:
                        break
            if any_auto_start and workspace_path is None:
                results.extend(self._block_unconfigured_ready_runs())
        return results

    def _active_count(self, repo: RepoConfig) -> int:
        workspace = self._workspace_key(repo)
        count = 0
        for run in self.store.list_runs({"running"}):
            try:
                other_repo = self._repo_for_run(run)
            except KeyError:
                continue
            if self._workspace_key(other_repo) == workspace:
                count += 1
        return count

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
        if self._active_count(repo) >= self._settings_for_repo(repo).max_concurrent_runs:
            return RunNextResult(False, "Max concurrent runs reached for workspace", run_id)
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
        if self.detach_jobs:
            self._spawn_detached_job(int(kwargs["run_id"]), JOB_KIND_BY_TARGET[target.__name__])
            return
        thread = threading.Thread(target=target, kwargs=kwargs, daemon=True)
        thread.start()

    def _run_dir_for(self, run: dict) -> Path:
        return self.config.data_dir / "runs" / f"issue-{run['issue_number']}" / f"run-{run['attempt']}"

    def _spawn_detached_job(self, run_id: int, kind: str) -> None:
        """Launch ``agent-desk run-job`` in its own session so it outlives the server."""
        if self.config_path is None:
            raise RuntimeError("detach_jobs requires config_path to spawn run-job processes")
        run = self.store.get_run(run_id)
        run_dir = self._run_dir_for(run)
        run_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            sys.executable,
            "-m",
            "agent_desk",
            "run-job",
            "--config",
            str(self.config_path),
            "--run-id",
            str(run_id),
            "--kind",
            kind,
        ]
        log = (run_dir / "supervisor.log").open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        self.store.update_run(run_id, supervisor_pid=process.pid)

    def run_job(self, run_id: int, kind: str) -> None:
        """Execute one detached job synchronously; reconstructs args from the record.

        This is the body a ``run-job`` process runs. Each ``_run_*`` already
        catches its own exceptions and records a ``failed`` state.
        """
        run = self.store.get_run(run_id)
        if kind == "issue":
            repo = self._repo_for_run(run)
            self._run_worker_for_issue(
                run_id=run_id,
                repo=repo,
                issue_number=int(run["issue_number"]),
                issue_title=str(run["issue_title"]),
                issue_body=str(run.get("issue_body") or ""),
                issue_url=str(run["issue_url"]),
                branch_name=str(run["branch_name"]),
            )
        elif kind == "approve-finish":
            self._run_approve_finish(run_id=run_id)
        elif kind == "auto-finish":
            self._run_auto_finish(run_id=run_id)
        elif kind == "ci-fix":
            repo = self._repo_for_run(run)
            pr_status = self.github.pr_checks_status(repo.name, str(run["pr_url"]))
            self._run_ci_fix(run_id=run_id, pr_status=pr_status, attempt=int(run.get("ci_fix_attempts") or 1))
        else:
            raise ValueError(f"unknown job kind: {kind}")

    def reconcile_orphans(self) -> list[int]:
        """Fail runs left ``running`` by a supervisor that is no longer alive.

        Called once at server startup, before polling. A run whose supervisor PID
        is still alive (the server restarted but the job kept going) is left
        untouched. Returns the run ids that were failed.
        """
        failed: list[int] = []
        for run in self.store.list_runs({"running"}):
            pid = run.get("supervisor_pid")
            if pid and self._pid_alive(int(pid)):
                continue
            run_id = int(run["id"])
            self.store.update_run(
                run_id,
                state="failed",
                stage="failed",
                last_error="Run orphaned: supervisor not running after server restart",
            )
            self.store.add_event(
                run_id,
                "error",
                "orphan",
                "Marked failed after server restart (no live supervisor)",
                {"supervisor_pid": pid},
            )
            failed.append(run_id)
        return failed

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _repo_for_run(self, run: dict) -> RepoConfig:
        for repo in self.config.repos:
            if repo.name == run["repo_name"]:
                return repo
        raise KeyError(f"repository {run['repo_name']} is not configured")

    def _repo_by_name(self, repo_name: str) -> RepoConfig | None:
        target = (repo_name or "").strip()
        for repo in self.config.repos:
            if repo.name == target:
                return repo
        return None

    def _workspace_key(self, repo: RepoConfig) -> Path:
        return repo.local_path.expanduser().resolve()

    def _settings_for_repo(self, repo: RepoConfig) -> SchedulerSettings:
        key = self._workspace_key(repo)
        settings = self._settings_by_workspace.get(key)
        if settings is None:
            settings = SchedulerSettings.from_repo(repo)
            self._settings_by_workspace[key] = settings
        return settings

    def _repo_for_settings(self, workspace_path: str | Path | None) -> RepoConfig | None:
        if workspace_path is None:
            return self.config.repos[0] if len(self.config.repos) == 1 else None
        key = Path(workspace_path).expanduser().resolve()
        for repo in self.config.repos:
            if self._workspace_key(repo) == key:
                return repo
        raise ValueError(f"workspace is not configured: {key}")

    def _repos_for_auto_start(self, workspace_path: str | Path | None) -> list[RepoConfig]:
        if workspace_path is None:
            return list(self.config.repos)
        return [self._repo_for_settings(workspace_path)]

    def _ready_runs_for_repo(self, repo: RepoConfig) -> list[dict]:
        return [
            run
            for run in self.store.list_runs({"ready"})
            if str(run.get("repo_name") or "") == repo.name
        ]

    def _block_unconfigured_ready_runs(self) -> list[RunNextResult]:
        results = []
        configured = {repo.name for repo in self.config.repos}
        for run in reversed(self.store.list_runs({"ready"})):
            if str(run.get("repo_name") or "") not in configured:
                results.append(self._start_ready_run(int(run["id"])))
        return results

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
                elif pr_status.state == "success" and not self._settings_for_repo(repo).requires_human_review:
                    results.append(self._handle_successful_ci_without_review(run, pr_status))
        return results

    def _handle_successful_ci_without_review(
        self,
        run: dict,
        pr_status: PullRequestChecksStatus,
    ) -> RunNextResult:
        run_id = int(run["id"])
        repo = self._repo_for_run(run)
        if self._settings_for_repo(repo).single_closeout_per_workspace:
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
            result = self.continuation_factory(self.config, self.store).approve_finish(run_id)
            self._start_ci_fix_if_closeout_blocked_by_failed_checks(run_id, result)
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "approve-finish", "Approved closeout failed", {"detail": str(exc)})

    def _run_auto_finish(self, *, run_id: int) -> None:
        try:
            result = self.continuation_factory(self.config, self.store).finish_after_ci_success(run_id)
            self._start_ci_fix_if_closeout_blocked_by_failed_checks(run_id, result)
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "auto-finish", "Automatic closeout failed", {"detail": str(exc)})

    def _start_ci_fix_if_closeout_blocked_by_failed_checks(self, run_id: int, result) -> None:
        if getattr(result, "ok", True):
            return
        run = self.store.get_run(run_id)
        if run["state"] != "blocked":
            return
        pr_url = str(run.get("pr_url") or "")
        if not pr_url:
            return
        repo = self._repo_for_run(run)
        pr_status = self.github.pr_checks_status(repo.name, pr_url)
        self.store.update_run(
            run_id,
            pr_ci_status=pr_status.state,
            pr_ci_summary=pr_status.summary,
            pr_ci_checked_at=utc_now(),
        )
        if pr_status.state == "failure":
            self._handle_failed_ci(self.store.get_run(run_id), pr_status)
