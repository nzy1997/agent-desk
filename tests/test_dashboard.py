import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest import mock

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.dashboard import (
    HTML,
    build_state_payload,
    make_handler,
    restart_process,
    run_viewer_html,
    serve_dashboard,
)
from agent_desk.dependencies import DependencyGraph, IssueDependencies
from agent_desk.scheduler import RunNextResult, Scheduler
from agent_desk.store import Store


class _IncludeIssueGitHub:
    def list_open_issues(self, repo, limit=200):
        return [
            {"number": 5, "title": "Wire it up", "body": "do 5", "url": "https://example.test/5", "labels": []},
            {"number": 6, "title": "Second", "body": "do 6", "url": "https://example.test/6", "labels": []},
        ]

    def get_issue(self, repo, issue_number):
        return {"number": issue_number, "title": "Wire it up", "body": "do", "url": "https://example.test/5"}

    def add_label(self, repo, issue_number, label):
        self.added = (repo, issue_number, label)


class _NoDependencyExtractor:
    def __init__(self):
        self.calls = []

    def __call__(self, repo_name, issues):
        self.calls.append((repo_name, issues))
        return DependencyGraph(
            repo=repo_name,
            issues=[IssueDependencies(number=int(issue["number"]), depends_on=[]) for issue in issues],
            warnings=[],
        )


class _RequestChangesScheduler:
    paused = False

    def __init__(self):
        self.calls = []

    def request_changes(self, run_id, feedback):
        self.calls.append((run_id, feedback))
        return RunNextResult(True, "Request changes started", run_id)


class _RestartScheduler:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _RestartServer:
    def __init__(self):
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True


class _HandlerSocket:
    def __init__(self, request: bytes):
        self._request = BytesIO(request)
        self.response = BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            return self._request
        return self.response

    def sendall(self, data):
        self.response.write(data)


class DashboardTests(unittest.TestCase):
    def test_state_payload_is_json_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            store.add_event(run_id, "info", "claim", "Claimed issue", {})

            payload = build_state_payload(store)

        encoded = json.dumps(payload)
        self.assertIn("Agent Desk", encoded)
        self.assertEqual(payload["app"], "Agent Desk")
        self.assertEqual(payload["runs"][0]["issue_number"], 5)

    def test_state_payload_orders_runs_by_display_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            running_id = store.create_run(
                repo_name="octo/example",
                issue_number=3,
                issue_title="Running",
                issue_url="https://github.com/octo/example/issues/3",
                branch_name="agent/issue-3-running",
            )
            store.update_run(running_id, state="running", stage="claimed")
            ready_id = store.create_run(
                repo_name="octo/example",
                issue_number=2,
                issue_title="Ready",
                issue_url="https://github.com/octo/example/issues/2",
                branch_name="agent/issue-2-ready",
            )
            store.update_run(ready_id, state="ready", stage="waiting for human run")
            pr_id = store.create_run(
                repo_name="octo/example",
                issue_number=4,
                issue_title="PR",
                issue_url="https://github.com/octo/example/issues/4",
                branch_name="agent/issue-4-pr",
            )
            store.update_run(pr_id, state="pr_open", stage="pr_open")
            failed_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Failed",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-failed",
            )
            store.update_run(failed_id, state="failed", stage="failed")
            blocked_id = store.create_run(
                repo_name="octo/example",
                issue_number=6,
                issue_title="Blocked",
                issue_url="https://github.com/octo/example/issues/6",
                branch_name="agent/issue-6-blocked",
            )
            store.update_run(blocked_id, state="blocked", stage="blocked")
            done_id = store.create_run(
                repo_name="octo/example",
                issue_number=1,
                issue_title="Done",
                issue_url="https://github.com/octo/example/issues/1",
                branch_name="agent/issue-1-done",
            )
            store.update_run(done_id, state="done", stage="done")

            payload = build_state_payload(store)

        self.assertEqual(
            [run["state"] for run in payload["runs"]],
            ["running", "pr_open", "failed", "ready", "blocked", "done"],
        )

    def test_state_payload_includes_workspace_scheduler_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[
                    RepoConfig(
                        name="octo/example",
                        local_path=root / "example",
                        auto_start_ready=True,
                        max_concurrent_runs=2,
                    )
                ],
            )
            scheduler = Scheduler(config, store)

            payload = build_state_payload(store, scheduler)

        self.assertEqual(payload["scheduler"]["settings"], None)
        self.assertEqual(payload["projects"][0]["path"], str(root / "example"))
        self.assertEqual(
            payload["projects"][0]["settings"],
            {
                "auto_start_ready": False,
                "max_concurrent_runs": 2,
                "requires_human_review": True,
                "single_closeout_per_workspace": True,
            },
        )

    def test_state_payload_lists_existing_run_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "issue-5" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "prompt.md").write_text("prompt", encoding="utf-8")
            (run_dir / "stderr.log").write_text("stderr", encoding="utf-8")
            (run_dir / "error.log").write_text("error", encoding="utf-8")
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            store.update_run(run_id, state="failed", stage="failed", run_dir=str(run_dir))

            payload = build_state_payload(store)

        self.assertEqual(payload["runs"][0]["log_files"], ["prompt.md", "stderr.log", "error.log"])

    def test_dashboard_html_renders_log_links(self):
        self.assertIn("logLinks(run)", HTML)
        self.assertIn("/api/run/${run.id}/${action}?name=", HTML)
        # .jsonl logs open the terminal-style viewer; other files open raw.
        self.assertIn("name.endsWith('.jsonl') ? 'view' : 'file'", HTML)

    def test_run_viewer_html_embeds_raw_file_url_and_live_poll(self):
        html = run_viewer_html(7, "stdout.jsonl")
        self.assertIn("/api/run/7/file?name=stdout.jsonl", html)
        self.assertIn("stdout.jsonl — run #7", html)
        self.assertIn("JSON.parse", html)
        self.assertIn("setInterval(tick", html)

    def test_run_viewer_renders_codex_items_as_terminal_transcript(self):
        html = run_viewer_html(7, "stdout.jsonl")
        # Merges item.started/item.completed by id so each item renders once.
        self.assertIn("item.started", html)
        self.assertIn("item.completed", html)
        # Renders command output (aggregated_output) and structured messages,
        # not just a single flattened text field.
        self.assertIn("aggregated_output", html)
        self.assertIn("renderCommand", html)
        self.assertIn("renderMessage", html)
        self.assertIn("renderFileChange", html)
        # Strips ANSI control sequences for a clean terminal feel.
        self.assertIn("function clean", html)
        # Terminal blocks preserve real newlines.
        self.assertIn("white-space: pre-wrap", html)

    def test_state_payload_includes_resume_command_from_stored_thread_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree_path = root / "worktrees" / "repo with spaces"
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            store.update_run(
                run_id,
                state="blocked",
                stage="blocked",
                worktree_path=str(worktree_path),
                codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
            )

            payload = build_state_payload(store)
            run = payload["runs"][0]

        self.assertEqual(run["codex_thread_id"], "019ed932-fe5d-7391-b856-98b2239a6380")
        self.assertIn("codex resume --include-non-interactive", run["resume_command"])
        self.assertIn("019ed932-fe5d-7391-b856-98b2239a6380", run["resume_command"])
        self.assertIn(f"'{worktree_path}'", run["resume_command"])

    def test_state_payload_backfills_resume_command_from_stdout_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "issue-5" / "run-1"
            worktree_path = root / "worktree"
            run_dir.mkdir(parents=True)
            (run_dir / "stdout.jsonl").write_text(
                '{"type":"thread.started","thread_id":"019ed932-fe5d-7391-b856-98b2239a6380"}\n',
                encoding="utf-8",
            )
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            store.update_run(
                run_id,
                state="blocked",
                stage="blocked",
                run_dir=str(run_dir),
                worktree_path=str(worktree_path),
            )

            payload = build_state_payload(store)
            run = payload["runs"][0]

        self.assertEqual(run["codex_thread_id"], "019ed932-fe5d-7391-b856-98b2239a6380")
        self.assertIn("codex resume --include-non-interactive", run["resume_command"])

    def test_state_payload_marks_interrupted_run_resume_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree_path = root / "worktree"
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            store.update_run(
                run_id,
                state="interrupted",
                stage="interrupted by shutdown",
                worktree_path=str(worktree_path),
                codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
            )

            run = build_state_payload(store)["runs"][0]

        self.assertTrue(run["resume_available"])
        self.assertEqual(run["resume_unavailable_reason"], "")

    def test_state_payload_marks_interrupted_run_resume_unavailable_without_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree_path = root / "worktree"
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            store.update_run(
                run_id,
                state="interrupted",
                stage="interrupted by shutdown",
                worktree_path=str(worktree_path),
            )

            run = build_state_payload(store)["runs"][0]

        self.assertFalse(run["resume_available"])
        self.assertEqual(run["resume_unavailable_reason"], "missing Codex thread id")

    def test_dashboard_html_renders_resume_command(self):
        self.assertIn("resumeCommand(run)", HTML)
        self.assertIn("navigator.clipboard.writeText(command)", HTML)

    def test_dashboard_html_renders_manual_run_and_pr_action_buttons(self):
        self.assertIn("/api/run/${run.id}/start", HTML)
        self.assertIn("/api/run/${runId}/request-changes", HTML)
        self.assertIn("/api/run/${run.id}/approve-finish", HTML)
        self.assertIn("Approve & finish", HTML)

    def test_state_payload_includes_pr_ci_status_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/example/pull/9",
                pr_ci_status="pending",
                pr_ci_summary="1 passed, 1 pending",
                pr_ci_checked_at="2026-06-18T00:00:00+00:00",
                ci_fix_attempts=1,
            )

            run = build_state_payload(store)["runs"][0]

        self.assertEqual(run["pr_ci_status"], "pending")
        self.assertEqual(run["pr_ci_summary"], "1 passed, 1 pending")
        self.assertEqual(run["ci_fix_attempts"], 1)

    def test_dashboard_html_renders_pr_ci_status(self):
        self.assertIn("prStatus(run)", HTML)
        self.assertIn("CI running", HTML)
        self.assertIn("CI passed", HTML)
        self.assertIn("CI failed", HTML)

    def test_state_payload_includes_projects_and_run_project_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "example"
            repo_path.mkdir()
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Add dashboard",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-add-dashboard",
            )
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/example", local_path=repo_path)],
            )
            scheduler = Scheduler(config, store)

            payload = build_state_payload(store, scheduler)
            run = payload["runs"][0]

        self.assertEqual(payload["projects"][0]["name"], "octo/example")
        self.assertEqual(payload["projects"][0]["path"], str(repo_path))
        self.assertEqual(payload["projects"][0]["settings"]["max_concurrent_runs"], 1)
        self.assertEqual(run["project_path"], str(repo_path))
        self.assertEqual(run["project_name"], "octo/example")

    def test_dashboard_html_renders_add_project_and_folder_index(self):
        # Folders are added by browsing and selecting, not by typing a path.
        self.assertIn("addProject(path)", HTML)
        self.assertIn("selectFolder(", HTML)
        self.assertIn("toggleBrowser()", HTML)
        self.assertNotIn('id="project-path"', HTML)
        self.assertIn("/api/projects", HTML)
        self.assertIn("renderProjectIndex(state)", HTML)
        self.assertIn("selectProjectByPath(this)", HTML)
        self.assertIn("Back to folders", HTML)

    def test_dashboard_html_renders_issue_picker_control(self):
        self.assertIn('id="issue-tools"', HTML)
        self.assertIn("syncIssues()", HTML)
        self.assertIn("/api/actions/sync-issues", HTML)
        self.assertIn("renderIssuePicker(", HTML)
        self.assertIn("addSelected('analyze')", HTML)
        self.assertIn("addSelected('direct')", HTML)
        self.assertIn('class="issue-actions"', HTML)
        self.assertIn('title="Analyze dependencies"', HTML)
        self.assertIn('aria-label="Analyze dependencies for selected issues"', HTML)
        self.assertIn(">Analyze</button>", HTML)
        self.assertNotIn(">Analyze dependencies</button>", HTML)
        self.assertIn('title="Add all directly"', HTML)
        self.assertIn('aria-label="Add selected issues directly"', HTML)
        self.assertIn(">Add</button>", HTML)
        self.assertNotIn(">Add all directly</button>", HTML)
        self.assertIn("toggleBody(", HTML)
        self.assertIn("/api/actions/include-issues", HTML)
        self.assertIn("removeIssue(", HTML)
        self.assertIn("/api/actions/remove-issue", HTML)
        self.assertIn("renderIssueTools(state)", HTML)
        self.assertIn("on desk", HTML)
        # Picker follows the selected project, not a separate repo dropdown.
        self.assertNotIn('id="include-repo"', HTML)

    def test_dashboard_html_renders_workspace_settings_controls(self):
        self.assertIn("/api/settings", HTML)
        self.assertIn("workspace_path", HTML)
        self.assertIn("Workspace Settings", HTML)
        self.assertIn("auto-start-ready", HTML)
        self.assertIn("max-concurrent-runs", HTML)
        self.assertIn("requires-human-review", HTML)
        self.assertIn("single-closeout-per-workspace", HTML)
        self.assertIn("saveSettings()", HTML)

    def test_dashboard_html_includes_restart_button(self):
        self.assertIn("Restart", HTML)
        self.assertIn("/api/actions/restart", HTML)
        self.assertIn("restartWithGuard()", HTML)
        self.assertIn("function restartHazards", HTML)
        self.assertIn("confirm(", HTML)
        self.assertIn("supervisor_pid", HTML)
        self.assertIn("request-changes queued", HTML)
        self.assertIn("issuesLoading", HTML)

    def test_dashboard_html_includes_interrupted_attention_state(self):
        self.assertIn("['blocked','failed','interrupted','needs_review']", HTML)

    def test_restart_process_reexecs_agent_desk_module(self):
        scheduler = _RestartScheduler()
        server = _RestartServer()
        argv = ["/repo/agent_desk/__main__.py", "serve", "--config", "config/repos.toml"]

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch("agent_desk.dashboard.time.sleep"),
            mock.patch("agent_desk.dashboard.os.execv") as execv,
        ):
            restart_process(scheduler, server)

        self.assertTrue(scheduler.stopped)
        self.assertTrue(server.shutdown_called)
        execv.assert_called_once_with(
            sys.executable,
            [
                sys.executable,
                "-m",
                "agent_desk",
                "serve",
                "--config",
                "config/repos.toml",
            ],
        )

    def test_restart_route_starts_non_daemon_worker(self):
        threads = []

        class RecordingThread:
            def __init__(self, target, daemon=None):
                self.target = target
                self.daemon_arg = daemon
                self.daemon = threading.current_thread().daemon if daemon is None else daemon

            def start(self):
                threads.append(self)

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            handler = make_handler(store, None, None, lambda: None)
            request = (
                b"POST /api/actions/restart HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n"
            )

            with mock.patch("agent_desk.dashboard.threading.Thread", RecordingThread):
                sock = _HandlerSocket(request)
                handler(sock, ("127.0.0.1", 12345), object())

        self.assertEqual(len(threads), 1)
        self.assertIs(threads[0].daemon_arg, False)
        self.assertFalse(threads[0].daemon)
        self.assertIn(b'"action": "restart"', sock.response.getvalue())

    def test_restart_route_returns_ok_and_invokes_restart_callback(self):
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            restarted = threading.Event()
            bound: dict[str, int] = {}
            ready = threading.Event()

            thread = threading.Thread(
                target=serve_dashboard,
                kwargs={
                    "host": host,
                    "port": 0,
                    "store": store,
                    "on_serving": lambda _h, port: (bound.update(port=port), ready.set()),
                    "restart_callback": restarted.set,
                },
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")

            request = urllib.request.Request(
                f"http://{host}:{bound['port']}/api/actions/restart",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "restart")
            self.assertTrue(restarted.wait(timeout=5), "restart callback was not invoked")

    def test_request_changes_route_dispatches_through_scheduler(self):
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            scheduler = _RequestChangesScheduler()
            bound: dict[str, int] = {}
            ready = threading.Event()
            thread = threading.Thread(
                target=serve_dashboard,
                kwargs={
                    "host": host,
                    "port": 0,
                    "store": store,
                    "scheduler": scheduler,
                    "on_serving": lambda _h, port: (bound.update(port=port), ready.set()),
                },
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")

            request = urllib.request.Request(
                f"http://{host}:{bound['port']}/api/run/42/request-changes",
                data=json.dumps({"feedback": "tighten the tests"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertTrue(payload["started"])
            self.assertEqual(payload["run_id"], 42)
            self.assertEqual(scheduler.calls, [(42, "tighten the tests")])


class ServeDashboardPortTests(unittest.TestCase):
    def test_serve_dashboard_auto_increments_when_port_busy(self):
        host = "127.0.0.1"
        # Occupy the preferred port so serve_dashboard must move to the next one.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind((host, 0))
        busy_port = blocker.getsockname()[1]
        blocker.listen(1)
        bound: dict[str, int] = {}
        ready = threading.Event()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "desk.sqlite")

                def on_serving(_host: str, port: int) -> None:
                    bound["port"] = port
                    ready.set()

                thread = threading.Thread(
                    target=serve_dashboard,
                    kwargs={
                        "host": host,
                        "port": busy_port,
                        "store": store,
                        "on_serving": on_serving,
                    },
                    daemon=True,
                )
                thread.start()
                self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")

                self.assertNotEqual(bound["port"], busy_port)
                self.assertGreater(bound["port"], busy_port)
                with urllib.request.urlopen(
                    f"http://{host}:{bound['port']}/api/state", timeout=5
                ) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["app"], "Agent Desk")
        finally:
            blocker.close()

    def test_serve_dashboard_raises_when_no_port_free(self):
        host = "127.0.0.1"
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind((host, 0))
        busy_port = blocker.getsockname()[1]
        blocker.listen(1)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "desk.sqlite")
                with self.assertRaises(OSError):
                    serve_dashboard(host, busy_port, store, port_attempts=1)
        finally:
            blocker.close()


class IssuePickerRouteTests(unittest.TestCase):
    host = "127.0.0.1"

    def _serve(self, store, scheduler):
        bound: dict[str, int] = {}
        ready = threading.Event()
        thread = threading.Thread(
            target=serve_dashboard,
            kwargs={
                "host": self.host,
                "port": 0,
                "store": store,
                "scheduler": scheduler,
                "on_serving": lambda _h, port: (bound.update(port=port), ready.set()),
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")
        return bound["port"]

    def _request(self, port, path, body=None):
        url = f"http://{self.host}:{port}{path}"
        if body is None:
            request = urllib.request.Request(url, method="GET")
        else:
            request = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, None

    def _build(self, tmp):
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        config = AgentDeskConfig(
            data_dir=root / "data",
            repos=[RepoConfig(name="octo/example", local_path=root / "example")],
        )
        extractor = _NoDependencyExtractor()
        scheduler = Scheduler(config, store, github=_IncludeIssueGitHub(), dependency_extractor=extractor)
        scheduler.dependency_extractor_spy = extractor
        return store, scheduler

    def test_include_issue_route_labels_and_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, scheduler = self._build(tmp)
            port = self._serve(store, scheduler)

            status, payload = self._request(port, "/api/actions/include-issue", {"repo": "octo/example", "issue": 5})
            self.assertEqual(status, 200)
            self.assertTrue(payload["started"])
            self.assertEqual([run["issue_number"] for run in store.list_runs()], [5])

            self.assertEqual(self._request(port, "/api/actions/include-issue", {"issue": 5})[0], 400)
            self.assertEqual(
                self._request(port, "/api/actions/include-issue", {"repo": "octo/example", "issue": "abc"})[0], 400
            )
            self.assertEqual(
                self._request(port, "/api/actions/include-issue", {"repo": "octo/example", "issue": 0})[0], 400
            )

    def test_sync_and_listing_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, scheduler = self._build(tmp)
            port = self._serve(store, scheduler)

            # Before sync the disk listing is empty (no GitHub call on read).
            status, payload = self._request(port, "/api/issues?repo=octo/example")
            self.assertEqual(status, 200)
            self.assertEqual(payload["issues"], [])

            # Sync pulls issues to disk; all start off-desk with their bodies.
            status, payload = self._request(port, "/api/actions/sync-issues", {"repo": "octo/example"})
            self.assertEqual(status, 200)
            on_desk = {i["number"]: i["on_desk"] for i in payload["issues"]}
            self.assertEqual(on_desk, {5: False, 6: False})
            self.assertEqual({i["number"]: i["body"] for i in payload["issues"]}[5], "do 5")

            # Adding one flips its on_desk flag on the disk-backed listing.
            self._request(port, "/api/actions/include-issues", {"repo": "octo/example", "issues": [6]})
            status, payload = self._request(port, "/api/issues?repo=octo/example")
            self.assertEqual({i["number"]: i["on_desk"] for i in payload["issues"]}, {5: False, 6: True})

            # Removing it from the local desk makes it selectable again.
            status, payload = self._request(port, "/api/actions/remove-issue", {"repo": "octo/example", "issue": 6})
            self.assertEqual(status, 200)
            self.assertTrue(payload["started"])
            status, payload = self._request(port, "/api/issues?repo=octo/example")
            self.assertEqual({i["number"]: i["on_desk"] for i in payload["issues"]}, {5: False, 6: False})

            self.assertEqual(self._request(port, "/api/issues")[0], 400)
            self.assertEqual(self._request(port, "/api/issues?repo=octo/missing")[0], 404)
            self.assertEqual(self._request(port, "/api/actions/sync-issues", {})[0], 400)
            self.assertEqual(self._request(port, "/api/actions/sync-issues", {"repo": "octo/missing"})[0], 404)
            self.assertEqual(self._request(port, "/api/actions/remove-issue", {"issue": 6})[0], 400)
            self.assertEqual(
                self._request(port, "/api/actions/remove-issue", {"repo": "octo/example", "issue": 0})[0], 400
            )

    def test_run_view_route_renders_viewer_and_file_route_serves_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, scheduler = self._build(tmp)
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "stdout.jsonl").write_text(
                '{"type":"thread.started","thread_id":"t1"}\n', encoding="utf-8"
            )
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=1,
                issue_title="t",
                issue_url="u",
                branch_name="b",
            )
            store.update_run(run_id, run_dir=str(run_dir))
            port = self._serve(store, scheduler)

            url = f"http://{self.host}:{port}/api/run/{run_id}/view?name=stdout.jsonl"
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertIn(f"/api/run/{run_id}/file?name=stdout.jsonl", body)

            raw_url = f"http://{self.host}:{port}/api/run/{run_id}/file?name=stdout.jsonl"
            with urllib.request.urlopen(raw_url, timeout=5) as response:
                self.assertIn("thread.started", response.read().decode("utf-8"))

    def test_include_issues_batch_route_adds_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, scheduler = self._build(tmp)
            port = self._serve(store, scheduler)

            status, payload = self._request(
                port, "/api/actions/include-issues", {"repo": "octo/example", "issues": [5, 5, -1]}
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["dependency_mode"], "analyze")
            self.assertEqual(payload["added"], 1)
            self.assertEqual(payload["blocked"], 0)
            self.assertEqual(payload["requested"], 1)
            self.assertEqual(len(scheduler.dependency_extractor_spy.calls), 1)
            self.assertEqual([run["issue_number"] for run in store.list_runs()], [5])

            status, payload = self._request(
                port,
                "/api/actions/include-issues",
                {"repo": "octo/example", "issues": [6], "dependency_mode": "direct"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["dependency_mode"], "direct")
            self.assertEqual(payload["added"], 1)
            self.assertEqual(len(scheduler.dependency_extractor_spy.calls), 1)
            self.assertEqual([run["issue_number"] for run in store.list_runs()], [6, 5])

            self.assertEqual(self._request(port, "/api/actions/include-issues", {"issues": [5]})[0], 400)
            self.assertEqual(
                self._request(port, "/api/actions/include-issues", {"repo": "octo/example", "issues": []})[0], 400
            )
            self.assertEqual(
                self._request(
                    port,
                    "/api/actions/include-issues",
                    {"repo": "octo/example", "issues": [5], "dependency_mode": "bogus"},
                )[0],
                400,
            )


if __name__ == "__main__":
    unittest.main()
