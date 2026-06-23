from __future__ import annotations

import errno
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .config import add_project_to_config, add_remote_repo_to_config, load_config
from .continuation import ContinuationRunner
from .scheduler import Scheduler
from .store import Store
from .worker import extract_thread_id, format_resume_command


RUN_DISPLAY_ORDER = {
    "running": 0,
    "ready": 1,
    "done": 3,
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
    return [name for name in LOG_FILE_ORDER if (run_dir / name).exists()]


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
            if not scheduler:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "scheduler disabled")
                return
            path = urlparse(self.path).path
            if path == "/api/actions/run-next":
                self._send_json(scheduler.run_next().__dict__)
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
                results = [
                    scheduler.mark_issue_ready(repo_name, number) for number in numbers
                ]
                added = sum(1 for result in results if result.started)
                self._send_json(
                    {
                        "added": added,
                        "requested": len(numbers),
                        "results": [result.__dict__ for result in results],
                    }
                )
                return
            if path.startswith("/api/run/") and path.endswith("/start"):
                run_id = int(path.split("/")[3])
                self._send_json(scheduler.start_run(run_id).__dict__)
                return
            if path.startswith("/api/run/") and path.endswith("/request-changes"):
                run_id = int(path.split("/")[3])
                feedback = str(self._read_json().get("feedback") or "")
                if not feedback.strip():
                    self.send_error(HTTPStatus.BAD_REQUEST, "feedback is required")
                    return
                self._start_continuation("request_changes", run_id, feedback)
                self._send_json(
                    {"ok": True, "message": "Request changes started", "run_id": run_id}
                )
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
                        single_closeout_per_workspace=payload.get(
                            "single_closeout_per_workspace"
                        )
                        if "single_closeout_per_workspace" in payload
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

        def _start_continuation(
            self, method_name: str, run_id: int, *args: Any
        ) -> None:
            runner = ContinuationRunner(scheduler.config, store)
            method = getattr(runner, method_name)
            threading.Thread(target=method, args=(run_id, *args), daemon=True).start()

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
            allowed = set(LOG_FILE_ORDER)
            if requested not in allowed:
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
            if requested not in set(LOG_FILE_ORDER):
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


def serve_dashboard(
    host: str,
    port: int,
    store: Store,
    scheduler: Scheduler | None = None,
    config_path: Path | None = None,
    port_attempts: int = 20,
    on_serving: Callable[[str, int], None] | None = None,
) -> None:
    """Serve the dashboard, auto-incrementing the port if it is already in use.

    Binding is attempted on ``port``, ``port + 1``, ... up to ``port_attempts``
    candidates. ``on_serving`` is invoked with the host and the port that was
    actually bound, so callers can report the real URL.
    """
    handler = make_handler(store, scheduler, config_path)
    server = None
    last_error: OSError | None = None
    for candidate in range(port, port + port_attempts):
        try:
            server = ThreadingHTTPServer((host, candidate), handler)
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
