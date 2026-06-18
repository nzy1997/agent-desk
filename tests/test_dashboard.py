import json
import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.dashboard import HTML, build_state_payload
from agent_desk.scheduler import Scheduler
from agent_desk.store import Store


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
        self.assertIn("/api/run/${run.id}/file?name=", HTML)

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

    def test_dashboard_html_renders_resume_command(self):
        self.assertIn("resumeCommand(run)", HTML)
        self.assertIn("navigator.clipboard.writeText(command)", HTML)

    def test_dashboard_html_renders_manual_run_and_pr_action_buttons(self):
        self.assertIn("/api/run/${run.id}/start", HTML)
        self.assertIn("/api/run/${runId}/request-changes", HTML)
        self.assertIn("/api/run/${run.id}/approve-finish", HTML)
        self.assertIn("Approve & finish", HTML)

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

        self.assertEqual(payload["projects"], [{"name": "octo/example", "path": str(repo_path)}])
        self.assertEqual(run["project_path"], str(repo_path))
        self.assertEqual(run["project_name"], "octo/example")

    def test_dashboard_html_renders_add_project_and_folder_index(self):
        self.assertIn("addProject()", HTML)
        self.assertIn("/api/projects", HTML)
        self.assertIn("renderProjectIndex(state)", HTML)
        self.assertIn("selectProjectByPath(this)", HTML)
        self.assertIn("Back to folders", HTML)


if __name__ == "__main__":
    unittest.main()
