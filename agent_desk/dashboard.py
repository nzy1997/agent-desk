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


def build_state_payload(store: Store, scheduler: Scheduler | None = None) -> dict[str, Any]:
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
        thread_id = run.get("codex_thread_id") or extract_thread_id_from_run_dir(run_dir)
        run["codex_thread_id"] = thread_id
        run["resume_command"] = format_resume_command(thread_id, str(run.get("worktree_path") or ""))
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
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "scheduler disabled")
                    return
                query = parse_qs(urlparse(self.path).query)
                repo_name = (query.get("repo", [""])[0] or "").strip()
                if not repo_name:
                    self.send_error(HTTPStatus.BAD_REQUEST, "repo is required")
                    return
                try:
                    issues = scheduler.list_repo_issues(repo_name)
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND, f"{repo_name} is not configured")
                    return
                except RuntimeError as exc:
                    self.send_error(HTTPStatus.BAD_GATEWAY, str(exc))
                    return
                self._send_json({"repo": repo_name, "issues": issues})
                return
            if path.startswith("/api/run/") and path.endswith("/file"):
                self._send_run_file(path)
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
                    self.send_error(HTTPStatus.NOT_FOUND, f"{repo_name} is not configured")
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
                    self.send_error(HTTPStatus.BAD_REQUEST, "issue must be a positive number")
                    return
                self._send_json(scheduler.mark_issue_ready(repo_name, issue_number).__dict__)
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
                    self.send_error(HTTPStatus.BAD_REQUEST, "issues must be a non-empty list of numbers")
                    return
                results = [scheduler.mark_issue_ready(repo_name, number) for number in numbers]
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
                self._send_json({"ok": True, "message": "Request changes started", "run_id": run_id})
                return
            if path.startswith("/api/run/") and path.endswith("/approve-finish"):
                run_id = int(path.split("/")[3])
                result = scheduler.approve_finish(run_id)
                self._send_json(result.__dict__)
                return
            if path == "/api/projects":
                if not config_path or not scheduler:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "config path unavailable")
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
                self._send_json({"ok": True, "repo": {"name": repo.name, "path": str(repo.local_path)}})
                return
            if path == "/api/projects/clone":
                if not config_path or not scheduler:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "config path unavailable")
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
                self._send_json({"ok": True, "repo": {"name": repo.name, "path": str(repo.local_path)}})
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
                        workspace_path=payload.get("workspace_path") if "workspace_path" in payload else None,
                        auto_start_ready=payload.get("auto_start_ready") if "auto_start_ready" in payload else None,
                        max_concurrent_runs=payload.get("max_concurrent_runs") if "max_concurrent_runs" in payload else None,
                        requires_human_review=payload.get("requires_human_review")
                        if "requires_human_review" in payload
                        else None,
                        single_closeout_per_workspace=payload.get("single_closeout_per_workspace")
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
                self._send_json({"ok": True, "settings": settings, "results": [result.__dict__ for result in results]})
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

        def _start_continuation(self, method_name: str, run_id: int, *args: Any) -> None:
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

        def _send_text(self, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
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
            self._send_json({"path": str(base), "parent": parent, "entries": entries})

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


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Desk</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17191f;
      --muted: #667085;
      --line: #d9dee7;
      --accent: #126c8f;
      --warn: #a15c00;
      --bad: #b42318;
      --good: #067647;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 1;
    }
    h1 { font-size: 18px; margin: 0; }
    button {
      border: 1px solid var(--line);
      background: #e4e9f0;
      border-radius: 6px;
      padding: 7px 10px;
      color: var(--ink);
      cursor: pointer;
    }
    button:hover { background: #d7deea; }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    button.primary:hover { background: #0f5c79; }
    main {
      display: grid;
      grid-template-columns: minmax(220px, 280px) minmax(360px, 1fr) minmax(260px, 340px);
      min-height: calc(100vh - 57px);
    }
    section {
      border-right: 1px solid var(--line);
      padding: 16px;
      overflow: auto;
    }
    section:last-child { border-right: 0; }
    h2 {
      font-size: 13px;
      margin: 0 0 10px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .02em;
    }
    .metric-row, .run, .event, .project-row {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      margin-bottom: 8px;
    }
    .metric-row { display: flex; justify-content: space-between; }
    .project-row {
      width: 100%;
      text-align: left;
      display: block;
    }
    .project-form, .settings-panel {
      display: grid;
      gap: 8px;
      margin-bottom: 16px;
    }
    .project-form input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 8px;
      font: inherit;
    }
    .issue-picker {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      margin-bottom: 16px;
    }
    .issue-picker .issue-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
    }
    .issue-list { max-height: 320px; overflow-y: auto; }
    .issue-row {
      display: flex;
      gap: 8px;
      align-items: baseline;
      padding: 6px 10px;
      border-bottom: 1px solid var(--line);
    }
    .issue-row:last-child { border-bottom: none; }
    .issue-row input { margin-top: 2px; }
    .issue-row.on-desk { opacity: 0.6; }
    .issue-title { cursor: pointer; }
    .issue-title:hover { text-decoration: underline; }
    .issue-body {
      flex-basis: 100%;
      margin-top: 6px;
      padding: 6px 8px;
      border-radius: 6px;
      background: var(--bg);
      color: var(--muted);
      font-size: 12px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .issue-badge {
      margin-left: auto;
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
    }
    .settings-panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
    }
    .setting-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 30px;
    }
    .setting-row input[type="number"] {
      width: 72px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 5px 6px;
      font: inherit;
    }
    .setting-row input[type="checkbox"] {
      width: 16px;
      height: 16px;
      flex: 0 0 auto;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 10px;
    }
    .section-head h2 { margin: 0; }
    .muted { color: var(--muted); }
    .state-running { color: var(--accent); }
    .state-ready { color: var(--warn); }
    .state-blocked, .state-failed { color: var(--bad); }
    .state-done, .state-pr_open, .state-needs_review { color: var(--good); }
    .pr-status {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 4px;
      align-items: center;
      margin-top: 6px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 3px 6px;
      font-size: 12px;
      background: #fff;
    }
    .pr-status-pending { border-color: #f7c46c; color: var(--warn); }
    .pr-status-success { border-color: #8fd0ad; color: var(--good); }
    .pr-status-failure { border-color: #f4b7b0; color: var(--bad); }
    .pr-status-unknown { color: var(--muted); }
    .event.error { border-color: #f4b7b0; }
    .event.warning { border-color: #f7c46c; }
    .log-links {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .log-links a {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 3px 6px;
      text-decoration: none;
      color: var(--accent);
      background: #fff;
      font-size: 12px;
    }
    .resume-command {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 6px;
      align-items: center;
      margin-top: 8px;
    }
    .resume-command code {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 5px 6px;
      background: #f8fafc;
      color: var(--ink);
      overflow-wrap: anywhere;
      font-size: 12px;
    }
    .resume-command button {
      padding: 5px 8px;
      font-size: 12px;
    }
    .run-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .feedback-box {
      width: 100%;
      min-height: 64px;
      margin-top: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px;
      font: inherit;
      resize: vertical;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: #101828;
      color: #f8fafc;
      padding: 12px;
      border-radius: 6px;
      max-height: 300px;
      overflow: auto;
    }
    .project-form-buttons { display: flex; gap: 8px; }
    .project-form-buttons button { flex: 1; }
    .fs-browser {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      margin-bottom: 16px;
      max-height: 260px;
      overflow: auto;
    }
    .fs-head {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 6px;
    }
    .fs-head code { flex: 1; overflow-wrap: anywhere; }
    .fs-list { list-style: none; margin: 0; padding: 0; }
    .fs-list li { display: flex; align-items: center; gap: 8px; padding: 2px 0; }
    .fs-dir { flex: 1; text-align: left; }
    .git-badge {
      font-size: 11px;
      color: #16a34a;
      border: 1px solid #16a34a;
      border-radius: 4px;
      padding: 0 4px;
    }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      section { border-right: 0; border-bottom: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Agent Desk</h1>
      <div id="health" class="muted">Loading</div>
    </div>
    <div>
      <button onclick="action('/api/actions/pause')">Pause</button>
      <button onclick="action('/api/actions/resume')">Resume</button>
      <button class="primary" onclick="action('/api/actions/run-next')">Run next</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Queue</h2>
      <div class="project-form">
        <input id="clone-spec" placeholder="OWNER/REPO or GitHub URL to clone">
        <button onclick="cloneProject()">Clone &amp; add</button>
      </div>
      <div class="project-form">
        <button onclick="toggleBrowser()">Browse for local folder&hellip;</button>
      </div>
      <div id="fs-browser" class="fs-browser" style="display:none"></div>
      <div class="settings-panel">
        <div class="section-head">
          <h2>Workspace Settings</h2>
          <button id="settings-save" onclick="saveSettings()">Save</button>
        </div>
        <label class="setting-row" for="auto-start-ready">
          <span>Auto-start ready</span>
          <input id="auto-start-ready" type="checkbox" onchange="markSettingsDirty()">
        </label>
        <label class="setting-row" for="max-concurrent-runs">
          <span>Max parallel</span>
          <input id="max-concurrent-runs" type="number" min="1" step="1" onchange="markSettingsDirty()" oninput="markSettingsDirty()">
        </label>
        <label class="setting-row" for="requires-human-review">
          <span>Require human review</span>
          <input id="requires-human-review" type="checkbox" onchange="markSettingsDirty()">
        </label>
        <label class="setting-row" for="single-closeout-per-workspace">
          <span>One closeout per workspace</span>
          <input id="single-closeout-per-workspace" type="checkbox" onchange="markSettingsDirty()">
        </label>
        <div id="settings-status" class="muted">Defaults loaded</div>
      </div>
      <div id="stats"></div>
    </section>
    <section>
      <div class="section-head">
        <h2 id="runs-title">Tasks</h2>
        <button id="project-back" onclick="backToProjects()" style="display:none">Back to folders</button>
      </div>
      <div id="runs"></div>
    </section>
    <section>
      <h2>Add Issues</h2>
      <div id="issue-tools" class="project-form"></div>
      <div id="issue-picker"></div>
      <h2>Needs Attention</h2>
      <div id="attention"></div>
      <h2>Recent Events</h2>
      <div id="events"></div>
    </section>
  </main>
  <script>
    let settingsDirty = false;
    let settingsProjectPath = '';
    let currentRepoName = '';
    let pickerRepo = null;
    let issuesLoading = false;
    async function action(path) {
      await fetch(path, { method: 'POST' });
      await refresh();
    }
    async function postJson(path, body) {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
      });
      if (!res.ok) throw new Error(await res.text());
      await refresh();
    }
    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function jsString(value) {
      return JSON.stringify(String(value ?? '')).replace(/</g, '\\u003c');
    }
    async function copyResume(command) {
      await navigator.clipboard.writeText(command);
    }
    function closeBrowser() {
      const panel = document.getElementById('fs-browser');
      if (panel) panel.style.display = 'none';
    }
    async function addProject(path) {
      path = (path || '').trim();
      if (!path) return;
      try {
        await postJson('/api/projects', { path });
        closeBrowser();
      } catch (error) {
        alert(error.message || String(error));
      }
    }
    async function cloneProject() {
      const input = document.getElementById('clone-spec');
      const repo = input.value.trim();
      if (!repo) return;
      try {
        await postJson('/api/projects/clone', { repo });
        input.value = '';
      } catch (error) {
        alert(error.message || String(error));
      }
    }
    function renderIssueTools(state) {
      const tools = document.getElementById('issue-tools');
      if (!tools) return;
      const path = selectedProjectPath();
      const project = path ? projectForPath(state, path) : null;
      currentRepoName = project ? project.name : '';
      if (!issuesLoading) {
        tools.innerHTML = currentRepoName
          ? `<button onclick="syncIssues()">Sync issues</button>`
          : '<div class="muted">Select a project folder to see its issues.</div>';
      }
      if (!currentRepoName) {
        pickerRepo = null;
        document.getElementById('issue-picker').innerHTML = '';
      } else if (pickerRepo !== currentRepoName && !issuesLoading) {
        // New repo selected: show its synced issues from disk (no GitHub call).
        loadIssues();
      }
    }
    async function loadIssues() {
      const repo = currentRepoName;
      const picker = document.getElementById('issue-picker');
      if (!repo) return;
      issuesLoading = true;
      pickerRepo = repo;
      try {
        const res = await fetch(`/api/issues?repo=${encodeURIComponent(repo)}`);
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        renderIssuePicker(repo, data.issues || []);
      } catch (error) {
        pickerRepo = null;
        picker.innerHTML = `<div class="muted" style="padding:10px">Failed to load: ${esc(error.message || String(error))}</div>`;
      } finally {
        issuesLoading = false;
      }
    }
    async function syncIssues() {
      const repo = currentRepoName;
      const picker = document.getElementById('issue-picker');
      const tools = document.getElementById('issue-tools');
      if (!repo) { alert('Select a project folder first'); return; }
      issuesLoading = true;
      pickerRepo = repo;
      tools.innerHTML = '<button disabled>Syncing…</button>';
      picker.innerHTML = '<div class="muted" style="padding:10px">Syncing from GitHub…</div>';
      try {
        const res = await fetch('/api/actions/sync-issues', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo })
        });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        renderIssuePicker(repo, data.issues || []);
      } catch (error) {
        pickerRepo = null;
        picker.innerHTML = `<div class="muted" style="padding:10px">Sync failed: ${esc(error.message || String(error))}</div>`;
      } finally {
        issuesLoading = false;
        tools.innerHTML = `<button onclick="syncIssues()">Sync issues</button>`;
      }
    }
    function renderIssuePicker(repo, issues) {
      const picker = document.getElementById('issue-picker');
      pickerRepo = repo;
      if (!issues.length) {
        picker.innerHTML = '<div class="muted" style="padding:10px">No issues yet — click Sync issues.</div>';
        return;
      }
      const rows = issues.map(issue => {
        const badge = issue.on_desk ? '<span class="issue-badge">on desk</span>' : '';
        const attrs = issue.on_desk ? 'checked disabled' : '';
        const body = String(issue.body || '').trim();
        const bodyHtml = body
          ? `<div class="issue-body" id="body-${issue.number}" style="display:none">${esc(body)}</div>`
          : '';
        return `<div class="issue-row ${issue.on_desk ? 'on-desk' : ''}">
          <input type="checkbox" value="${issue.number}" ${attrs}>
          <span class="issue-title" onclick="toggleBody(${issue.number})"><strong>#${issue.number}</strong> ${esc(issue.title)}</span>
          ${badge}
          ${bodyHtml}
        </div>`;
      }).join('');
      picker.innerHTML = `<div class="issue-picker">
        <div class="issue-head">
          <strong>${esc(repo)}</strong>
          <button class="primary" onclick="addSelected()">Add selected</button>
        </div>
        <div class="issue-list">${rows}</div>
      </div>`;
    }
    function toggleBody(number) {
      const el = document.getElementById('body-' + number);
      if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
    }
    async function addSelected() {
      const repo = currentRepoName;
      if (!repo) { alert('Select a project folder first'); return; }
      const picker = document.getElementById('issue-picker');
      const checked = [...picker.querySelectorAll('input[type=checkbox]:checked:not([disabled])')];
      const issues = checked.map(box => parseInt(box.value, 10)).filter(Number.isInteger);
      if (!issues.length) { alert('Select at least one issue to add'); return; }
      const btn = picker.querySelector('.issue-head button');
      if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
      try {
        const res = await fetch('/api/actions/include-issues', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo, issues })
        });
        const result = await res.json().catch(() => ({}));
        if (!res.ok) { alert(result.message || 'Could not add the selected issues'); return; }
      } catch (error) {
        alert(error.message || String(error));
        return;
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Add selected'; }
      }
      // Mark added rows on-desk in place — no second GitHub round-trip.
      checked.forEach(box => {
        box.checked = true;
        box.disabled = true;
        const row = box.closest('.issue-row');
        if (row && !row.querySelector('.issue-badge')) {
          row.classList.add('on-desk');
          const badge = document.createElement('span');
          badge.className = 'issue-badge';
          badge.textContent = 'on desk';
          row.appendChild(badge);
        }
      });
      await refresh();
    }
    async function toggleBrowser() {
      const panel = document.getElementById('fs-browser');
      if (panel.style.display === 'none') {
        panel.style.display = 'block';
        await browseTo('');
      } else {
        panel.style.display = 'none';
      }
    }
    async function browseTo(path) {
      const res = await fetch('/api/fs?path=' + encodeURIComponent(path || ''));
      if (!res.ok) { alert(await res.text()); return; }
      renderBrowser(await res.json());
    }
    function renderBrowser(data) {
      const panel = document.getElementById('fs-browser');
      const up = data.parent
        ? `<button onclick="browseTo(${jsString(data.parent)})">&uarr; Up</button>`
        : '';
      const rows = (data.entries || []).map(entry => `
        <li>
          <button class="fs-dir" onclick="browseTo(${jsString(entry.path)})">${esc(entry.name)}${entry.is_git ? ' <span class="git-badge">git</span>' : ''}</button>
          <button onclick="selectFolder(${jsString(entry.path)})">Select</button>
        </li>`).join('') || '<li class="muted">No subfolders</li>';
      panel.innerHTML = `
        <div class="fs-head">
          ${up}
          <code>${esc(data.path)}</code>
          <button class="primary" onclick="selectFolder(${jsString(data.path)})">Add this folder</button>
        </div>
        <ul class="fs-list">${rows}</ul>`;
    }
    async function selectFolder(path) {
      await addProject(path);
    }
    function markSettingsDirty() {
      settingsDirty = true;
      const status = document.getElementById('settings-status');
      if (status) status.textContent = 'Unsaved changes';
    }
    function settingsControls() {
      return [
        document.getElementById('auto-start-ready'),
        document.getElementById('max-concurrent-runs'),
        document.getElementById('requires-human-review'),
        document.getElementById('single-closeout-per-workspace'),
        document.getElementById('settings-save')
      ];
    }
    function setSettingsDisabled(disabled) {
      settingsControls().forEach(control => {
        if (control) control.disabled = disabled;
      });
    }
    function projectForPath(state, path) {
      return (state.projects || []).find(item => item.path === path);
    }
    function renderSettings(state) {
      const path = selectedProjectPath();
      if (path !== settingsProjectPath) {
        settingsDirty = false;
        settingsProjectPath = path;
      }
      if (settingsDirty) return;
      const project = path ? projectForPath(state, path) : null;
      const settings = project && project.settings ? project.settings : {
        auto_start_ready: false,
        max_concurrent_runs: 1,
        requires_human_review: true,
        single_closeout_per_workspace: true
      };
      setSettingsDisabled(!project);
      document.getElementById('auto-start-ready').checked = !!settings.auto_start_ready;
      document.getElementById('max-concurrent-runs').value = Number(settings.max_concurrent_runs || 1);
      document.getElementById('requires-human-review').checked = settings.requires_human_review !== false;
      document.getElementById('single-closeout-per-workspace').checked = settings.single_closeout_per_workspace !== false;
      document.getElementById('settings-status').textContent = project ? `Settings for ${project.name}` : 'Select a folder';
    }
    async function saveSettings() {
      const path = selectedProjectPath();
      if (!path) {
        document.getElementById('settings-status').textContent = 'Select a folder';
        return;
      }
      const maxInput = document.getElementById('max-concurrent-runs');
      const max = Math.max(1, Number(maxInput.value || 1));
      settingsDirty = false;
      try {
        await postJson('/api/settings', {
          workspace_path: path,
          auto_start_ready: document.getElementById('auto-start-ready').checked,
          max_concurrent_runs: max,
          requires_human_review: document.getElementById('requires-human-review').checked,
          single_closeout_per_workspace: document.getElementById('single-closeout-per-workspace').checked
        });
        document.getElementById('settings-status').textContent = 'Saved';
      } catch (error) {
        settingsDirty = true;
        document.getElementById('settings-status').textContent = 'Save failed';
        alert(error.message || String(error));
      }
    }
    function selectedProjectPath() {
      if (!location.hash.startsWith('#project=')) return '';
      return decodeURIComponent(location.hash.slice('#project='.length));
    }
    function selectProject(path) {
      location.hash = `project=${encodeURIComponent(path)}`;
      refresh();
    }
    function selectProjectByPath(button) {
      selectProject(button.dataset.path || '');
    }
    function backToProjects() {
      history.pushState('', document.title, location.pathname + location.search);
      refresh();
    }
    function logLinks(run) {
      const files = run.log_files || [];
      if (!files.length) return '';
      const links = files.map(name => {
        const href = `/api/run/${run.id}/file?name=${encodeURIComponent(name)}`;
        return `<a href="${href}" target="_blank" rel="noopener">${esc(name)}</a>`;
      }).join('');
      return `<div class="log-links">${links}</div>`;
    }
    function resumeCommand(run) {
      const command = run.resume_command || '';
      if (!command) return '';
      return `<div class="resume-command"><code>${esc(command)}</code><button onclick="copyResume(${jsString(command)})">Copy</button></div>`;
    }
    function requestChanges(runId) {
      const box = document.getElementById(`feedback-${runId}`);
      const feedback = box ? box.value : '';
      return postJson(`/api/run/${runId}/request-changes`, { feedback });
    }
    function prStatus(run) {
      if (!run.pr_url) return '';
      const status = run.pr_ci_status || 'unknown';
      const labels = {
        pending: 'CI running',
        success: 'CI passed',
        failure: 'CI failed',
        unknown: 'CI unknown'
      };
      const summary = run.pr_ci_summary ? ` · ${esc(run.pr_ci_summary)}` : '';
      const attempts = Number(run.ci_fix_attempts || 0);
      const fixes = attempts ? ` · fixes ${attempts}/3` : '';
      const label = labels[status] || labels.unknown;
      return `<div class="pr-status pr-status-${esc(status)}"><strong>${esc(label)}</strong><span class="muted">${summary}${fixes}</span></div>`;
    }
    function runActions(run) {
      if (run.state === 'ready') {
        return `<div class="run-actions"><button class="primary" onclick="action('/api/run/${run.id}/start')">Run</button></div>`;
      }
      if (run.state === 'pr_open') {
        return `<textarea id="feedback-${run.id}" class="feedback-box" placeholder="Review feedback"></textarea>
          <div class="run-actions">
            <button onclick="requestChanges(${run.id})">Request changes</button>
            <button class="primary" onclick="action('/api/run/${run.id}/approve-finish')">Approve & finish</button>
          </div>`;
      }
      return '';
    }
    function runHtml(run) {
      return `<div class="run">
        <strong>#${run.issue_number} ${esc(run.issue_title)}</strong>
        <div class="muted">${esc(run.repo_name)} · ${esc(run.branch_name)}</div>
        <div>State: <span class="state-${esc(run.state)}">${esc(run.state)}</span></div>
        <div>Stage: ${esc(run.stage)}</div>
        ${run.pr_url ? `<div><a href="${esc(run.pr_url)}">Pull request</a></div>` : ''}
        ${prStatus(run)}
        ${resumeCommand(run)}
        ${runActions(run)}
        ${logLinks(run)}
      </div>`;
    }
    function stateCounts(runs) {
      return runs.reduce((counts, run) => {
        counts[run.state] = (counts[run.state] || 0) + 1;
        return counts;
      }, {});
    }
    function stateSummary(runs) {
      const counts = stateCounts(runs);
      return Object.entries(counts).sort().map(([key, value]) => `${value} ${key}`).join(' · ') || 'nothing queued';
    }
    function projectHtml(project, state) {
      const runs = state.runs.filter(run => run.project_path === project.path);
      return `<button class="project-row" data-path="${esc(project.path)}" onclick="selectProjectByPath(this)">
        <strong>${esc(project.name)}</strong>
        <div class="muted">${esc(project.path)}</div>
        <div>${esc(stateSummary(runs))}</div>
      </button>`;
    }
    function renderProjectIndex(state) {
      const projects = state.projects || [];
      document.getElementById('runs-title').textContent = 'Tasks';
      document.getElementById('project-back').style.display = 'none';
      return projects.map(project => projectHtml(project, state)).join('') || '<div class="muted">No project folders</div>';
    }
    function renderSelectedProject(state, path) {
      const project = (state.projects || []).find(item => item.path === path);
      const runs = state.runs.filter(run => run.project_path === path);
      document.getElementById('runs-title').textContent = project ? project.name : 'Tasks';
      document.getElementById('project-back').style.display = '';
      return runs.slice(0, 24).map(runHtml).join('') || '<div class="muted">No tasks in this folder</div>';
    }
    function renderRuns(state) {
      const path = selectedProjectPath();
      return path ? renderSelectedProject(state, path) : renderProjectIndex(state);
    }
    async function refresh() {
      const res = await fetch('/api/state');
      const state = await res.json();
      const stats = state.stats || {};
      document.getElementById('health').textContent = `${state.scheduler.paused ? 'Paused' : 'Active'} · ${Object.values(stats).reduce((a,b) => a + b, 0)} runs tracked`;
      renderSettings(state);
      renderIssueTools(state);
      document.getElementById('stats').innerHTML = Object.entries(stats).sort().map(([key, value]) =>
        `<div class="metric-row"><span>${esc(key)}</span><strong>${value}</strong></div>`
      ).join('') || '<div class="muted">No runs yet</div>';
      document.getElementById('runs').innerHTML = renderRuns(state);
      document.getElementById('attention').innerHTML = state.runs
        .filter(run => ['blocked','failed','needs_review'].includes(run.state))
        .slice(0, 8).map(runHtml).join('') || '<div class="muted">Nothing needs you</div>';
      document.getElementById('events').innerHTML = state.events.slice(0, 20).map(event =>
        `<div class="event ${esc(event.level)}">
          <div><strong>${esc(event.message)}</strong></div>
          <div class="muted">${esc(event.repo_name)} #${event.issue_number} · ${esc(event.created_at)}</div>
        </div>`
      ).join('') || '<div class="muted">No events</div>';
    }
    refresh();
    window.addEventListener('hashchange', refresh);
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""
