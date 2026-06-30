import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.continuation import ContinuationResult
from agent_desk.dependencies import Dependency, DependencyGraph, IssueDependencies
from agent_desk.github_client import PullRequestChecksStatus
from agent_desk.scheduler import Scheduler
from agent_desk.shutdown import ProcessInfo
from agent_desk.store import Store
from agent_desk.worker import CommandResult, FakeCommandRunner, Worker


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

    def list_open_issues(self, repo, limit=200):
        return [{**issue, "labels": []} for issue in self.issues.get(repo, [])][:limit]

    def get_issue(self, repo, issue_number):
        for issue in self.issues.get(repo, []):
            if int(issue["number"]) == issue_number:
                return issue
        return {"number": issue_number, "title": f"Issue {issue_number}", "body": "", "url": ""}

    def add_label(self, repo, issue_number, label):
        pass

    def remove_label(self, repo, issue_number, label):
        pass


def queue_ready(scheduler, repo_name, numbers):
    """Test helper: sync a repo's issues to disk, then move the given ones onto the desk."""
    scheduler.sync_repo_issues(repo_name)
    return [scheduler.mark_issue_ready(repo_name, number).run_id for number in numbers]


class RecordingGitHub(FakeGitHub):
    """FakeGitHub that records label writes instead of rejecting them."""

    def __init__(self, add_label_error: Exception | None = None):
        super().__init__()
        self.added_labels = []
        self.removed_labels = []
        self._add_label_error = add_label_error

    def add_label(self, repo, issue_number, label):
        if self._add_label_error is not None:
            raise self._add_label_error
        self.added_labels.append((repo, issue_number, label))

    def remove_label(self, repo, issue_number, label):
        self.removed_labels.append((repo, issue_number, label))


class OpenIssueGitHub(RecordingGitHub):
    """Serves open issues with labels for list_repo_issues tests."""

    def __init__(self, open_issues):
        super().__init__()
        self._open_issues = open_issues

    def list_open_issues(self, repo, limit=50):
        return self._open_issues.get(repo, [])[:limit]


class NoopScheduler(Scheduler):
    def _run_worker_for_issue(self, **kwargs):
        return None

    def _start_daemon_thread(self, target, kwargs):
        target(**kwargs)


class RecordingDispatchScheduler(Scheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dispatched = []

    def _start_daemon_thread(self, target, kwargs):
        self.dispatched.append((target.__name__, kwargs))


class FakeShutdownController:
    def __init__(self, infos):
        self.infos = infos
        self.terminated = []
        self.killed = []
        self.alive = {}

    def process_info(self, pid):
        return self.infos.get(pid)

    def process_group(self, pgid):
        return [info for info in self.infos.values() if info.pgid == pgid]

    def terminate_group(self, pgid):
        self.terminated.append(pgid)

    def kill_group(self, pgid):
        self.killed.append(pgid)

    def pid_alive(self, pid):
        return self.alive.get(pid, False)


class FailingSpawnScheduler(Scheduler):
    def _spawn_detached_job(self, run_id: int, kind: str) -> None:
        raise RuntimeError(f"spawn failed for {kind}")


class SpawnRecordingScheduler(Scheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spawned = []

    def _spawn_detached_job(self, run_id: int, kind: str) -> None:
        self.spawned.append({"run_id": run_id, "kind": kind})


class RecordingDependencyExtractor:
    def __init__(self, graph: DependencyGraph):
        self.graph = graph
        self.calls = []

    def __call__(self, repo_name, issues):
        self.calls.append((repo_name, issues))
        return self.graph


class FakeContinuationRunner:
    def __init__(self):
        self.calls = []

    def request_changes(self, run_id, feedback):
        self.calls.append(("request_changes", run_id, feedback))

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


class TerminalWorker:
    def __init__(self, store, state: str):
        self.store = store
        self.state = state

    def run_issue(self, *, run_id, **kwargs):
        self.store.update_run(
            run_id,
            state=self.state,
            stage=self.state,
            pr_url="https://example.test/pr/1",
        )


class SchedulerTests(unittest.TestCase):
    def test_sync_then_add_queues_ready_runs_without_starting_workers(self):
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

            queue_ready(scheduler, "octo/one", [1, 2])
            queue_ready(scheduler, "octo/two", [3, 4])

            self.assertEqual(store.dashboard_state()["stats"]["ready"], 4)
            issues_by_run_order = sorted(run["issue_number"] for run in store.list_runs())
            self.assertEqual(issues_by_run_order, [1, 2, 3, 4])
            # Syncing again does not duplicate records.
            scheduler.sync_repo_issues("octo/one")
            self.assertEqual(store.dashboard_state()["stats"]["ready"], 4)

    def test_list_repo_issues_flags_on_desk_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            github = OpenIssueGitHub(
                {
                    "octo/one": [
                        {"number": 10, "title": "Fresh", "url": "u10", "body": "b10", "labels": []},
                        {"number": 11, "title": "Added", "url": "u11", "body": "b11", "labels": []},
                        {"number": 12, "title": "Running", "url": "u12", "body": "b12", "labels": []},
                    ]
                }
            )
            scheduler = NoopScheduler(config, store, github=github)
            scheduler.sync_repo_issues("octo/one")
            # Move two issues onto the desk; #10 stays available.
            scheduler.mark_issue_ready("octo/one", 11)
            scheduler.mark_issue_ready("octo/one", 12)

            issues = scheduler.list_repo_issues("octo/one")

            on_desk = {issue["number"]: issue["on_desk"] for issue in issues}
            self.assertEqual(on_desk, {10: False, 11: True, 12: True})
            self.assertEqual({i["number"]: i["body"] for i in issues}[10], "b10")

    def test_list_repo_issues_rejects_unconfigured_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=OpenIssueGitHub({}))

            with self.assertRaises(KeyError):
                scheduler.list_repo_issues("octo/missing")

    def test_mark_issue_ready_queues_run_without_touching_github_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            github = RecordingGitHub()
            scheduler = NoopScheduler(config, store, github=github)
            scheduler.sync_repo_issues("octo/one")

            result = scheduler.mark_issue_ready("octo/one", 1)

            self.assertTrue(result.started)
            # Adding to the desk is a pure local file move; no label is written.
            self.assertEqual(github.added_labels, [])
            run = store.get_run(result.run_id)
            self.assertEqual(run["issue_number"], 1)
            self.assertEqual(run["state"], "ready")

    def test_remove_issue_from_desk_returns_ready_issue_to_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=RecordingGitHub())
            scheduler.sync_repo_issues("octo/one")
            add_result = scheduler.mark_issue_ready("octo/one", 1)

            result = scheduler.remove_issue_from_desk("octo/one", 1)

            self.assertTrue(result.started)
            self.assertEqual(result.run_id, add_result.run_id)
            record = store.get_record("octo/one", 1)
            self.assertEqual(record["state"], "available")
            self.assertEqual(record["stage"], "")
            self.assertEqual(record["branch_name"], "")
            self.assertEqual(record["dependencies"], [])
            self.assertEqual(record["blocked_by"], [])
            self.assertEqual(store.list_runs(), [])
            issues = {issue["number"]: issue for issue in scheduler.list_repo_issues("octo/one")}
            self.assertFalse(issues[1]["on_desk"])

    def test_remove_issue_from_desk_rejects_active_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=RecordingGitHub())
            scheduler.sync_repo_issues("octo/one")
            run_id = scheduler.mark_issue_ready("octo/one", 1).run_id
            store.update_run(run_id, state="running", stage="running")

            result = scheduler.remove_issue_from_desk("octo/one", 1)

            self.assertFalse(result.started)
            self.assertIn("cannot be removed", result.message)
            self.assertEqual(store.get_record("octo/one", 1)["state"], "running")

    def test_mark_issues_ready_direct_mode_queues_every_selected_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=RecordingGitHub())
            scheduler.sync_repo_issues("octo/one")

            results = scheduler.mark_issues_ready("octo/one", [1, 2], dependency_mode="direct")

            self.assertEqual([result.run_id for result in results], [1, 2])
            self.assertEqual({run["issue_number"]: run["state"] for run in store.list_runs()}, {1: "ready", 2: "ready"})

    def test_mark_issues_ready_analyze_mode_blocks_dependent_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            graph = DependencyGraph(
                repo="octo/one",
                issues=[
                    IssueDependencies(number=1, depends_on=[]),
                    IssueDependencies(
                        number=2,
                        depends_on=[
                            Dependency(
                                repo="octo/one",
                                number=1,
                                evidence="Depends on #1",
                                confidence="high",
                            )
                        ],
                    ),
                ],
                warnings=[],
            )
            extractor = RecordingDependencyExtractor(graph)
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=RecordingGitHub(), dependency_extractor=extractor)
            scheduler.sync_repo_issues("octo/one")

            scheduler.mark_issues_ready("octo/one", [1, 2], dependency_mode="analyze")

            self.assertEqual(extractor.calls[0][0], "octo/one")
            self.assertEqual([issue["number"] for issue in extractor.calls[0][1]], [1, 2])
            records = {record["issue_number"]: record for record in store.list_records("octo/one")}
            self.assertEqual(records[1]["state"], "ready")
            self.assertEqual(records[1]["dependency_state"], "ready")
            self.assertEqual(records[2]["state"], "blocked")
            self.assertEqual(records[2]["stage"], "waiting for dependencies")
            self.assertEqual(records[2]["dependency_state"], "blocked")
            self.assertEqual(records[2]["blocked_by"][0]["number"], 1)

    def test_poll_once_unlocks_blocked_issue_after_dependency_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            graph = DependencyGraph(
                repo="octo/one",
                issues=[
                    IssueDependencies(number=1, depends_on=[]),
                    IssueDependencies(
                        number=2,
                        depends_on=[Dependency(repo="octo/one", number=1, evidence="Depends on #1", confidence="high")],
                    ),
                ],
                warnings=[],
            )
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(
                config,
                store,
                github=RecordingGitHub(),
                dependency_extractor=RecordingDependencyExtractor(graph),
            )
            scheduler.sync_repo_issues("octo/one")
            scheduler.mark_issues_ready("octo/one", [1, 2], dependency_mode="analyze")
            first = store.get_record("octo/one", 1)
            second = store.get_record("octo/one", 2)
            store.update_run(first["id"], state="done", stage="done")

            scheduler.poll_once()

            unlocked = store.get_run(second["id"])
            self.assertEqual(unlocked["state"], "ready")
            self.assertEqual(unlocked["stage"], "waiting for human run")
            self.assertEqual(unlocked["dependency_state"], "ready")
            self.assertEqual(unlocked["blocked_by"], [])

    def test_default_dependency_extractor_invokes_codex_with_fixed_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex"],
                        0,
                        '{"repo":"octo/one","issues":[{"number":2,"depends_on":[{"repo":"octo/one","number":1,"evidence":"Depends on #1","confidence":"high"}],"notes":""}],"warnings":[]}',
                        "",
                    )
                ]
            )
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            worker = Worker(config, store, runner)
            scheduler = NoopScheduler(config, store, github=RecordingGitHub(), worker=worker)

            graph = scheduler._extract_dependencies_with_codex(
                "octo/one",
                [{"number": 2, "title": "Second", "body": "Depends on #1", "url": "u2"}],
            )

            call = runner.calls[0]
            self.assertIn("codex", call.argv)
            self.assertIn("--output-last-message", call.argv)
            self.assertIn("You are Agent Desk's dependency extractor.", call.stdin)
            self.assertEqual(graph.issues[0].depends_on[0].number, 1)

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
            run_id = queue_ready(scheduler, "octo/one", [1])[0]

            result = scheduler.start_run(run_id)
            run = store.get_run(run_id)

            self.assertTrue(result.started)
            self.assertEqual(run["state"], "running")
            self.assertEqual(run["stage"], "claimed")
            self.assertEqual(run["issue_body"], "one")

    def test_start_run_does_not_touch_github_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                max_concurrent_runs=1,
                repos=[RepoConfig(name="octo/one", local_path=root / "one", mutate_github=True)],
            )
            github = RecordingGitHub()
            scheduler = NoopScheduler(config, store, github=github)
            run_id = queue_ready(scheduler, "octo/one", [1])[0]

            result = scheduler.start_run(run_id)

            self.assertTrue(result.started)
            self.assertEqual(github.added_labels, [])
            self.assertEqual(github.removed_labels, [])

    def test_request_changes_dispatches_detached_job_with_persisted_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = RecordingDispatchScheduler(config, store, github=FakeGitHub())
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1",
            )
            store.update_run(run_id, state="pr_open", pr_url="https://example.test/pr/1")

            result = scheduler.request_changes(run_id, "please tighten the tests")

            run = store.get_run(run_id)
            self.assertTrue(result.started)
            self.assertEqual(run["state"], "running")
            self.assertEqual(run["stage"], "request-changes queued")
            self.assertEqual(run["request_changes_feedback"], "please tighten the tests")
            self.assertEqual(scheduler.dispatched, [("_run_request_changes", {"run_id": run_id})])

    def test_shutdown_preview_lists_running_runs_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "data" / "runs" / "issue-1" / "run-1"
            run_dir.mkdir(parents=True)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(config, store, github=FakeGitHub())
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1",
            )
            store.update_run(
                run_id,
                state="running",
                stage="running codex",
                run_dir=str(run_dir),
                worktree_path=str(root / "worktree"),
                codex_thread_id="thread",
                supervisor_pid=111,
            )
            controller = FakeShutdownController(
                {
                    111: ProcessInfo(
                        pid=111,
                        ppid=1,
                        pgid=111,
                        command=(
                            "python -m agent_desk run-job --config config/repos.toml "
                            f"--run-id {run_id} --kind issue"
                        ),
                    ),
                    112: ProcessInfo(pid=112, ppid=111, pgid=111, command="codex exec"),
                }
            )

            preview = scheduler.shutdown_preview(controller=controller)

            self.assertEqual(preview["running_count"], 1)
            self.assertEqual(preview["runs"][0]["run_id"], run_id)
            self.assertTrue(preview["runs"][0]["killable"])
            self.assertEqual(store.get_run(run_id)["state"], "running")

    def test_shutdown_all_marks_running_runs_interrupted_and_records_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "data" / "runs" / "issue-1" / "run-1"
            run_dir.mkdir(parents=True)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = NoopScheduler(
                config,
                store,
                github=FakeGitHub(),
                config_path=root / "repos.toml",
            )
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1",
            )
            store.update_run(
                run_id,
                state="running",
                stage="running codex",
                run_dir=str(run_dir),
                worktree_path=str(root / "worktree"),
                codex_thread_id="thread",
                supervisor_pid=111,
            )
            controller = FakeShutdownController(
                {
                    111: ProcessInfo(
                        pid=111,
                        ppid=1,
                        pgid=111,
                        command=(
                            "python -m agent_desk run-job --config config/repos.toml "
                            f"--run-id {run_id} --kind issue"
                        ),
                    )
                }
            )
            controller.alive = {111: True}

            result = scheduler.shutdown_all(controller=controller, dashboard_pid=999, grace_seconds=0)
            run = store.get_run(run_id)

            self.assertEqual(run["state"], "interrupted")
            self.assertEqual(run["stage"], "interrupted by shutdown")
            self.assertIn("Interrupted by user shutdown", run["last_error"])
            self.assertTrue(
                any(event["event_type"] == "shutdown-interrupted" for event in run["events"])
            )
            self.assertEqual(controller.terminated, [111])
            self.assertEqual(controller.killed, [111])
            self.assertTrue(Path(result["manifest_path"]).exists())
            self.assertTrue((run_dir / f"shutdown-{result['shutdown_id']}.json").exists())
            self.assertEqual(result["signal_results"][0]["result"], "killed")

    def test_resume_interrupted_dispatches_detached_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=5,
                issue_title="Shutdown",
                issue_url="u5",
                branch_name="b5",
            )
            worktree = root / "worktree"
            worktree.mkdir()
            store.update_run(
                run_id,
                state="interrupted",
                stage="interrupted by shutdown",
                codex_thread_id="thread",
                worktree_path=str(worktree),
                supervisor_pid=222,
            )
            scheduler = SpawnRecordingScheduler(
                AgentDeskConfig(
                    data_dir=root / "data",
                    repos=[RepoConfig(name="octo/one", local_path=root / "one")],
                ),
                store,
                github=FakeGitHub(),
                config_path=root / "repos.toml",
                detach_jobs=True,
            )

            result = scheduler.resume_interrupted(run_id)

            self.assertTrue(result.started)
            self.assertEqual(store.get_run(run_id)["state"], "running")
            self.assertEqual(store.get_run(run_id)["stage"], "resume-interrupted queued")
            self.assertEqual(store.get_run(run_id)["ended_at"], "")
            self.assertEqual(store.get_run(run_id)["supervisor_pid"], "")
            self.assertEqual(scheduler.spawned[-1]["kind"], "resume-interrupted")

    def test_worker_completion_does_not_touch_github_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                max_concurrent_runs=1,
                repos=[RepoConfig(name="octo/one", local_path=root / "one", mutate_github=True)],
            )
            github = RecordingGitHub()
            scheduler = Scheduler(
                config,
                store,
                github=github,
                worker=TerminalWorker(store, "pr_open"),
            )
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1",
            )
            store.update_run(run_id, state="running")

            scheduler._run_worker_for_issue(
                run_id=run_id,
                repo=config.repos[0],
                issue_number=1,
                issue_title="First",
                issue_body="one",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1",
            )

            self.assertEqual(store.get_run(run_id)["state"], "pr_open")
            self.assertEqual(github.added_labels, [])
            self.assertEqual(github.removed_labels, [])

    def test_start_run_marks_failed_when_detached_supervisor_spawn_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            config = AgentDeskConfig(
                data_dir=root / "data",
                max_concurrent_runs=1,
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            )
            scheduler = FailingSpawnScheduler(
                config,
                store,
                github=FakeGitHub(),
                config_path=root / "repos.toml",
                detach_jobs=True,
            )
            run_id = queue_ready(scheduler, "octo/one", [1])[0]

            result = scheduler.start_run(run_id)
            run = store.get_run(run_id)

            self.assertFalse(result.started)
            self.assertIn("Failed to start supervisor", result.message)
            self.assertIn("spawn failed for issue", result.message)
            self.assertEqual(run["state"], "failed")
            self.assertEqual(run["stage"], "failed")
            self.assertIn("spawn failed for issue", run["last_error"])
            self.assertFalse(run.get("supervisor_pid"))
            self.assertTrue(any(event["event_type"] == "spawn-failed" for event in run["events"]))

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
            run_ids = queue_ready(scheduler, "octo/one", [1, 2])

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

    def test_workspace_settings_reset_auto_start_to_false_on_scheduler_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = NoopScheduler(
                AgentDeskConfig(
                    data_dir=root / "data",
                    repos=[
                        RepoConfig(
                            name="octo/one",
                            local_path=root / "one",
                            auto_start_ready=True,
                            max_concurrent_runs=4,
                            requires_human_review=False,
                        )
                    ],
                ),
                store,
                github=FakeGitHub(),
            )

            settings = scheduler.settings_payload(root / "one")

        self.assertFalse(settings["auto_start_ready"])
        self.assertEqual(settings["max_concurrent_runs"], 4)
        self.assertFalse(settings["requires_human_review"])

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
            queue_ready(scheduler, "octo/one", [1, 2])

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
            run_ids = queue_ready(scheduler, "octo/one", [1, 2]) + queue_ready(scheduler, "octo/two", [3])

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

            # Re-adding a failed issue to the desk creates a fresh run with a new branch.
            result = scheduler.mark_issue_ready("octo/one", 1)
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


class RecordingWorker:
    """Captures the kwargs run_job reconstructs for the issue job."""

    def __init__(self):
        self.calls = []

    def run_issue(self, **kwargs):
        self.calls.append(kwargs)


class DetachedJobTests(unittest.TestCase):
    def _config(self, root: Path) -> AgentDeskConfig:
        return AgentDeskConfig(
            data_dir=root / "data",
            repos=[RepoConfig(name="octo/one", local_path=root / "one")],
        )

    def test_run_job_issue_reconstructs_worker_kwargs_from_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            worker = RecordingWorker()
            scheduler = Scheduler(self._config(root), store, github=FakeGitHub(), worker=worker)
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=7,
                issue_title="Title",
                issue_url="https://example.test/7",
                branch_name="agent/issue-7",
                issue_body="body text",
            )

            scheduler.run_job(run_id, "issue")

            self.assertEqual(len(worker.calls), 1)
            call = worker.calls[0]
            self.assertEqual(call["issue_number"], 7)
            self.assertEqual(call["issue_title"], "Title")
            self.assertEqual(call["issue_body"], "body text")
            self.assertEqual(call["branch_name"], "agent/issue-7")
            self.assertEqual(call["repo"].name, "octo/one")

    def test_run_job_ci_fix_refetches_pr_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            pr_status = PullRequestChecksStatus(state="failure", summary="boom", checks=[], head_sha="abc")
            github = FakePullRequestGitHub(pr_status)
            continuation = FakeContinuationRunner()
            scheduler = Scheduler(
                self._config(root),
                store,
                github=github,
                continuation_factory=lambda config, store: continuation,
            )
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=8,
                issue_title="T",
                issue_url="u8",
                branch_name="agent/issue-8",
            )
            store.update_run(run_id, pr_url="https://example.test/pr/8", ci_fix_attempts=2)

            scheduler.run_job(run_id, "ci-fix")

            self.assertEqual(github.pr_status_calls, [("octo/one", "https://example.test/pr/8")])
            self.assertEqual(continuation.calls, [(run_id, pr_status, 2, 3)])

    def test_run_job_request_changes_uses_stored_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            continuation = FakeContinuationRunner()
            scheduler = Scheduler(
                self._config(root),
                store,
                github=FakeGitHub(),
                continuation_factory=lambda config, store: continuation,
            )
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=8,
                issue_title="T",
                issue_url="u8",
                branch_name="agent/issue-8",
            )
            store.update_run(run_id, request_changes_feedback="please tighten the tests")

            scheduler.run_job(run_id, "request-changes")

            self.assertEqual(continuation.calls, [("request_changes", run_id, "please tighten the tests")])

    def test_run_job_closeout_kinds_call_continuation(self):
        for kind, expected in [
            ("approve-finish", "approve_finish"),
            ("auto-finish", "finish_after_ci_success"),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(root / "desk.sqlite")
                continuation = FakeContinuationRunner()
                scheduler = Scheduler(
                    self._config(root),
                    store,
                    github=FakeGitHub(),
                    continuation_factory=lambda config, store: continuation,
                )
                run_id = store.create_run(
                    repo_name="octo/one",
                    issue_number=20,
                    issue_title="T",
                    issue_url="u20",
                    branch_name="b",
                )
                scheduler.run_job(run_id, kind)
                self.assertEqual(continuation.calls, [(expected, run_id)])

    def test_spawn_detached_job_requires_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = Scheduler(self._config(root), store, github=FakeGitHub(), detach_jobs=True)
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=21,
                issue_title="T",
                issue_url="u21",
                branch_name="b",
            )
            with self.assertRaises(RuntimeError):
                scheduler._spawn_detached_job(run_id, "issue")

    def test_run_job_rejects_unknown_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = Scheduler(self._config(root), store, github=FakeGitHub())
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=9,
                issue_title="T",
                issue_url="u9",
                branch_name="b",
            )
            with self.assertRaises(ValueError):
                scheduler.run_job(run_id, "nope")

    def test_dispatch_spawns_detached_job_and_records_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = Scheduler(
                self._config(root),
                store,
                github=FakeGitHub(),
                config_path=root / "repos.toml",
                detach_jobs=True,
            )
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=5,
                issue_title="T",
                issue_url="u5",
                branch_name="b",
            )
            spawned = {}

            def fake_popen(argv, **kwargs):
                spawned["argv"] = argv
                spawned["kwargs"] = kwargs

                class _Proc:
                    pid = 4321

                return _Proc()

            import agent_desk.scheduler as scheduler_module

            original = scheduler_module.subprocess.Popen
            scheduler_module.subprocess.Popen = fake_popen
            try:
                scheduler._start_daemon_thread(scheduler._run_worker_for_issue, {"run_id": run_id})
            finally:
                scheduler_module.subprocess.Popen = original

            self.assertIn("run-job", spawned["argv"])
            self.assertIn("issue", spawned["argv"])
            self.assertTrue(spawned["kwargs"]["start_new_session"])
            self.assertEqual(store.get_run(run_id)["supervisor_pid"], 4321)
            self.assertEqual(store.get_run(run_id)["run_dir"], str(scheduler.config.data_dir / "runs" / "issue-5" / "run-1"))

    def test_reconcile_orphans_fails_dead_runs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = Scheduler(self._config(root), store, github=FakeGitHub())
            alive = store.create_run(
                repo_name="octo/one", issue_number=1, issue_title="a", issue_url="u1", branch_name="b1"
            )
            dead = store.create_run(
                repo_name="octo/one", issue_number=2, issue_title="b", issue_url="u2", branch_name="b2"
            )
            no_pid = store.create_run(
                repo_name="octo/one", issue_number=3, issue_title="c", issue_url="u3", branch_name="b3"
            )
            store.update_run(alive, state="running", supervisor_pid=111)
            store.update_run(dead, state="running", supervisor_pid=222)
            store.update_run(no_pid, state="running")
            scheduler._pid_alive = staticmethod(lambda pid: pid == 111)

            failed = scheduler.reconcile_orphans()

            self.assertEqual(set(failed), {dead, no_pid})
            self.assertEqual(store.get_run(alive)["state"], "running")
            self.assertEqual(store.get_run(dead)["state"], "failed")
            self.assertIn("orphaned", store.get_run(dead)["last_error"])
            self.assertEqual(store.get_run(no_pid)["state"], "failed")

    def test_reconcile_orphans_ignores_interrupted_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = Scheduler(self._config(root), store, github=FakeGitHub())
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="a",
                issue_url="u1",
                branch_name="b1",
            )
            store.update_run(
                run_id,
                state="interrupted",
                stage="interrupted by shutdown",
                supervisor_pid=222,
            )
            scheduler._pid_alive = staticmethod(lambda pid: False)

            failed = scheduler.reconcile_orphans()

            self.assertEqual(failed, [])
            self.assertEqual(store.get_run(run_id)["state"], "interrupted")

    def test_pid_alive_false_for_unused_pid(self):
        self.assertFalse(Scheduler._pid_alive(2_000_000_000))
        self.assertFalse(Scheduler._pid_alive(0))


if __name__ == "__main__":
    unittest.main()
