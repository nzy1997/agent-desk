import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.scheduler import Scheduler
from agent_desk.store import Store


class FakeGitHub:
    def __init__(self):
        self.issues = {
            "octo/one": [
                {"number": 1, "title": "First", "body": "one", "url": "https://example.test/1"},
                {"number": 2, "title": "Second", "body": "two", "url": "https://example.test/2"},
            ],
            "octo/two": [
                {"number": 3, "title": "Third", "body": "three", "url": "https://example.test/3"},
                {"number": 4, "title": "Fourth", "body": "four", "url": "https://example.test/4"},
            ],
        }

    def list_ready_issues(self, repo, label, limit=10):
        return self.issues[repo][:limit]

    def add_label(self, repo, issue_number, label):
        raise AssertionError("label mutation should be disabled in this test")

    def remove_label(self, repo, issue_number, label):
        raise AssertionError("label mutation should be disabled in this test")


class NoopScheduler(Scheduler):
    def _run_worker_for_issue(self, **kwargs):
        return None


class SchedulerTests(unittest.TestCase):
    def test_run_available_fills_concurrency_across_repositories_without_overfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                max_concurrent_runs=3,
                repos=[
                    RepoConfig(name="octo/one", local_path=root / "one"),
                    RepoConfig(name="octo/two", local_path=root / "two"),
                ],
            )
            scheduler = NoopScheduler(config, store, github=FakeGitHub())

            results = scheduler.run_available()

            self.assertEqual(len(results), 3)
            self.assertTrue(all(result.started for result in results))
            self.assertEqual(store.dashboard_state()["stats"]["running"], 3)
            issues_by_run_order = [run["issue_number"] for run in reversed(store.list_runs())]
            self.assertEqual(issues_by_run_order, [1, 3, 2])
            self.assertEqual(scheduler.run_available(), [])

    def test_retry_uses_unique_branch_name_after_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(1, state="failed", stage="failed")
            config = AgentDeskConfig(
                data_dir=root / "data",
                max_concurrent_runs=1,
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=FakeGitHub())

            result = scheduler.run_next()
            run = store.get_run(result.run_id)

            self.assertTrue(result.started)
            self.assertEqual(run["branch_name"], "agent/issue-1-first-run-2")


if __name__ == "__main__":
    unittest.main()
