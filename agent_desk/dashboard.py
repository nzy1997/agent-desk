from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import add_project_to_config, load_config
from .continuation import ContinuationRunner
from .scheduler import Scheduler
from .store import Store
from .worker import extract_thread_id, format_resume_command


def build_state_payload(store: Store, scheduler: Scheduler | None = None) -> dict[str, Any]:
    payload = store.dashboard_state()
    repo_paths = {}
    projects = []
    if scheduler:
        for repo in scheduler.config.repos:
            project = {"name": repo.name, "path": str(repo.local_path)}
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
    payload["projects"] = projects
    payload["app"] = "Agent Desk"
    payload["scheduler"] = {"paused": scheduler.paused if scheduler else False}
    return payload


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
    "open-pr-prompt.md",
    "open-pr.stdout.jsonl",
    "open-pr.stderr.log",
    "open-pr-result.json",
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
                self._start_continuation("approve_finish", run_id)
                self._send_json({"ok": True, "message": "Approve and finish started", "run_id": run_id})
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
            if path == "/api/actions/pause":
                scheduler.pause()
                self._send_json({"ok": True, "paused": True})
                return
            if path == "/api/actions/resume":
                scheduler.resume()
                self._send_json({"ok": True, "paused": False})
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

    return Handler


def serve_dashboard(
    host: str,
    port: int,
    store: Store,
    scheduler: Scheduler | None = None,
    config_path: Path | None = None,
) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(store, scheduler, config_path))
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
      background: #fff;
      border-radius: 6px;
      padding: 7px 10px;
      color: var(--ink);
      cursor: pointer;
    }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
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
    .project-form {
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
        <input id="project-path" placeholder="/path/to/project">
        <button onclick="addProject()">Add project</button>
      </div>
      <div id="stats"></div>
    </section>
    <section>
      <div class="section-head">
        <h2 id="runs-title">Current Runs</h2>
        <button id="project-back" onclick="backToProjects()" style="display:none">Back to folders</button>
      </div>
      <div id="runs"></div>
    </section>
    <section>
      <h2>Needs Attention</h2>
      <div id="attention"></div>
      <h2>Recent Events</h2>
      <div id="events"></div>
    </section>
  </main>
  <script>
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
    async function addProject() {
      const input = document.getElementById('project-path');
      const path = input.value.trim();
      if (!path) return;
      try {
        await postJson('/api/projects', { path });
        input.value = '';
      } catch (error) {
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
      return Object.entries(counts).sort().map(([key, value]) => `${key} ${value}`).join(' · ') || 'no runs';
    }
    function projectHtml(project, state) {
      const runs = state.runs.filter(run => run.project_path === project.path);
      return `<button class="project-row" data-path="${esc(project.path)}" onclick="selectProjectByPath(this)">
        <strong>${esc(project.name)}</strong>
        <div class="muted">${esc(project.path)}</div>
        <div>${runs.length} runs · ${esc(stateSummary(runs))}</div>
      </button>`;
    }
    function renderProjectIndex(state) {
      const projects = state.projects || [];
      document.getElementById('runs-title').textContent = 'Current Runs';
      document.getElementById('project-back').style.display = 'none';
      return projects.map(project => projectHtml(project, state)).join('') || '<div class="muted">No project folders</div>';
    }
    function renderSelectedProject(state, path) {
      const project = (state.projects || []).find(item => item.path === path);
      const runs = state.runs.filter(run => run.project_path === path);
      document.getElementById('runs-title').textContent = project ? project.name : 'Project Runs';
      document.getElementById('project-back').style.display = '';
      return runs.slice(0, 24).map(runHtml).join('') || '<div class="muted">No runs in this folder</div>';
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
