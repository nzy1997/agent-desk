import tempfile
import unittest
from pathlib import Path

from agent_desk.store import Store


class StoreTests(unittest.TestCase):
    def test_records_run_and_events_for_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=42,
                issue_title="Fix queue",
                issue_url="https://github.com/octo/example/issues/42",
                branch_name="agent/issue-42-fix-queue",
            )
            store.update_run(run_id, state="running", stage="testing", worktree_path="/tmp/wt")
            store.add_event(run_id, "info", "stage", "Running tests", {"stage": "testing"})

            state = store.dashboard_state()

        self.assertEqual(state["stats"]["running"], 1)
        self.assertEqual(state["runs"][0]["id"], run_id)
        self.assertEqual(state["runs"][0]["stage"], "testing")
        self.assertEqual(state["runs"][0]["worktree_path"], "/tmp/wt")
        self.assertEqual(state["events"][0]["message"], "Running tests")
        self.assertEqual(state["events"][0]["payload"]["stage"], "testing")

    def test_records_pr_ci_status_and_auto_fix_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=42,
                issue_title="Fix queue",
                issue_url="https://github.com/octo/example/issues/42",
                branch_name="agent/issue-42-fix-queue",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/example/pull/9",
                pr_ci_status="failure",
                pr_ci_summary="1 failed",
                pr_ci_checked_at="2026-06-18T00:00:00+00:00",
                ci_fix_attempts=2,
                ci_fix_last_sha="abc123",
            )

            run = store.dashboard_state()["runs"][0]

        self.assertEqual(run["pr_ci_status"], "failure")
        self.assertEqual(run["pr_ci_summary"], "1 failed")
        self.assertEqual(run["ci_fix_attempts"], 2)
        self.assertEqual(run["ci_fix_last_sha"], "abc123")


if __name__ == "__main__":
    unittest.main()
