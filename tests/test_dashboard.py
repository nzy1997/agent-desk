import json
import tempfile
import unittest
from pathlib import Path

from agent_desk.dashboard import HTML, build_state_payload
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


if __name__ == "__main__":
    unittest.main()
