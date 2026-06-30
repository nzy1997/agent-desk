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

    def test_interrupted_is_terminal_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=42,
                issue_title="Fix queue",
                issue_url="https://github.com/octo/example/issues/42",
                branch_name="agent/issue-42-fix-queue",
            )
            store.update_run(run_id, state="running", stage="running codex")
            store.update_run(run_id, state="interrupted", stage="interrupted by shutdown")

            run = store.get_run(run_id)
            state = store.dashboard_state()

        self.assertEqual(run["state"], "interrupted")
        self.assertEqual(run["stage"], "interrupted by shutdown")
        self.assertTrue(run["ended_at"])
        self.assertEqual(state["stats"]["interrupted"], 1)


    def test_state_is_traced_by_folder_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = Store(base / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=42,
                issue_title="Fix queue",
                issue_url="u",
                branch_name="b",
            )
            repo_dir = base / "state" / "octo__example"
            self.assertTrue((repo_dir / "queued" / f"{run_id}.json").exists())

            store.update_run(run_id, state="running")
            self.assertFalse((repo_dir / "queued" / f"{run_id}.json").exists())
            self.assertTrue((repo_dir / "running" / f"{run_id}.json").exists())

            store.update_run(run_id, state="done")
            self.assertFalse((repo_dir / "running" / f"{run_id}.json").exists())
            self.assertTrue((repo_dir / "done" / f"{run_id}.json").exists())
            self.assertEqual(store.get_run(run_id)["state"], "done")

    def test_available_records_are_intake_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            avail_id = store.create_available(
                repo_name="octo/example",
                issue_number=7,
                issue_title="Synced",
                issue_url="u7",
                issue_body="body text",
            )
            # Available records do not count as runs or open runs.
            self.assertEqual(store.list_runs(), [])
            self.assertIsNone(store.find_open_run("octo/example", 7))
            # But they show up in the picker view with their body.
            records = store.list_records("octo/example")
            self.assertEqual([r["issue_number"] for r in records], [7])
            self.assertEqual(records[0]["issue_body"], "body text")

            # Moving it onto the desk makes it a ready run.
            store.update_run(avail_id, state="ready", branch_name="agent/issue-7")
            self.assertEqual([r["state"] for r in store.list_runs()], ["ready"])
            self.assertIsNotNone(store.find_open_run("octo/example", 7))
            self.assertEqual(store.list_records("octo/example")[0]["state"], "ready")

    def test_next_attempt_increments_per_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            self.assertEqual(store.next_attempt("octo/example", 5), 1)
            first = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="t",
                issue_url="u",
                branch_name="b1",
            )
            store.update_run(first, state="failed")
            self.assertEqual(store.next_attempt("octo/example", 5), 2)
            # A finished/failed run is not an open run.
            self.assertIsNone(store.find_open_run("octo/example", 5))

    def test_counter_stays_monotonic_under_concurrent_threads(self):
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            results: list[int] = []
            lock = threading.Lock()

            def grab():
                value = store._next("widget")
                with lock:
                    results.append(value)

            threads = [threading.Thread(target=grab) for _ in range(25)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # File lock + in-process lock guarantee no duplicate or skipped ids.
            self.assertEqual(sorted(results), list(range(1, 26)))

    def test_next_falls_back_without_fcntl(self):
        import agent_desk.store as store_module

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            original = store_module.fcntl
            store_module.fcntl = None
            try:
                self.assertEqual(store._next("gadget"), 1)
                self.assertEqual(store._next("gadget"), 2)
            finally:
                store_module.fcntl = original


if __name__ == "__main__":
    unittest.main()
