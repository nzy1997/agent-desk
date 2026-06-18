from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .scheduler import Scheduler
from .store import Store
from .worker import extract_thread_id, format_resume_command


def build_state_payload(store: Store, scheduler: Scheduler | None = None) -> dict[str, Any]:
    payload = store.dashboard_state()
    for run in payload["runs"]:
        run_dir = Path(run.get("run_dir") or "")
        run["log_files"] = available_log_files(run_dir)
        thread_id = run.get("codex_thread_id") or extract_thread_id_from_run_dir(run_dir)
        run["codex_thread_id"] = thread_id
        run["resume_command"] = format_resume_command(thread_id, str(run.get("worktree_path") or ""))
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


def make_handler(store: Store, scheduler: Scheduler | None = None) -> type[BaseHTTPRequestHandler]:
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


def serve_dashboard(host: str, port: int, store: Store, scheduler: Scheduler | None = None) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(store, scheduler))
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
    .metric-row, .run, .event {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      margin-bottom: 8px;
    }
    .metric-row { display: flex; justify-content: space-between; }
    .muted { color: var(--muted); }
    .state-running { color: var(--accent); }
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
      <div id="stats"></div>
    </section>
    <section>
      <h2>Current Runs</h2>
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
    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function jsString(value) {
      return JSON.stringify(String(value ?? '')).replace(/</g, '\\u003c');
    }
    async function copyResume(command) {
      await navigator.clipboard.writeText(command);
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
    function runHtml(run) {
      return `<div class="run">
        <strong>#${run.issue_number} ${esc(run.issue_title)}</strong>
        <div class="muted">${esc(run.repo_name)} · ${esc(run.branch_name)}</div>
        <div>State: <span class="state-${esc(run.state)}">${esc(run.state)}</span></div>
        <div>Stage: ${esc(run.stage)}</div>
        ${run.pr_url ? `<div><a href="${esc(run.pr_url)}">Pull request</a></div>` : ''}
        ${resumeCommand(run)}
        ${logLinks(run)}
      </div>`;
    }
    async function refresh() {
      const res = await fetch('/api/state');
      const state = await res.json();
      const stats = state.stats || {};
      document.getElementById('health').textContent = `${state.scheduler.paused ? 'Paused' : 'Active'} · ${Object.values(stats).reduce((a,b) => a + b, 0)} runs tracked`;
      document.getElementById('stats').innerHTML = Object.entries(stats).sort().map(([key, value]) =>
        `<div class="metric-row"><span>${esc(key)}</span><strong>${value}</strong></div>`
      ).join('') || '<div class="muted">No runs yet</div>';
      document.getElementById('runs').innerHTML = state.runs.slice(0, 12).map(runHtml).join('') || '<div class="muted">Idle</div>';
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
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""
