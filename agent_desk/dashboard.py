from __future__ import annotations

import errno
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .config import add_project_to_config, add_remote_repo_to_config, load_config
from .scheduler import Scheduler
from .store import Store
from .worker import extract_thread_id, format_resume_command


RUN_DISPLAY_ORDER = {
    "running": 0,
    "interrupted": 1,
    "pr_open": 2,
    "needs_review": 3,
    "failed": 4,
    "ready": 5,
    "waiting_dependencies": 6,
    "blocked": 7,
    "done": 8,
}


def build_state_payload(
    store: Store, scheduler: Scheduler | None = None
) -> dict[str, Any]:
    payload = store.dashboard_state()
    repo_paths = {}
    projects = []
    if scheduler:
        for repo in scheduler.config.repos:
            project = {
                "name": repo.name,
                "path": str(repo.local_path),
                "settings": scheduler.settings_payload(repo.local_path),
            }
            projects.append(project)
            repo_paths[repo.name] = project
    for run in payload["runs"]:
        run_dir = Path(run.get("run_dir") or "")
        run["log_files"] = available_log_files(run_dir)
        thread_id = run.get("codex_thread_id") or extract_thread_id_from_run_dir(
            run_dir
        )
        run["codex_thread_id"] = thread_id
        run["resume_command"] = format_resume_command(
            thread_id, str(run.get("worktree_path") or "")
        )
        enrich_resume_fields(run)
        project = repo_paths.get(str(run.get("repo_name") or ""), {})
        run["project_name"] = project.get("name", run.get("repo_name") or "")
        run["project_path"] = project.get("path", "")
    payload["runs"] = sorted(payload["runs"], key=run_display_key)
    payload["projects"] = projects
    payload["app"] = "Agent Desk"
    payload["scheduler"] = {
        "paused": scheduler.paused if scheduler else False,
        "settings": None,
    }
    return payload


def resume_unavailable_reason(run: dict[str, Any]) -> str:
    if str(run.get("state") or "") != "interrupted":
        return ""
    if not str(run.get("codex_thread_id") or ""):
        return "missing Codex thread id"
    if not str(run.get("worktree_path") or ""):
        return "missing worktree path"
    return ""


def enrich_resume_fields(run: dict[str, Any]) -> None:
    reason = resume_unavailable_reason(run)
    run["resume_available"] = str(run.get("state") or "") == "interrupted" and not reason
    run["resume_unavailable_reason"] = reason


def run_display_key(run: dict[str, Any]) -> tuple[int, int]:
    state = str(run.get("state") or "")
    return (RUN_DISPLAY_ORDER.get(state, 2), -int(run.get("id") or 0))


LOG_FILE_ORDER = [
    "prompt.md",
    "stderr.log",
    "error.log",
    "stdout.jsonl",
    "codex-resume.txt",
    "result.json",
    "request-changes-prompt.md",
    "request-changes.stdout.jsonl",
    "request-changes.stderr.log",
    "request-changes-result.json",
    "approve-finish-prompt.md",
    "approve-finish.stdout.jsonl",
    "approve-finish.stderr.log",
    "approve-finish-result.json",
    "auto-finish-prompt.md",
    "auto-finish.stdout.jsonl",
    "auto-finish.stderr.log",
    "auto-finish-result.json",
    "ai-review-prompt.md",
    "ai-review.stdout.jsonl",
    "ai-review.stderr.log",
    "ai-review-result.json",
    "open-pr-prompt.md",
    "open-pr.stdout.jsonl",
    "open-pr.stderr.log",
    "open-pr-result.json",
    "fix-ci-1-prompt.md",
    "fix-ci-1.stdout.jsonl",
    "fix-ci-1.stderr.log",
    "fix-ci-1-result.json",
    "fix-ci-2-prompt.md",
    "fix-ci-2.stdout.jsonl",
    "fix-ci-2.stderr.log",
    "fix-ci-2-result.json",
    "fix-ci-3-prompt.md",
    "fix-ci-3.stdout.jsonl",
    "fix-ci-3.stderr.log",
    "fix-ci-3-result.json",
    "resume-interrupted-prompt.md",
    "resume-interrupted.stdout.jsonl",
    "resume-interrupted.stderr.log",
    "resume-interrupted-result.json",
    "git-fetch.stderr.log",
    "git-fetch.stdout.log",
    "git-worktree.stderr.log",
    "git-worktree.stdout.log",
    "git-push.stderr.log",
    "git-push.stdout.log",
    "gh-pr-create.stderr.log",
    "gh-pr-create.stdout.log",
    "pr-body.md",
]


def available_log_files(run_dir: Path) -> list[str]:
    if not run_dir or not run_dir.exists() or not run_dir.is_dir():
        return []
    ordered = [name for name in LOG_FILE_ORDER if (run_dir / name).exists()]
    dynamic = sorted(
        path.name
        for path in run_dir.iterdir()
        if path.is_file() and is_dynamic_shutdown_log(path.name)
    )
    ordered.extend(name for name in dynamic if name not in ordered)
    return ordered


def is_dynamic_shutdown_log(name: str) -> bool:
    return (
        (name.startswith("shutdown-") and name.endswith(".json"))
        or (name.startswith("shutdown-resume-") and name.endswith(".md"))
    )


def is_allowed_run_log(name: str) -> bool:
    return name in set(LOG_FILE_ORDER) or is_dynamic_shutdown_log(name)


def extract_thread_id_from_run_dir(run_dir: Path) -> str:
    if not run_dir or not run_dir.exists() or not run_dir.is_dir():
        return ""
    stdout_path = run_dir / "stdout.jsonl"
    if not stdout_path.exists():
        return ""
    return extract_thread_id(stdout_path.read_text(encoding="utf-8", errors="replace"))


def make_handler(
    store: Store,
    scheduler: Scheduler | None = None,
    config_path: Path | None = None,
    restart_callback: Callable[[], None] | None = None,
    shutdown_callback: Callable[[], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self._send_text(HTML, "text/html; charset=utf-8")
                return
            if path == "/api/state":
                self._send_json(build_state_payload(store, scheduler))
                return
            if path == "/api/actions/shutdown-preview":
                if not scheduler:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "scheduler disabled")
                    return
                self._send_json(scheduler.shutdown_preview())
                return
            if path == "/api/fs":
                self._send_fs_listing()
                return
            if path == "/api/issues":
                if not scheduler:
                    self.send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE, "scheduler disabled"
                    )
                    return
                query = parse_qs(urlparse(self.path).query)
                repo_name = (query.get("repo", [""])[0] or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                try:
                    issues = scheduler.list_repo_issues(repo_name)
                except KeyError:
                    self.send_error(
                        HTTPStatus.NOT_FOUND, f"{repo_name} is not configured"
                    )
                    return
                except RuntimeError as exc:
                    self.send_error(HTTPStatus.BAD_GATEWAY, str(exc))
                    return
                self._send_json({"repo": repo_name, "issues": issues})
                return
            if path.startswith("/api/run/") and path.endswith("/file"):
                self._send_run_file(path)
                return
            if path.startswith("/api/run/") and path.endswith("/view"):
                self._send_run_viewer(path)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/actions/restart":
                self._send_json({"ok": True, "action": "restart"})
                if restart_callback is not None:
                    threading.Thread(target=restart_callback, daemon=False).start()
                return
            if not scheduler:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "scheduler disabled")
                return
            if path == "/api/actions/run-next":
                self._send_json(scheduler.run_next().__dict__)
                return
            if path == "/api/actions/shutdown-all":
                result = scheduler.shutdown_all()
                self._send_json(result)
                if shutdown_callback is not None:
                    threading.Thread(target=shutdown_callback, daemon=False).start()
                return
            if path == "/api/actions/sync-issues":
                payload = self._read_json()
                repo_name = str(payload.get("repo") or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                try:
                    issues = scheduler.sync_repo_issues(repo_name)
                except KeyError:
                    self.send_error(
                        HTTPStatus.NOT_FOUND, f"{repo_name} is not configured"
                    )
                    return
                except RuntimeError as exc:
                    self.send_error(HTTPStatus.BAD_GATEWAY, str(exc))
                    return
                self._send_json({"repo": repo_name, "issues": issues})
                return
            if path == "/api/actions/include-issue":
                payload = self._read_json()
                repo_name = str(payload.get("repo") or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                try:
                    issue_number = int(payload.get("issue"))
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "issue must be a number")
                    return
                if issue_number <= 0:
                    self.send_error(
                        HTTPStatus.BAD_REQUEST, "issue must be a positive number"
                    )
                    return
                self._send_json(
                    scheduler.mark_issue_ready(repo_name, issue_number).__dict__
                )
                return
            if path == "/api/actions/include-issues":
                payload = self._read_json()
                repo_name = str(payload.get("repo") or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                numbers = []
                for raw in payload.get("issues") or []:
                    try:
                        number = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if number > 0 and number not in numbers:
                        numbers.append(number)
                if not numbers:
                    self.send_error(
                        HTTPStatus.BAD_REQUEST,
                        "issues must be a non-empty list of numbers",
                    )
                    return
                dependency_mode = str(payload.get("dependency_mode") or "analyze")
                provided_dependencies = None
                if dependency_mode == "provided":
                    try:
                        provided_dependencies = self._provided_dependencies(
                            payload.get("dependencies"), repo_name
                        )
                    except ValueError as exc:
                        self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                        return
                try:
                    results = scheduler.mark_issues_ready(
                        repo_name,
                        numbers,
                        dependency_mode=dependency_mode,
                        provided_dependencies=provided_dependencies,
                    )
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                added = sum(1 for result in results if result.started)
                self._send_json(
                    {
                        "added": added,
                        "blocked": len(results) - added,
                        "dependency_mode": dependency_mode,
                        "requested": len(numbers),
                        "results": [result.__dict__ for result in results],
                    }
                )
                return
            if path == "/api/actions/dependency-override":
                payload = self._read_json()
                repo_name = str(payload.get("repo") or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                dependency_repo = str(payload.get("dependency_repo") or repo_name).strip()
                try:
                    issue_number = int(payload.get("issue"))
                    dependency_number = int(payload.get("dependency"))
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "issue and dependency must be numbers")
                    return
                if issue_number <= 0 or dependency_number <= 0:
                    self.send_error(HTTPStatus.BAD_REQUEST, "issue and dependency must be positive numbers")
                    return
                if payload.get("satisfied", True):
                    result = scheduler.mark_dependency_satisfied(
                        repo_name,
                        issue_number,
                        dependency_repo,
                        dependency_number,
                        reason=str(payload.get("reason") or "manual override"),
                    )
                else:
                    result = scheduler.clear_dependency_override(
                        repo_name,
                        issue_number,
                        dependency_repo,
                        dependency_number,
                    )
                self._send_json(result.__dict__)
                return
            if path == "/api/actions/dependency-edge":
                payload = self._read_json()
                repo_name = str(payload.get("repo") or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                dependency_repo = str(payload.get("dependency_repo") or repo_name).strip()
                try:
                    issue_number = int(payload.get("issue"))
                    dependency_number = int(payload.get("dependency"))
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "issue and dependency must be numbers")
                    return
                if issue_number <= 0 or dependency_number <= 0:
                    self.send_error(HTTPStatus.BAD_REQUEST, "issue and dependency must be positive numbers")
                    return
                if payload.get("present", True):
                    result = scheduler.add_dependency_edge(
                        repo_name,
                        issue_number,
                        dependency_repo,
                        dependency_number,
                        evidence=str(payload.get("evidence") or "manual dependency repair"),
                    )
                else:
                    result = scheduler.remove_dependency_edge(
                        repo_name,
                        issue_number,
                        dependency_repo,
                        dependency_number,
                    )
                self._send_json(result.__dict__)
                return
            if path == "/api/actions/remove-issue":
                payload = self._read_json()
                repo_name = str(payload.get("repo") or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                try:
                    issue_number = int(payload.get("issue"))
                except (TypeError, ValueError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "issue must be a number")
                    return
                if issue_number <= 0:
                    self.send_error(
                        HTTPStatus.BAD_REQUEST, "issue must be a positive number"
                    )
                    return
                self._send_json(
                    scheduler.remove_issue_from_desk(repo_name, issue_number).__dict__
                )
                return
            if path.startswith("/api/run/") and path.endswith("/start"):
                run_id = int(path.split("/")[3])
                self._send_json(scheduler.start_run(run_id).__dict__)
                return
            if path.startswith("/api/run/") and path.endswith("/interrupt"):
                run_id = int(path.split("/")[3])
                self._send_json(scheduler.interrupt_run(run_id).__dict__)
                return
            if path.startswith("/api/run/") and path.endswith("/request-changes"):
                run_id = int(path.split("/")[3])
                feedback = str(self._read_json().get("feedback") or "")
                if not feedback.strip():
                    self.send_error(HTTPStatus.BAD_REQUEST, "feedback is required")
                    return
                self._send_json(scheduler.request_changes(run_id, feedback).__dict__)
                return
            if path.startswith("/api/run/") and path.endswith("/resume-interrupted"):
                run_id = int(path.split("/")[3])
                self._send_json(scheduler.resume_interrupted(run_id).__dict__)
                return
            if path.startswith("/api/run/") and path.endswith("/approve-finish"):
                run_id = int(path.split("/")[3])
                result = scheduler.approve_finish(run_id)
                self._send_json(result.__dict__)
                return
            if path == "/api/projects":
                if not config_path or not scheduler:
                    self.send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE, "config path unavailable"
                    )
                    return
                payload = self._read_json()
                folder = str(payload.get("path") or "").strip()
                if not folder:
                    self.send_error(HTTPStatus.BAD_REQUEST, "path is required")
                    return
                try:
                    repo = add_project_to_config(config_path, folder)
                    scheduler.config = load_config(config_path)
                    scheduler.worker.config = scheduler.config
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._send_json(
                    {
                        "ok": True,
                        "repo": {"name": repo.name, "path": str(repo.local_path)},
                    }
                )
                return
            if path == "/api/projects/clone":
                if not config_path or not scheduler:
                    self.send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE, "config path unavailable"
                    )
                    return
                spec = str(self._read_json().get("repo") or "").strip()
                if not spec:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                try:
                    repo = add_remote_repo_to_config(config_path, spec)
                    scheduler.config = load_config(config_path)
                    scheduler.worker.config = scheduler.config
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self._send_json(
                    {
                        "ok": True,
                        "repo": {"name": repo.name, "path": str(repo.local_path)},
                    }
                )
                return
            if path == "/api/actions/pause":
                scheduler.pause()
                self._send_json({"ok": True, "paused": True})
                return
            if path == "/api/actions/resume":
                scheduler.resume()
                self._send_json({"ok": True, "paused": False})
                return
            if path == "/api/settings":
                payload = self._read_json()
                try:
                    settings = scheduler.update_settings(
                        workspace_path=payload.get("workspace_path")
                        if "workspace_path" in payload
                        else None,
                        auto_start_ready=payload.get("auto_start_ready")
                        if "auto_start_ready" in payload
                        else None,
                        max_concurrent_runs=payload.get("max_concurrent_runs")
                        if "max_concurrent_runs" in payload
                        else None,
                        requires_human_review=payload.get("requires_human_review")
                        if "requires_human_review" in payload
                        else None,
                        enable_ai_review=payload.get("enable_ai_review")
                        if "enable_ai_review" in payload
                        else None,
                        single_closeout_per_workspace=payload.get(
                            "single_closeout_per_workspace"
                        )
                        if "single_closeout_per_workspace" in payload
                        else None,
                        worker_timeout_seconds=payload.get("worker_timeout_seconds")
                        if "worker_timeout_seconds" in payload
                        else None,
                    )
                except (TypeError, ValueError) as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                results = (
                    scheduler.auto_start_ready_runs(payload.get("workspace_path"))
                    if settings["auto_start_ready"]
                    else []
                )
                self._send_json(
                    {
                        "ok": True,
                        "settings": settings,
                        "results": [result.__dict__ for result in results],
                    }
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            body = self.rfile.read(length).decode("utf-8")
            value = json.loads(body)
            return value if isinstance(value, dict) else {}

        def _provided_dependencies(
            self, raw_dependencies: Any, repo_name: str
        ) -> list[dict[str, Any]]:
            if raw_dependencies is None:
                return []
            if not isinstance(raw_dependencies, list):
                raise ValueError("dependencies must be a list")
            dependencies: list[dict[str, Any]] = []
            for raw in raw_dependencies:
                if not isinstance(raw, dict):
                    raise ValueError("dependency entries must be objects")
                try:
                    issue_number = int(raw.get("issue"))
                    dependency_number = int(
                        raw.get("dependency")
                        or raw.get("dependency_number")
                        or raw.get("number")
                    )
                except (TypeError, ValueError):
                    raise ValueError(
                        "dependency issue and dependency must be numbers"
                    ) from None
                if issue_number <= 0 or dependency_number <= 0:
                    raise ValueError(
                        "dependency issue and dependency must be positive numbers"
                    )
                dependency_repo = str(
                    raw.get("dependency_repo") or raw.get("repo") or repo_name
                ).strip()
                if not dependency_repo:
                    dependency_repo = repo_name
                if dependency_repo == repo_name and dependency_number == issue_number:
                    raise ValueError("issue cannot depend on itself")
                dependencies.append(
                    {
                        "issue": issue_number,
                        "dependency_repo": dependency_repo,
                        "dependency": dependency_number,
                        "evidence": str(raw.get("evidence") or "provided dependency"),
                    }
                )
            return dependencies

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(
            self, text: str, content_type: str = "text/plain; charset=utf-8"
        ) -> None:
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_run_file(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) < 5:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            run_id = int(parts[3])
            run = store.get_run(run_id)
            query = parse_qs(urlparse(self.path).query)
            requested = query.get("name", [""])[0]
            if not is_allowed_run_log(requested):
                self.send_error(HTTPStatus.BAD_REQUEST, "file not allowed")
                return
            run_dir = Path(run["run_dir"])
            candidate = run_dir / requested
            if not candidate.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_text(candidate.read_text(encoding="utf-8", errors="replace"))

        def _send_run_viewer(self, path: str) -> None:
            parts = path.split("/")
            if len(parts) < 5:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            try:
                run_id = int(parts[3])
                store.get_run(run_id)
            except (ValueError, KeyError):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            requested = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            if requested not in set(LOG_FILE_ORDER) or not requested.endswith(".jsonl"):
                self.send_error(HTTPStatus.BAD_REQUEST, "file not allowed")
                return
            self._send_text(
                run_viewer_html(run_id, requested), "text/html; charset=utf-8"
            )

        def _send_fs_listing(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            requested = query.get("path", [""])[0]
            base = Path(requested).expanduser() if requested else Path.home()
            try:
                base = base.resolve()
            except OSError:
                self.send_error(HTTPStatus.BAD_REQUEST, "invalid path")
                return
            if not base.is_dir():
                self.send_error(HTTPStatus.BAD_REQUEST, "not a directory")
                return
            entries = []
            try:
                children = sorted(base.iterdir(), key=lambda item: item.name.lower())
            except OSError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            for child in children:
                if child.name.startswith(".") or not child.is_dir():
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "is_git": (child / ".git").exists(),
                    }
                )
            parent = str(base.parent) if base.parent != base else None
            self._send_json(
                {
                    "path": str(base),
                    "parent": parent,
                    "is_git": (base / ".git").exists(),
                    "entries": entries,
                }
            )

    return Handler


def default_restart_argv() -> list[str]:
    return [sys.executable, "-m", "agent_desk", *sys.argv[1:]]


def restart_process(scheduler: Scheduler | None, server: ThreadingHTTPServer) -> None:
    if scheduler is not None:
        scheduler.stop()
    server.shutdown()
    time.sleep(0.05)
    try:
        restart_argv = default_restart_argv()
        os.execv(restart_argv[0], restart_argv)
    except Exception as exc:
        print(f"agent-desk: restart failed: {exc}", file=sys.stderr, flush=True)
        os._exit(1)


def shutdown_process(scheduler: Scheduler | None, server: ThreadingHTTPServer) -> None:
    if scheduler is not None:
        scheduler.stop()
    server.shutdown()


def serve_dashboard(
    host: str,
    port: int,
    store: Store,
    scheduler: Scheduler | None = None,
    config_path: Path | None = None,
    port_attempts: int = 20,
    on_serving: Callable[[str, int], None] | None = None,
    restart_callback: Callable[[], None] | None = None,
    shutdown_callback: Callable[[], None] | None = None,
) -> None:
    """Serve the dashboard, auto-incrementing the port if it is already in use.

    Binding is attempted on ``port``, ``port + 1``, ... up to ``port_attempts``
    candidates. ``on_serving`` is invoked with the host and the port that was
    actually bound, so callers can report the real URL.
    """
    server = None
    last_error: OSError | None = None
    for candidate in range(port, port + port_attempts):
        try:
            server = ThreadingHTTPServer((host, candidate), BaseHTTPRequestHandler)
            break
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            last_error = error
    if server is None:
        raise OSError(
            errno.EADDRINUSE,
            f"no free port in range {port}..{port + port_attempts - 1} on {host}",
        ) from last_error
    actual_restart_callback = restart_callback or (
        lambda: restart_process(scheduler, server)
    )
    actual_shutdown_callback = shutdown_callback or (
        lambda: shutdown_process(scheduler, server)
    )
    handler = make_handler(
        store,
        scheduler,
        config_path,
        actual_restart_callback,
        actual_shutdown_callback,
    )
    server.RequestHandlerClass = handler
    if on_serving is not None:
        on_serving(host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()


def run_viewer_html(run_id: int, name: str) -> str:
    """A terminal-style, auto-refreshing viewer for a run's ``.jsonl`` log.

    The page polls the raw file endpoint, parses each JSON line, and renders it
    readably; ``run_id``/``name`` are caller-validated so they are safe to embed.
    """
    file_url = f"/api/run/{run_id}/file?name={name}"
    return VIEWER_HTML.replace("__FILE_URL__", file_url).replace(
        "__TITLE__", f"{name} — run #{run_id}"
    )


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _load_page(html_name: str, script_name: str) -> str:
    """Load a static HTML page and inline its sibling script for a self-contained response."""
    html = (STATIC_DIR / html_name).read_text(encoding="utf-8")
    script = (STATIC_DIR / script_name).read_text(encoding="utf-8")
    return html.replace(
        f'<script src="{script_name}"></script>',
        "<script>\n" + script + "</script>",
    )


HTML = _load_page("dashboard.html", "dashboard.js")
VIEWER_HTML = _load_page("viewer.html", "viewer.js")
