from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any, Callable

from .config import DEFAULT_WORKER_TIMEOUT_SECONDS, AgentDeskConfig, RepoConfig
from .continuation import ContinuationRunner
from .dependencies import Dependency, DependencyGraph, parse_dependency_result, render_dependency_prompt
from .github_client import GitHubClient
from .github_client import PullRequestChecksStatus
from .shutdown import (
    LocalProcessController,
    ProcessController,
    build_run_shutdown_item,
    shutdown_id,
    stop_verified_process_groups,
    write_shutdown_artifacts,
)
from .store import Store, utc_now
from .worker import CommandRunner, Worker, parse_json_object, slugify


MAX_CI_FIX_ATTEMPTS = 3

# Maps an internal dispatch target to the detached job kind run-job understands.
JOB_KIND_BY_TARGET = {
    "_run_worker_for_issue": "issue",
    "_run_request_changes": "request-changes",
    "_run_approve_finish": "approve-finish",
    "_run_auto_finish": "auto-finish",
    "_run_ci_fix": "ci-fix",
    "_run_resume_interrupted": "resume-interrupted",
}
CLOSEOUT_STAGES = {
    "approve-finish queued",
    "approve-finish",
    "auto-finishing after ci success",
    "auto-finish",
}
ISSUE_REFERENCE_RE = re.compile(r"(?<![\w/#])#(\d+)\b")
DEPENDENCY_WAITING_STATE = "waiting_dependencies"
DEPENDENCY_WAITING_STAGE = "waiting for dependencies"


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
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS

    @classmethod
    def from_repo(
        cls,
        repo: RepoConfig,
        *,
        worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS,
    ) -> "SchedulerSettings":
        return cls(
            auto_start_ready=repo.auto_start_ready,
            max_concurrent_runs=max(1, repo.max_concurrent_runs),
            requires_human_review=repo.requires_human_review,
            single_closeout_per_workspace=repo.single_closeout_per_workspace,
            worker_timeout_seconds=max(60, int(worker_timeout_seconds)),
        )

    def as_payload(self) -> dict[str, bool | int]:
        return {
            "auto_start_ready": self.auto_start_ready,
            "max_concurrent_runs": self.max_concurrent_runs,
            "requires_human_review": self.requires_human_review,
            "single_closeout_per_workspace": self.single_closeout_per_workspace,
            "worker_timeout_seconds": self.worker_timeout_seconds,
        }


class Scheduler:
    def __init__(
        self,
        config: AgentDeskConfig,
        store: Store,
        github: GitHubClient | None = None,
        worker: Worker | None = None,
        continuation_factory: Callable[[AgentDeskConfig, Store], ContinuationRunner] | None = None,
        dependency_extractor: Callable[[str, list[dict[str, Any]]], DependencyGraph] | None = None,
        config_path: Path | None = None,
        detach_jobs: bool = False,
    ):
        self.config = config
        self.store = store
        self.github = github or GitHubClient()
        self.worker = worker or Worker(config, store)
        self.continuation_factory = continuation_factory or (lambda config, store: ContinuationRunner(config, store))
        self.dependency_runner = getattr(self.worker, "runner", CommandRunner())
        self.dependency_extractor = dependency_extractor or self._extract_dependencies_with_codex
        # When True (server + run-job child), jobs run as detached processes so a
        # server restart cannot kill them; otherwise they run in a daemon thread.
        self.config_path = Path(config_path) if config_path else None
        self.detach_jobs = detach_jobs
        self._paused = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._settings_by_workspace = {
            self._workspace_key(repo): SchedulerSettings.from_repo(
                repo,
                worker_timeout_seconds=config.worker_timeout_seconds,
            )
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

    def shutdown_preview(self, controller: ProcessController | None = None) -> dict[str, Any]:
        process_controller = controller or LocalProcessController()
        runs = self.store.list_runs({"running"})
        items = [build_run_shutdown_item(run, process_controller) for run in runs]
        return {
            "shutdown_id": shutdown_id(),
            "running_count": len(items),
            "runs": items,
        }

    def shutdown_all(
        self,
        controller: ProcessController | None = None,
        *,
        dashboard_pid: int | None = None,
        grace_seconds: float = 3.0,
    ) -> dict[str, Any]:
        process_controller = controller or LocalProcessController()
        runs = self.store.list_runs({"running"})
        items = [build_run_shutdown_item(run, process_controller) for run in runs]
        sid = shutdown_id()
        manifest = write_shutdown_artifacts(
            config=self.config,
            shutdown_id=sid,
            items=items,
            dashboard_pid=dashboard_pid or os.getpid(),
            config_path=self.config_path,
            extra_fields={"status": "recorded"},
        )
        for run, item in zip(runs, items, strict=False):
            run_id = int(run["id"])
            fields = {
                "state": "interrupted",
                "stage": "interrupted by shutdown",
                "last_error": "Interrupted by user shutdown; resume from dashboard",
                "shutdown_id": sid,
                "shutdown_manifest": item.get("shutdown_manifest", ""),
                "shutdown_resume_note": item.get("shutdown_resume_note", ""),
            }
            if item.get("codex_thread_id"):
                fields["codex_thread_id"] = item["codex_thread_id"]
            self.store.update_run(run_id, **fields)
            self.store.add_event(
                run_id,
                "warning",
                "shutdown-interrupted",
                "Run interrupted by dashboard shutdown",
                {
                    "shutdown_id": sid,
                    "manifest_path": manifest["manifest_path"],
                    "killable": item.get("killable", False),
                },
            )
        signal_results = stop_verified_process_groups(
            items, process_controller, grace_seconds=grace_seconds
        )
        return write_shutdown_artifacts(
            config=self.config,
            shutdown_id=sid,
            items=items,
            dashboard_pid=dashboard_pid or os.getpid(),
            config_path=self.config_path,
            extra_fields={"status": "signaled", "signal_results": signal_results},
        )

    def interrupt_run(
        self,
        run_id: int,
        controller: ProcessController | None = None,
        *,
        grace_seconds: float = 3.0,
    ) -> RunNextResult:
        process_controller = controller or LocalProcessController()
        with self._lock:
            run = self.store.get_run(run_id)
            if run["state"] != "running":
                return RunNextResult(False, f"Run #{run_id} is not running", run_id)
            item = build_run_shutdown_item(run, process_controller)
            if not item.get("killable"):
                reason = "; ".join(item.get("warnings") or []) or "run supervisor is not verified"
                return RunNextResult(False, f"Run #{run_id} cannot be safely interrupted: {reason}", run_id)
            sid = shutdown_id()
            manifest = write_shutdown_artifacts(
                config=self.config,
                shutdown_id=sid,
                items=[item],
                dashboard_pid=os.getpid(),
                config_path=self.config_path,
                extra_fields={"status": "manual-interrupt-recorded"},
            )
            fields = {
                "state": "interrupted",
                "stage": "interrupted by user",
                "last_error": "Interrupted by user; resume from dashboard",
                "shutdown_id": sid,
                "shutdown_manifest": item.get("shutdown_manifest", ""),
                "shutdown_resume_note": item.get("shutdown_resume_note", ""),
            }
            if item.get("codex_thread_id"):
                fields["codex_thread_id"] = item["codex_thread_id"]
            self.store.update_run(run_id, **fields)
            self.store.add_event(
                run_id,
                "warning",
                "user-interrupted",
                "Run interrupted by user",
                {
                    "shutdown_id": sid,
                    "manifest_path": manifest["manifest_path"],
                    "killable": item.get("killable", False),
                },
            )
        signal_results = stop_verified_process_groups(
            [item], process_controller, grace_seconds=grace_seconds
        )
        write_shutdown_artifacts(
            config=self.config,
            shutdown_id=sid,
            items=[item],
            dashboard_pid=os.getpid(),
            config_path=self.config_path,
            extra_fields={"status": "manual-interrupt-signaled", "signal_results": signal_results},
        )
        return RunNextResult(True, "Run interrupted", run_id)

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
        worker_timeout_seconds: int | None = None,
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
            if worker_timeout_seconds is not None:
                value = int(worker_timeout_seconds)
                if value < 60:
                    raise ValueError("worker_timeout_seconds must be at least 60")
                settings.worker_timeout_seconds = value
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
        results.extend(self.unlock_ready_dependencies())
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
                existing = self.store.get_record(repo.name, number)
                fields = {
                    "issue_title": str(issue.get("title") or ""),
                    "issue_url": str(issue.get("url") or ""),
                    "issue_body": str(issue.get("body") or ""),
                }
                if existing is None:
                    self.store.create_available(
                        repo_name=repo.name,
                        issue_number=number,
                        issue_title=fields["issue_title"],
                        issue_url=fields["issue_url"],
                        issue_body=fields["issue_body"],
                    )
                elif self._can_refresh_synced_issue_metadata(existing):
                    updates = {
                        key: value
                        for key, value in fields.items()
                        if str(existing.get(key) or "") != value
                    }
                    if updates:
                        self.store.update_run(existing["id"], **updates)
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
                "state": str(record.get("state") or ""),
                "on_desk": record["state"] != "available",
                "removable": self._can_remove_from_desk(record),
                "dependency_state": str(record.get("dependency_state") or ""),
                "blocked_by": record.get("blocked_by") or [],
                "dependencies": record.get("dependencies") or [],
            }
            for record in self.store.list_records(repo.name)
        ]

    def mark_issue_ready(self, repo_name: str, issue_number: int) -> RunNextResult:
        """Move a synced issue onto the desk (available -> ready) on disk.

        Desk state is folder-driven, so this is a pure local file move with no
        GitHub label mutation.
        """
        return self.mark_issues_ready(repo_name, [issue_number], dependency_mode="direct")[0]

    def remove_issue_from_desk(self, repo_name: str, issue_number: int) -> RunNextResult:
        repo = self._repo_by_name(repo_name)
        if repo is None:
            return RunNextResult(False, f"{repo_name} is not a configured repository")
        with self._lock:
            record = self.store.get_record(repo.name, issue_number)
            if record is None:
                return RunNextResult(False, f"{repo.name}#{issue_number} is not synced")
            if record["state"] == "available":
                return RunNextResult(True, f"{repo.name}#{issue_number} is already off the desk", record["id"])
            if not self._can_remove_from_desk(record):
                return RunNextResult(
                    False,
                    f"{repo.name}#{issue_number} is {record['state']} and cannot be removed from the desk",
                    record["id"],
                )
            fields: dict[str, Any] = {
                "state": "available",
                "stage": "",
                "branch_name": "",
                "dependencies": [],
                "blocked_by": [],
                "dependency_state": "",
                "last_error": "",
                "ended_at": "",
            }
            if record["state"] in {"failed", "interrupted"}:
                fields.update(
                    {
                        "attempt": int(record.get("attempt") or 1) + 1,
                        "run_dir": "",
                        "worktree_path": "",
                        "codex_thread_id": "",
                        "pr_url": "",
                        "pr_ci_status": "",
                        "pr_ci_summary": "",
                        "pr_ci_checked_at": "",
                        "ci_fix_attempts": 0,
                        "ci_fix_last_sha": "",
                        "request_changes_feedback": "",
                        "supervisor_pid": "",
                        "shutdown_id": "",
                        "shutdown_manifest": "",
                        "shutdown_resume_note": "",
                    }
                )
            self.store.update_run(record["id"], **fields)
            self.store.add_event(
                record["id"],
                "info",
                "removed",
                "Issue was removed from the desk",
                {"repo": repo.name},
            )
            return RunNextResult(True, f"Removed {repo.name}#{issue_number} from the desk", record["id"])

    def mark_issues_ready(
        self,
        repo_name: str,
        issue_numbers: list[int],
        *,
        dependency_mode: str = "analyze",
    ) -> list[RunNextResult]:
        numbers = []
        for raw_number in issue_numbers:
            number = int(raw_number)
            if number > 0 and number not in numbers:
                numbers.append(number)
        if not numbers:
            return []
        repo = self._repo_by_name(repo_name)
        if repo is None:
            return [RunNextResult(False, f"{repo_name} is not a configured repository")]
        if dependency_mode == "direct":
            with self._lock:
                return [self._mark_issue_ready_direct(repo, number) for number in numbers]
        if dependency_mode != "analyze":
            raise ValueError(f"unknown dependency mode: {dependency_mode}")
        issues = [self._issue_for_dependency_extraction(repo, number) for number in numbers]
        try:
            graph = self.dependency_extractor(repo.name, issues)
        except Exception as error:
            with self._lock:
                return [
                    self._mark_issue_blocked(
                        repo,
                        number,
                        issues[index],
                        dependencies=[],
                        blocked_by=[{"repo": repo.name, "number": number, "state": "unknown"}],
                        dependency_state="unknown",
                        reason=f"dependency analysis failed: {error}",
                    )
                    for index, number in enumerate(numbers)
                ]
        deps_by_issue = {issue.number: issue.depends_on for issue in graph.issues}
        with self._lock:
            results = []
            for index, number in enumerate(numbers):
                deps = deps_by_issue.get(number, [])
                blocked_by = self._unsatisfied_dependencies(repo, deps)
                if blocked_by:
                    result = self._mark_issue_blocked(
                        repo,
                        number,
                        issues[index],
                        dependencies=deps,
                        blocked_by=blocked_by,
                        dependency_state="blocked",
                        reason="waiting for dependencies",
                    )
                else:
                    result = self._mark_issue_ready_direct(
                        repo,
                        number,
                        issue=issues[index],
                        dependencies=deps,
                        dependency_state="ready",
                    )
                results.append(result)
            return results

    def unlock_ready_dependencies(self) -> list[RunNextResult]:
        results: list[RunNextResult] = []
        with self._lock:
            for record in reversed(self.store.list_runs({DEPENDENCY_WAITING_STATE, "blocked"})):
                if str(record.get("dependency_state") or "") != "blocked":
                    continue
                if not self._is_dependency_waiting(record):
                    continue
                try:
                    repo = self._repo_for_run(record)
                except KeyError:
                    continue
                deps = [
                    Dependency(
                        repo=str(dep.get("repo") or repo.name),
                        number=int(dep.get("number") or 0),
                        evidence=str(dep.get("evidence") or ""),
                        confidence=str(dep.get("confidence") or ""),
                    )
                    for dep in record.get("dependencies", [])
                    if int(dep.get("number") or 0) > 0
                ]
                blocked_by = self._unsatisfied_dependencies(
                    repo, deps, overrides=record.get("dependency_overrides") or []
                )
                if blocked_by:
                    self.store.update_run(record["id"], blocked_by=blocked_by)
                    continue
                run_id = self._promote_to_ready(
                    repo,
                    record,
                    dependencies=[dep.as_payload() for dep in deps],
                    blocked_by=[],
                    dependency_state="ready",
                )
                results.append(RunNextResult(True, f"Unlocked {repo.name}#{record['issue_number']}", run_id))
        return results

    def _mark_issue_ready_direct(
        self,
        repo: RepoConfig,
        issue_number: int,
        *,
        issue: dict | None = None,
        dependencies: list[Dependency] | None = None,
        dependency_state: str = "ready",
    ) -> RunNextResult:
        open_run = self.store.find_open_run(repo.name, issue_number)
        if open_run is not None:
            return RunNextResult(True, f"{repo.name}#{issue_number} is already on the desk", open_run["id"])
        record = self.store.get_record(repo.name, issue_number)
        payload = {
            "dependencies": [dep.as_payload() for dep in dependencies or []],
            "blocked_by": [],
            "dependency_state": dependency_state,
        }
        if record is not None and record["state"] in {"available", "blocked", DEPENDENCY_WAITING_STATE}:
            run_id = self._promote_to_ready(repo, record, **payload)
        else:
            issue = issue or record or self._fetch_issue(repo, issue_number)
            run_id = self._create_ready_run(repo, issue_number, issue, **payload)
        return RunNextResult(True, f"Added {repo.name}#{issue_number} to the desk", run_id)

    def _mark_issue_blocked(
        self,
        repo: RepoConfig,
        issue_number: int,
        issue: dict,
        *,
        dependencies: list[Dependency],
        blocked_by: list[dict[str, Any]],
        dependency_state: str,
        reason: str,
    ) -> RunNextResult:
        record = self.store.get_record(repo.name, issue_number)
        payload = {
            "dependencies": [dep.as_payload() for dep in dependencies],
            "blocked_by": blocked_by,
            "dependency_state": dependency_state,
            "last_error": reason,
        }
        if record is not None and record["state"] in {"available", "blocked", DEPENDENCY_WAITING_STATE}:
            run_id = self._promote_to_dependency_waiting(repo, record, **payload)
        else:
            run_id = self._create_dependency_waiting_run(repo, issue_number, issue, **payload)
        return RunNextResult(False, f"{repo.name}#{issue_number} is waiting for dependencies", run_id)

    def mark_dependency_satisfied(
        self,
        repo_name: str,
        issue_number: int,
        dependency_repo: str,
        dependency_number: int,
        *,
        reason: str = "manual override",
    ) -> RunNextResult:
        return self._set_dependency_override(
            repo_name,
            issue_number,
            dependency_repo,
            dependency_number,
            satisfied=True,
            reason=reason,
        )

    def clear_dependency_override(
        self,
        repo_name: str,
        issue_number: int,
        dependency_repo: str,
        dependency_number: int,
    ) -> RunNextResult:
        return self._set_dependency_override(
            repo_name,
            issue_number,
            dependency_repo,
            dependency_number,
            satisfied=False,
            reason="",
        )

    def add_dependency_edge(
        self,
        repo_name: str,
        issue_number: int,
        dependency_repo: str,
        dependency_number: int,
        *,
        evidence: str = "manual dependency repair",
    ) -> RunNextResult:
        repo = self._repo_by_name(repo_name)
        if repo is None:
            return RunNextResult(False, f"{repo_name} is not a configured repository")
        dependency_repo = str(dependency_repo or repo.name).strip()
        dep_number = int(dependency_number)
        if not dependency_repo:
            return RunNextResult(False, "dependency repo is required")
        if dep_number <= 0:
            return RunNextResult(False, "dependency issue must be a positive number")
        if dependency_repo == repo.name and dep_number == int(issue_number):
            return RunNextResult(False, "issue cannot depend on itself")
        with self._lock:
            record = self.store.get_record(repo.name, int(issue_number))
            if record is None or record["state"] == "available":
                return RunNextResult(False, f"{repo.name}#{issue_number} is not on the desk")
            if not self._can_repair_dependencies(record):
                return RunNextResult(
                    False,
                    f"{repo.name}#{issue_number} is {record['state']} and cannot repair dependencies",
                    record["id"],
                )
            dependencies = self._dependencies_from_record(repo, record)
            if not any(dep.repo == dependency_repo and dep.number == dep_number for dep in dependencies):
                dependencies.append(
                    Dependency(
                        repo=dependency_repo,
                        number=dep_number,
                        evidence=evidence or "manual dependency repair",
                        confidence="manual",
                    )
                )
            overrides = self._dependency_overrides_without(record, repo.name, dependency_repo, dep_number)
            return self._apply_dependency_overrides(repo, record, dependencies, overrides)

    def remove_dependency_edge(
        self,
        repo_name: str,
        issue_number: int,
        dependency_repo: str,
        dependency_number: int,
    ) -> RunNextResult:
        repo = self._repo_by_name(repo_name)
        if repo is None:
            return RunNextResult(False, f"{repo_name} is not a configured repository")
        dependency_repo = str(dependency_repo or repo.name).strip()
        dep_number = int(dependency_number)
        if dep_number <= 0:
            return RunNextResult(False, "dependency issue must be a positive number")
        with self._lock:
            record = self.store.get_record(repo.name, int(issue_number))
            if record is None or record["state"] == "available":
                return RunNextResult(False, f"{repo.name}#{issue_number} is not on the desk")
            if not self._can_repair_dependencies(record):
                return RunNextResult(
                    False,
                    f"{repo.name}#{issue_number} is {record['state']} and cannot repair dependencies",
                    record["id"],
                )
            current = self._dependencies_from_record(repo, record)
            dependencies = [
                dep for dep in current if not (dep.repo == dependency_repo and dep.number == dep_number)
            ]
            if len(dependencies) == len(current):
                return RunNextResult(
                    False,
                    f"{repo.name}#{issue_number} does not depend on {dependency_repo}#{dep_number}",
                    record["id"],
                )
            overrides = self._dependency_overrides_without(record, repo.name, dependency_repo, dep_number)
            return self._apply_dependency_overrides(repo, record, dependencies, overrides)

    def _set_dependency_override(
        self,
        repo_name: str,
        issue_number: int,
        dependency_repo: str,
        dependency_number: int,
        *,
        satisfied: bool,
        reason: str,
    ) -> RunNextResult:
        repo = self._repo_by_name(repo_name)
        if repo is None:
            return RunNextResult(False, f"{repo_name} is not a configured repository")
        dep_number = int(dependency_number)
        if dep_number <= 0:
            return RunNextResult(False, "dependency issue must be a positive number")
        with self._lock:
            record = self.store.get_record(repo.name, int(issue_number))
            if record is None:
                return RunNextResult(False, f"{repo.name}#{issue_number} is not on the desk")
            dependencies = self._dependencies_from_record(repo, record)
            if not any(dep.repo == dependency_repo and dep.number == dep_number for dep in dependencies):
                return RunNextResult(False, f"{repo.name}#{issue_number} does not depend on {dependency_repo}#{dep_number}", record["id"])
            overrides = self._dependency_overrides(record)
            key = (dependency_repo, dep_number)
            overrides = [
                override
                for override in overrides
                if (str(override.get("repo") or repo.name), int(override.get("number") or 0)) != key
            ]
            if satisfied:
                overrides.append(
                    {
                        "repo": dependency_repo,
                        "number": dep_number,
                        "state": "satisfied",
                        "reason": reason or "manual override",
                    }
                )
            return self._apply_dependency_overrides(repo, record, dependencies, overrides)

    def _apply_dependency_overrides(
        self,
        repo: RepoConfig,
        record: dict[str, Any],
        dependencies: list[Dependency],
        overrides: list[dict[str, Any]],
    ) -> RunNextResult:
        blocked_by = self._unsatisfied_dependencies(repo, dependencies, overrides=overrides)
        if blocked_by:
            self.store.update_run(
                record["id"],
                state=DEPENDENCY_WAITING_STATE,
                stage=DEPENDENCY_WAITING_STAGE,
                dependencies=[dep.as_payload() for dep in dependencies],
                blocked_by=blocked_by,
                dependency_state="blocked",
                dependency_overrides=overrides,
                last_error="waiting for dependencies",
            )
            self.store.add_event(
                record["id"],
                "info",
                "dependencies",
                "Dependency override updated; issue is still waiting for dependencies",
                {"blocked_by": blocked_by, "dependency_overrides": overrides},
            )
            return RunNextResult(False, f"{repo.name}#{record['issue_number']} is still waiting for dependencies", record["id"])
        run_id = self._promote_to_ready(
            repo,
            record,
            dependencies=[dep.as_payload() for dep in dependencies],
            blocked_by=[],
            dependency_state="ready",
            dependency_overrides=overrides,
            last_error="",
        )
        self.store.add_event(
            run_id,
            "info",
            "dependencies",
            "Dependency override updated; issue is ready",
            {"dependency_overrides": overrides},
        )
        return RunNextResult(True, f"Unlocked {repo.name}#{record['issue_number']}", run_id)

    def _issue_for_dependency_extraction(self, repo: RepoConfig, issue_number: int) -> dict:
        record = self.store.get_record(repo.name, issue_number)
        if record is not None:
            return {
                "number": int(record["issue_number"]),
                "title": str(record.get("issue_title") or ""),
                "body": str(record.get("issue_body") or ""),
                "url": str(record.get("issue_url") or ""),
            }
        issue = self._fetch_issue(repo, issue_number)
        return {
            "number": issue_number,
            "title": str(issue.get("title") or issue.get("issue_title") or ""),
            "body": str(issue.get("body") or issue.get("issue_body") or ""),
            "url": str(issue.get("url") or issue.get("issue_url") or ""),
        }

    def _dependencies_from_record(self, repo: RepoConfig, record: dict[str, Any]) -> list[Dependency]:
        return [
            Dependency(
                repo=str(dep.get("repo") or repo.name),
                number=int(dep.get("number") or 0),
                evidence=str(dep.get("evidence") or ""),
                confidence=str(dep.get("confidence") or ""),
            )
            for dep in record.get("dependencies", [])
            if int(dep.get("number") or 0) > 0
        ]

    @staticmethod
    def _dependency_overrides(record: dict[str, Any]) -> list[dict[str, Any]]:
        overrides = []
        for override in record.get("dependency_overrides") or []:
            if not isinstance(override, dict):
                continue
            number = int(override.get("number") or 0)
            if number <= 0:
                continue
            overrides.append(
                {
                    "repo": str(override.get("repo") or record.get("repo_name") or ""),
                    "number": number,
                    "state": str(override.get("state") or ""),
                    "reason": str(override.get("reason") or ""),
                }
            )
        return overrides

    @classmethod
    def _dependency_overrides_without(
        cls,
        record: dict[str, Any],
        default_repo: str,
        dependency_repo: str,
        dependency_number: int,
    ) -> list[dict[str, Any]]:
        key = (dependency_repo, int(dependency_number))
        return [
            override
            for override in cls._dependency_overrides(record)
            if (str(override.get("repo") or default_repo), int(override.get("number") or 0)) != key
        ]

    @staticmethod
    def _dependency_override_satisfied(
        dep: Dependency, overrides: list[dict[str, Any]] | None
    ) -> bool:
        for override in overrides or []:
            if str(override.get("state") or "") != "satisfied":
                continue
            if str(override.get("repo") or "") == dep.repo and int(override.get("number") or 0) == dep.number:
                return True
        return False

    def _unsatisfied_dependencies(
        self,
        repo: RepoConfig,
        dependencies: list[Dependency],
        *,
        overrides: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        blocked_by = []
        for dep in dependencies:
            if dep.number <= 0:
                continue
            if self._dependency_override_satisfied(dep, overrides):
                continue
            if dep.repo != repo.name:
                blocked_by.append({"repo": dep.repo, "number": dep.number, "state": "unknown"})
                continue
            record = self.store.get_record(dep.repo, dep.number)
            state = self._known_issue_state(repo, dep.number, record=record)
            if self._issue_state_is_satisfied(state):
                continue
            blocked_by.append(
                {
                    "repo": dep.repo,
                    "number": dep.number,
                    "state": str(state.get("local_state") or state.get("github_state") or "unknown"),
                }
            )
        return blocked_by

    @staticmethod
    def _can_remove_from_desk(record: dict[str, Any]) -> bool:
        if record["state"] == "ready":
            return True
        if record["state"] in {"failed", "interrupted"}:
            return True
        return Scheduler._is_dependency_waiting(record)

    @staticmethod
    def _is_dependency_waiting(record: dict[str, Any]) -> bool:
        if record["state"] == DEPENDENCY_WAITING_STATE:
            return True
        return record["state"] == "blocked" and record.get("stage") == DEPENDENCY_WAITING_STAGE

    @staticmethod
    def _can_repair_dependencies(record: dict[str, Any]) -> bool:
        return record["state"] == "ready" or Scheduler._is_dependency_waiting(record)

    @staticmethod
    def _can_refresh_synced_issue_metadata(record: dict[str, Any]) -> bool:
        return record["state"] in {
            "available",
            "ready",
            DEPENDENCY_WAITING_STATE,
            "blocked",
            "failed",
            "interrupted",
        }

    def _extract_dependencies_with_codex(self, repo_name: str, issues: list[dict[str, Any]]) -> DependencyGraph:
        repo = self._repo_by_name(repo_name)
        known_issue_states = self._known_issue_states_for_dependency_analysis(repo, issues) if repo else []
        prompt = render_dependency_prompt(repo_name, issues, known_issue_states=known_issue_states)
        result_dir = self.config.data_dir / "dependency-extraction"
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / f"deps-{os.getpid()}-{threading.get_ident()}.json"
        completed = self.dependency_runner.run(
            [
                "codex",
                "--ask-for-approval",
                "never",
                "--sandbox",
                "workspace-write",
                "exec",
                "--json",
                "--output-last-message",
                str(result_path),
                "-",
            ],
            cwd=Path.cwd(),
            stdin=prompt,
            timeout=(
                self._config_for_repo(repo).worker_timeout_seconds
                if repo is not None
                else self.config.worker_timeout_seconds
            ),
            idle_timeout=self.config.worker_idle_timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "codex dependency extraction failed")
        output = result_path.read_text(encoding="utf-8") if result_path.exists() else completed.stdout
        payload = parse_json_object(output)
        if payload is None:
            raise ValueError("codex dependency extraction returned no JSON")
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return parse_dependency_result(text, default_repo=repo_name)

    def _known_issue_states_for_dependency_analysis(
        self, repo: RepoConfig, issues: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        numbers = sorted(
            {
                int(match.group(1))
                for issue in issues
                for field in (issue.get("title"), issue.get("issue_title"), issue.get("body"), issue.get("issue_body"))
                for match in ISSUE_REFERENCE_RE.finditer(str(field or ""))
                if int(match.group(1)) > 0
            }
        )
        return [self._known_issue_state(repo, number) for number in numbers]

    def _known_issue_state(
        self, repo: RepoConfig, issue_number: int, *, record: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        record = record if record is not None else self.store.get_record(repo.name, issue_number)
        state: dict[str, Any] = {
            "repo": repo.name,
            "number": int(issue_number),
            "local_state": str(record.get("state") or "") if record else "",
            "github_state": "",
            "state_reason": "",
            "closed_at": "",
        }
        try:
            issue = self.github.get_issue(repo.name, issue_number)
        except RuntimeError:
            issue = {}
        state.update(
            {
                "github_state": str(issue.get("state") or ""),
                "state_reason": str(issue.get("stateReason") or issue.get("state_reason") or ""),
                "closed_at": str(issue.get("closedAt") or issue.get("closed_at") or ""),
            }
        )
        return state

    @staticmethod
    def _issue_state_is_satisfied(state: dict[str, Any]) -> bool:
        local_state = str(state.get("local_state") or "").lower()
        github_state = str(state.get("github_state") or "").lower()
        state_reason = str(state.get("state_reason") or "").lower()
        return local_state == "done" or (github_state == "closed" and state_reason == "completed")

    def _fetch_issue(self, repo: RepoConfig, issue_number: int) -> dict:
        try:
            return self.github.get_issue(repo.name, issue_number)
        except RuntimeError:
            return {"number": issue_number}

    def _promote_to_ready(self, repo: RepoConfig, record: dict, **fields: Any) -> int:
        number = int(record["issue_number"])
        title = str(record.get("issue_title") or f"Issue {number}")
        branch = f"agent/issue-{number}-{slugify(title)[:48]}-run-{int(record.get('attempt', 1))}"
        self.store.update_run(record["id"], state="ready", stage="waiting for human run", branch_name=branch, **fields)
        self.store.add_event(record["id"], "info", "ready", "Issue is ready to run", {"repo": repo.name})
        return record["id"]

    def _promote_to_dependency_waiting(self, repo: RepoConfig, record: dict, **fields: Any) -> int:
        number = int(record["issue_number"])
        title = str(record.get("issue_title") or f"Issue {number}")
        branch = f"agent/issue-{number}-{slugify(title)[:48]}-run-{int(record.get('attempt', 1))}"
        self.store.update_run(
            record["id"],
            state=DEPENDENCY_WAITING_STATE,
            stage=DEPENDENCY_WAITING_STAGE,
            branch_name=branch,
            **fields,
        )
        self.store.add_event(record["id"], "info", "dependencies", "Issue is waiting for dependencies", fields)
        return record["id"]

    def _create_ready_run(self, repo: RepoConfig, issue_number: int, issue: dict, **fields: Any) -> int:
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
        self.store.update_run(run_id, state="ready", stage="waiting for human run", **fields)
        self.store.add_event(run_id, "info", "ready", "Issue is ready to run", {"repo": repo.name})
        return run_id

    def _create_dependency_waiting_run(self, repo: RepoConfig, issue_number: int, issue: dict, **fields: Any) -> int:
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
        self.store.update_run(run_id, state=DEPENDENCY_WAITING_STATE, stage=DEPENDENCY_WAITING_STAGE, **fields)
        self.store.add_event(run_id, "info", "dependencies", "Issue is waiting for dependencies", fields)
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
            return RunNextResult(False, "No ready issues found")

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

    def request_changes(self, run_id: int, feedback: str) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused", run_id)
            run = self.store.get_run(run_id)
            if run["state"] != "pr_open":
                return RunNextResult(False, f"Run #{run_id} is not open for review feedback", run_id)
            feedback = str(feedback or "").strip()
            if not feedback:
                return RunNextResult(False, "feedback is required", run_id)
            self.store.update_run(
                run_id,
                state="running",
                stage="request-changes queued",
                request_changes_feedback=feedback,
                last_error="",
            )
            self.store.add_event(run_id, "info", "request-changes", "Starting request changes", {})
            self._start_daemon_thread(self._run_request_changes, {"run_id": run_id})
            return RunNextResult(True, "Request changes started", run_id)

    def resume_interrupted(self, run_id: int) -> RunNextResult:
        with self._lock:
            if self._paused:
                return RunNextResult(False, "Scheduler is paused", run_id)
            run = self.store.get_run(run_id)
            if run["state"] != "interrupted":
                return RunNextResult(False, f"Run #{run_id} is not interrupted", run_id)
            if not str(run.get("codex_thread_id") or ""):
                return RunNextResult(False, "resume requires codex_thread_id", run_id)
            if not str(run.get("worktree_path") or ""):
                return RunNextResult(False, "resume requires worktree_path", run_id)
            self.store.update_run(
                run_id,
                state="running",
                stage="resume-interrupted queued",
                last_error="",
                ended_at="",
                supervisor_pid="",
            )
            self.store.add_event(
                run_id,
                "info",
                "resume-interrupted",
                "Starting interrupted run resume",
                {},
            )
            self._start_daemon_thread(self._run_resume_interrupted, {"run_id": run_id})
            return RunNextResult(True, "Resume interrupted started", run_id)

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
        try:
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
        except Exception as exc:
            message = f"Failed to start supervisor for issue #{issue_number}: {exc}"
            self.store.update_run(run_id, state="failed", stage="failed", last_error=message, supervisor_pid="")
            self.store.add_event(run_id, "error", "spawn-failed", message, {"repo": repo.name})
            return RunNextResult(False, message, run_id)
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
        self.store.update_run(run_id, run_dir=str(run_dir))
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
        elif kind == "request-changes":
            self._run_request_changes(
                run_id=run_id,
                feedback=str(run.get("request_changes_feedback") or ""),
            )
        elif kind == "approve-finish":
            self._run_approve_finish(run_id=run_id)
        elif kind == "auto-finish":
            self._run_auto_finish(run_id=run_id)
        elif kind == "ci-fix":
            repo = self._repo_for_run(run)
            pr_status = self.github.pr_checks_status(repo.name, str(run["pr_url"]))
            self._run_ci_fix(run_id=run_id, pr_status=pr_status, attempt=int(run.get("ci_fix_attempts") or 1))
        elif kind == "resume-interrupted":
            self._run_resume_interrupted(run_id=run_id)
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
            settings = SchedulerSettings.from_repo(
                repo,
                worker_timeout_seconds=self.config.worker_timeout_seconds,
            )
            self._settings_by_workspace[key] = settings
        return settings

    def _config_for_repo(self, repo: RepoConfig) -> AgentDeskConfig:
        timeout = self._settings_for_repo(repo).worker_timeout_seconds
        if timeout == self.config.worker_timeout_seconds:
            return self.config
        return replace(self.config, worker_timeout_seconds=timeout)

    def _config_for_run_id(self, run_id: int) -> AgentDeskConfig:
        try:
            repo = self._repo_for_run(self.store.get_run(run_id))
        except KeyError:
            return self.config
        return self._config_for_repo(repo)

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
        if hasattr(self.worker, "config"):
            self.worker.config = self._config_for_repo(repo)
        self.worker.run_issue(
            run_id=run_id,
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_url=issue_url,
            branch_name=branch_name,
        )

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
            self.continuation_factory(self._config_for_run_id(run_id), self.store).fix_ci(
                run_id,
                pr_status,
                attempt=attempt,
                max_attempts=MAX_CI_FIX_ATTEMPTS,
            )
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "fix-ci", "Automatic CI fix failed", {"detail": str(exc)})

    def _run_request_changes(self, *, run_id: int, feedback: str | None = None) -> None:
        try:
            if feedback is None:
                run = self.store.get_run(run_id)
                feedback = str(run.get("request_changes_feedback") or "")
            if not str(feedback).strip():
                message = "request-changes requires feedback"
                self.store.update_run(run_id, state="blocked", stage="blocked", last_error=message)
                self.store.add_event(run_id, "error", "request-changes", message, {})
                return
            self.continuation_factory(self._config_for_run_id(run_id), self.store).request_changes(
                run_id, str(feedback)
            )
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "request-changes", "Request changes failed", {"detail": str(exc)})

    def _run_resume_interrupted(self, *, run_id: int) -> None:
        try:
            result = self.continuation_factory(
                self._config_for_run_id(run_id), self.store
            ).resume_interrupted(run_id)
            self._start_ci_fix_if_closeout_blocked_by_failed_checks(run_id, result)
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(
                run_id,
                "error",
                "resume-interrupted",
                "Interrupted run resume failed",
                {"detail": str(exc)},
            )

    def _run_approve_finish(self, *, run_id: int) -> None:
        try:
            result = self.continuation_factory(
                self._config_for_run_id(run_id), self.store
            ).approve_finish(run_id)
            self._start_ci_fix_if_closeout_blocked_by_failed_checks(run_id, result)
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "approve-finish", "Approved closeout failed", {"detail": str(exc)})

    def _run_auto_finish(self, *, run_id: int) -> None:
        try:
            result = self.continuation_factory(
                self._config_for_run_id(run_id), self.store
            ).finish_after_ci_success(run_id)
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
