import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.continuation import ContinuationResult
from agent_desk.github_client import PullRequestChecksStatus
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


class RecordingGitHub(FakeGitHub):
    """FakeGitHub that records label writes instead of rejecting them."""

    def __init__(self, add_label_error: Exception | None = None):
        super().__init__()
        self.added_labels = []
        self._add_label_error = add_label_error

    def add_label(self, repo, issue_number, label):
        if self._add_label_error is not None:
            raise self._add_label_error
        self.added_labels.append((repo, issue_number, label))


class NoopScheduler(Scheduler):
    def _run_worker_for_issue(self, **kwargs):
        return None

    def _start_daemon_thread(self, target, kwargs):
        target(**kwargs)


class FakeContinuationRunner:
    def __init__(self):
        self.calls = []

    def approve_finish(self, run_id):
        self.calls.append(("approve_finish", run_id))

    def fix_ci(self, run_id, pr_status, attempt, max_attempts):
        self.calls.append((run_id, pr_status, attempt, max_attempts))

    def finish_after_ci_success(self, run_id):
        self.calls.append(("finish_after_ci_success", run_id))


class FakePullRequestGitHub(FakeGitHub):
    def __init__(self, pr_status):
        super().__init__()
        self.pr_status = pr_status
        self.pr_status_calls = []

    def pr_checks_status(self, repo, pr_url):
        self.pr_status_calls.append((repo, pr_url))
        return self.pr_status


class SequencedPullRequestGitHub(FakeGitHub):
    def __init__(self, *statuses):
        super().__init__()
        self.statuses = list(statuses)
        self.pr_status_calls = []

    def pr_checks_status(self, repo, pr_url):
        self.pr_status_calls.append((repo, pr_url))
        if len(self.statuses) == 1:
            return self.statuses[0]
        return self.statuses.pop(0)


class BlockingCloseoutContinuationRunner:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def approve_finish(self, run_id):
        self.calls.append(("approve_finish", run_id))
        return self._block(run_id)

    def finish_after_ci_success(self, run_id):
        self.calls.append(("finish_after_ci_success", run_id))
        return self._block(run_id)

    def fix_ci(self, run_id, pr_status, attempt, max_attempts):
        self.calls.append((run_id, pr_status, attempt, max_attempts))

    def _block(self, run_id):
        message = "checks are failing"
        self.store.update_run(run_id, state="blocked", stage="blocked", last_error=message)
        self.store.add_event(run_id, "warning", "auto-finish", message, {"status": "blocked"})
        return ContinuationResult(False, message, run_id)


class SchedulerTests(unittest.TestCase):
    def test_run_available_discovers_ready_runs_without_starting_workers(self):
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

            self.assertEqual(len(results), 4)
            self.assertTrue(all(not result.started for result in results))
            self.assertEqual(store.dashboard_state()["stats"]["ready"], 4)
            issues_by_run_order = [run["issue_number"] for run in reversed(store.list_runs())]
            self.assertEqual(issues_by_run_order, [1, 2, 3, 4])
            self.assertEqual(scheduler.run_available(), [])

    def test_mark_issue_ready_adds_label_and_queues_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            github = RecordingGitHub()
            scheduler = NoopScheduler(config, store, github=github)

            result = scheduler.mark_issue_ready("octo/one", 1)

            self.assertTrue(result.started)
            self.assertEqual(github.added_labels, [("octo/one", 1, "agent:ready")])
            self.assertIsNotNone(result.run_id)
            run = store.get_run(result.run_id)
            self.assertEqual(run["issue_number"], 1)
            self.assertEqual(run["state"], "ready")

    def test_mark_issue_ready_rejects_unconfigured_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            github = RecordingGitHub()
            scheduler = NoopScheduler(config, store, github=github)

            result = scheduler.mark_issue_ready("octo/missing", 7)

            self.assertFalse(result.started)
            self.assertIn("not a configured repository", result.message)
            self.assertEqual(github.added_labels, [])
            self.assertEqual(store.list_runs(), [])

    def test_mark_issue_ready_reports_github_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            github = RecordingGitHub(add_label_error=RuntimeError("label not found"))
            scheduler = NoopScheduler(config, store, github=github)

            result = scheduler.mark_issue_ready("octo/one", 1)

            self.assertFalse(result.started)
            self.assertIn("label not found", result.message)
            self.assertEqual(store.list_runs(), [])

    def test_start_run_claims_ready_issue_after_human_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                max_concurrent_runs=1,
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=FakeGitHub())
            run_id = scheduler.run_available()[0].run_id

            result = scheduler.start_run(run_id)
            run = store.get_run(run_id)

            self.assertTrue(result.started)
            self.assertEqual(run["state"], "running")
            self.assertEqual(run["stage"], "claimed")
            self.assertEqual(run["issue_body"], "one")

    def test_runtime_settings_limit_concurrent_manual_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one", max_concurrent_runs=2)],
            )
            scheduler = NoopScheduler(config, store, github=FakeGitHub())
            scheduler.update_settings(workspace_path=root / "one", max_concurrent_runs=1)
            run_ids = [result.run_id for result in scheduler.run_available()]

            first = scheduler.start_run(run_ids[0])
            second = scheduler.start_run(run_ids[1])

        self.assertTrue(first.started)
        self.assertFalse(second.started)
        self.assertEqual(second.message, "Max concurrent runs reached for workspace")

    def test_runtime_settings_default_to_one_closeout_per_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakeGitHub(),
            )

            settings = scheduler.settings_payload(root / "one")
            updated = scheduler.update_settings(workspace_path=root / "one", single_closeout_per_workspace=False)

        self.assertTrue(settings["single_closeout_per_workspace"])
        self.assertFalse(updated["single_closeout_per_workspace"])

    def test_workspace_settings_default_to_manual_single_worker_with_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakeGitHub(),
            )

            settings = scheduler.settings_payload(root / "one")

        self.assertFalse(settings["auto_start_ready"])
        self.assertEqual(settings["max_concurrent_runs"], 1)
        self.assertTrue(settings["requires_human_review"])

    def test_poll_once_auto_starts_ready_runs_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one", max_concurrent_runs=2)],
            )
            scheduler = NoopScheduler(config, store, github=FakeGitHub())
            scheduler.update_settings(workspace_path=root / "one", auto_start_ready=True)

            scheduler.poll_once()
            stats = store.dashboard_state()["stats"]

        self.assertEqual(stats["running"], 2)

    def test_workspace_parallel_limit_does_not_block_other_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[
                    RepoConfig(name="octo/one", local_path=root / "one", max_concurrent_runs=1),
                    RepoConfig(name="octo/two", local_path=root / "two", max_concurrent_runs=1),
                ],
            )
            scheduler = NoopScheduler(config, store, github=FakeGitHub())
            run_ids = [result.run_id for result in scheduler.run_available()]

            first = scheduler.start_run(run_ids[0])
            second_same_workspace = scheduler.start_run(run_ids[1])
            other_workspace = scheduler.start_run(run_ids[2])

        self.assertTrue(first.started)
        self.assertFalse(second_same_workspace.started)
        self.assertEqual(second_same_workspace.message, "Max concurrent runs reached for workspace")
        self.assertTrue(other_workspace.started)

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

    def test_monitor_prs_records_failed_ci_and_starts_auto_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
                codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
                worktree_path=str(root / "worktree"),
            )
            pr_status = PullRequestChecksStatus(
                state="failure",
                summary="1 failed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "FAILURE"}],
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
            )

            scheduler.monitor_prs()
            run = store.get_run(run_id)

        self.assertEqual(run["pr_ci_status"], "failure")
        self.assertEqual(run["pr_ci_summary"], "1 failed")
        self.assertEqual(run["ci_fix_attempts"], 1)
        self.assertEqual(run["ci_fix_last_sha"], "abc123")
        self.assertEqual(run["state"], "running")
        self.assertEqual(run["stage"], "auto-fixing ci (1/3)")
        self.assertEqual(continuation.calls, [(run_id, pr_status, 1, 3)])

    def test_monitor_prs_auto_finishes_successful_ci_when_human_review_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
                codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
                worktree_path=str(root / "worktree"),
            )
            pr_status = PullRequestChecksStatus(
                state="success",
                summary="2 passed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "SUCCESS"}],
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
            )
            scheduler.update_settings(workspace_path=root / "one", requires_human_review=False)

            scheduler.monitor_prs()
            run = store.get_run(run_id)

        self.assertEqual(run["pr_ci_status"], "success")
        self.assertEqual(run["state"], "running")
        self.assertEqual(run["stage"], "auto-finishing after ci success")
        self.assertEqual(continuation.calls, [("finish_after_ci_success", run_id)])

    def test_auto_finish_blocked_by_late_failing_checks_starts_auto_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
                codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
                worktree_path=str(root / "worktree"),
            )
            initial_success = PullRequestChecksStatus(
                state="success",
                summary="3 passed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "SUCCESS"}],
            )
            late_failure = PullRequestChecksStatus(
                state="failure",
                summary="2 failed, 3 passed",
                head_sha="abc123",
                checks=[
                    {"name": "codecov/patch", "state": "FAILURE"},
                    {"name": "codecov/project", "state": "FAILURE"},
                ],
            )
            github = SequencedPullRequestGitHub(initial_success, late_failure)
            continuation = BlockingCloseoutContinuationRunner(store)
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=github,
                continuation_factory=lambda config, store: continuation,
            )
            scheduler.update_settings(workspace_path=root / "one", requires_human_review=False)

            scheduler.monitor_prs()
            run = store.get_run(run_id)

        self.assertEqual(github.pr_status_calls, [("octo/one", "https://github.com/octo/one/pull/9")] * 2)
        self.assertEqual(run["state"], "running")
        self.assertEqual(run["stage"], "auto-fixing ci (1/3)")
        self.assertEqual(run["ci_fix_attempts"], 1)
        self.assertEqual(
            continuation.calls,
            [
                ("finish_after_ci_success", run_id),
                (run_id, late_failure, 1, 3),
            ],
        )

    def test_monitor_prs_allows_only_one_auto_finish_per_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            first_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            second_id = store.create_run(
                repo_name="octo/one",
                issue_number=2,
                issue_title="Second",
                issue_url="https://example.test/2",
                branch_name="agent/issue-2-second-run-1",
            )
            for run_id, pr_number in ((first_id, 9), (second_id, 10)):
                store.update_run(
                    run_id,
                    state="pr_open",
                    stage="pull request opened",
                    pr_url=f"https://github.com/octo/one/pull/{pr_number}",
                    codex_thread_id=f"thread-{run_id}",
                    worktree_path=str(root / f"worktree-{run_id}"),
                )
            pr_status = PullRequestChecksStatus(
                state="success",
                summary="2 passed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "SUCCESS"}],
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
            )
            scheduler.update_settings(workspace_path=root / "one", requires_human_review=False)

            results = scheduler.monitor_prs()
            first = store.get_run(first_id)
            second = store.get_run(second_id)

        self.assertEqual(sum(1 for run in (first, second) if run["state"] == "running"), 1)
        self.assertEqual(sum(1 for run in (first, second) if run["state"] == "pr_open"), 1)
        self.assertEqual(len([result for result in results if result.started]), 1)
        self.assertEqual(len(continuation.calls), 1)
        self.assertTrue(any("Closeout already in progress" in result.message for result in results))

    def test_approve_finish_allows_only_one_closeout_per_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            active_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            candidate_id = store.create_run(
                repo_name="octo/one",
                issue_number=2,
                issue_title="Second",
                issue_url="https://example.test/2",
                branch_name="agent/issue-2-second-run-1",
            )
            store.update_run(active_id, state="running", stage="approve-finish")
            store.update_run(
                candidate_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/10",
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakeGitHub(),
                continuation_factory=lambda config, store: continuation,
            )

            result = scheduler.approve_finish(candidate_id)
            candidate = store.get_run(candidate_id)

        self.assertFalse(result.started)
        self.assertIn("Closeout already in progress", result.message)
        self.assertEqual(candidate["state"], "pr_open")
        self.assertEqual(candidate["stage"], "pull request opened")
        self.assertEqual(continuation.calls, [])

    def test_approve_finish_allows_parallel_closeout_when_workspace_lock_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            active_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            candidate_id = store.create_run(
                repo_name="octo/one",
                issue_number=2,
                issue_title="Second",
                issue_url="https://example.test/2",
                branch_name="agent/issue-2-second-run-1",
            )
            store.update_run(active_id, state="running", stage="approve-finish")
            store.update_run(
                candidate_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/10",
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakeGitHub(),
                continuation_factory=lambda config, store: continuation,
            )
            scheduler.update_settings(workspace_path=root / "one", single_closeout_per_workspace=False)

            result = scheduler.approve_finish(candidate_id)
            candidate = store.get_run(candidate_id)

        self.assertTrue(result.started)
        self.assertEqual(candidate["state"], "running")
        self.assertEqual(candidate["stage"], "approve-finish queued")
        self.assertEqual(continuation.calls, [("approve_finish", candidate_id)])

    def test_monitor_prs_blocks_failed_ci_after_three_auto_fix_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="ci fix pushed",
                pr_url="https://github.com/octo/one/pull/9",
                ci_fix_attempts=3,
            )
            pr_status = PullRequestChecksStatus(
                state="failure",
                summary="1 failed",
                head_sha="def456",
                checks=[{"name": "unit", "state": "FAILURE"}],
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
            )

            scheduler.monitor_prs()
            run = store.get_run(run_id)

        self.assertEqual(run["state"], "blocked")
        self.assertEqual(run["stage"], "ci failed after auto-fix limit")
        self.assertIn("CI failed after 3 automatic fix attempts", run["last_error"])
        self.assertEqual(continuation.calls, [])

    def test_poll_once_blocks_unconfigured_ready_run_and_still_monitors_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            stale_run_id = store.create_run(
                repo_name="octo/stale",
                issue_number=99,
                issue_title="Old queued work",
                issue_url="https://example.test/stale/99",
                branch_name="agent/issue-99-old-queued-work",
            )
            store.update_run(stale_run_id, state="ready", stage="waiting for human run")
            pr_run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                pr_run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
                codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
                worktree_path=str(root / "worktree"),
            )
            pr_status = PullRequestChecksStatus(
                state="failure",
                summary="1 failed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "FAILURE"}],
            )
            github = FakePullRequestGitHub(pr_status)
            github.issues = {"octo/one": []}
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=github,
                continuation_factory=lambda config, store: continuation,
            )
            scheduler.update_settings(workspace_path=root / "one", auto_start_ready=True, max_concurrent_runs=1)

            scheduler.poll_once()
            stale_run = store.get_run(stale_run_id)
            pr_run = store.get_run(pr_run_id)

        self.assertEqual(stale_run["state"], "blocked")
        self.assertEqual(stale_run["stage"], "blocked")
        self.assertIn("repository octo/stale is not configured", stale_run["last_error"])
        self.assertEqual(pr_run["pr_ci_status"], "failure")
        self.assertEqual(pr_run["stage"], "auto-fixing ci (1/3)")
        self.assertEqual(continuation.calls, [(pr_run_id, pr_status, 1, 3)])


if __name__ == "__main__":
    unittest.main()
