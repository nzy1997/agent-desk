import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.continuation import ContinuationRunner, render_resume_interrupted_prompt
from agent_desk.github_client import PullRequestChecksStatus
from agent_desk.store import Store
from agent_desk.worker import CommandResult, FakeCommandRunner


class FakeGitHub:
    def __init__(self, existing_pr_urls: set[str]):
        self.existing_pr_urls = existing_pr_urls
        self.checked: list[tuple[str, str]] = []

    def pull_request_exists(self, repo: str, pr_url: str) -> bool:
        self.checked.append((repo, pr_url))
        return pr_url in self.existing_pr_urls


class ContinuationTests(unittest.TestCase):
    def _store_with_pr_run(self, root: Path) -> tuple[AgentDeskConfig, Store, int, Path]:
        worktree = root / "worktree"
        worktree.mkdir()
        config = AgentDeskConfig(
            data_dir=root / "data",
            repos=[RepoConfig(name="octo/example", local_path=root / "repo", base_branch="main")],
        )
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=7,
            issue_title="Fix parser",
            issue_url="https://github.com/octo/example/issues/7",
            branch_name="agent/issue-7-fix-parser-run-1",
            issue_body="Parser drops escaped commas.",
        )
        store.update_run(
            run_id,
            state="pr_open",
            stage="pull request opened",
            run_dir=str(root / "run"),
            worktree_path=str(worktree),
            codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
            pr_url="https://github.com/octo/example/pull/9",
        )
        return config, store, run_id, worktree

    def test_render_resume_interrupted_prompt_points_to_shutdown_context(self):
        run = {
            "id": 7,
            "repo_name": "octo/example",
            "issue_number": 5,
            "issue_title": "Shutdown",
            "run_dir": "/tmp/run-7",
            "worktree_path": "/tmp/worktree",
            "stage": "interrupted by shutdown",
        }

        prompt = render_resume_interrupted_prompt(run)

        self.assertIn("controlled Agent Desk shutdown", prompt)
        self.assertIn("/tmp/run-7", prompt)
        self.assertIn("/tmp/worktree", prompt)
        self.assertIn("worker-result.schema.json", prompt)

    def test_resume_interrupted_resumes_thread_and_opens_pr_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            worktree = root / "worktree"
            worktree.mkdir()
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Shutdown",
                issue_url="u5",
                branch_name="b5",
            )
            store.update_run(
                run_id,
                state="interrupted",
                stage="interrupted by shutdown",
                run_dir=str(run_dir),
                worktree_path=str(worktree),
                codex_thread_id="thread",
            )
            store.update_run(run_id, ended_at="old-ended-at")
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        (
                            '{"status":"done","summary":"resumed","tests":[],'
                            '"questions":[],"risks":[],'
                            '"pr_url":"https://example.test/pr/1","decision_log":[]}'
                        ),
                        "",
                    )
                ]
            )
            config = AgentDeskConfig(
                data_dir=root,
                repos=[RepoConfig(name="octo/example", local_path=root)],
            )

            result = ContinuationRunner(config, store, runner=runner).resume_interrupted(run_id)
            run = store.get_run(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(run["state"], "pr_open")
        self.assertEqual(run["pr_url"], "https://example.test/pr/1")
        self.assertNotEqual(run["ended_at"], "old-ended-at")
        self.assertTrue(any("resume-interrupted" in arg for arg in runner.calls[0].argv))

    def test_resume_interrupted_timeout_remains_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            worktree = root / "worktree"
            worktree.mkdir()
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="Shutdown",
                issue_url="u5",
                branch_name="b5",
            )
            store.update_run(
                run_id,
                state="interrupted",
                stage="interrupted by timeout",
                run_dir=str(run_dir),
                worktree_path=str(worktree),
                codex_thread_id="thread",
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        -9,
                        "",
                        "agent-desk: timeout timeout killed process after 28800.0s\n",
                        timeout_reason="timeout",
                    )
                ]
            )
            config = AgentDeskConfig(
                data_dir=root,
                repos=[RepoConfig(name="octo/example", local_path=root)],
            )

            result = ContinuationRunner(config, store, runner=runner).resume_interrupted(run_id)
            run = store.get_run(run_id)

        self.assertFalse(result.ok)
        self.assertEqual(run["state"], "interrupted")
        self.assertEqual(run["stage"], "interrupted by timeout")
        self.assertIn("Timed out", run["last_error"])

    def test_request_changes_resumes_original_codex_thread_with_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"updated PR","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).request_changes(
                run_id,
                "Please rename the CI job to Full Suite.",
            )
            call = runner.calls[0]
            run = store.get_run(run_id)

            self.assertTrue(result.ok)
            self.assertEqual(call.argv[0], "codex")
            ask_index = call.argv.index("--ask-for-approval")
            self.assertEqual(call.argv[ask_index : ask_index + 3], ["--ask-for-approval", "never", "--sandbox"])
            self.assertEqual(call.argv[call.argv.index("--sandbox") + 1], "workspace-write")
            self.assertEqual(call.argv[call.argv.index("-C") + 1], str(worktree))
            self.assertLess(call.argv.index("-C"), call.argv.index("exec"))
            self.assertEqual(call.idle_timeout, config.worker_idle_timeout_seconds)
            self.assertIn("resume", call.argv)
            self.assertIn("019ed932-fe5d-7391-b856-98b2239a6380", call.argv)
            self.assertIn("Please rename the CI job to Full Suite.", call.stdin)
            self.assertIn("push the updates to the existing PR branch", call.stdin)
            self.assertEqual(run["state"], "pr_open")
            self.assertEqual(run["stage"], "changes addressed")

    def test_request_changes_passes_run_ai_settings_to_codex_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            worktree = root / "worktree"
            worktree.mkdir()
            config = AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/example", local_path=root / "repo")],
            )
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=5,
                issue_title="PR",
                issue_url="https://github.com/octo/example/issues/5",
                branch_name="agent/issue-5-pr",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                worktree_path=str(worktree),
                codex_thread_id="thread-1",
                ai_model="gpt-5.6-sol",
                ai_reasoning_effort="max",
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"ok","tests":[],"questions":[],"pr_url":"https://github.com/octo/example/pull/1"}',
                        "",
                    )
                ]
            )

            ContinuationRunner(config, store, runner=runner).request_changes(run_id, "Please revise")
            argv = runner.calls[0].argv

        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertLess(argv.index("-m"), argv.index("exec"))

    def test_approve_finish_resumes_thread_with_generic_closeout_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"merged and cleaned up","tests":["gh pr checks passed"],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":["marked next issues ready"]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).approve_finish(run_id)
            call = runner.calls[0]
            run = store.get_run(run_id)

            self.assertTrue(result.ok)
            self.assertEqual(call.cwd, worktree)
            self.assertIn("Human approval has been granted", call.stdin)
            self.assertIn("Do not merge while checks are pending or failing", call.stdin)
            self.assertIn("Do not inspect or modify follow-up issue labels", call.stdin)
            self.assertIn("Agent Desk manages dependency unlocking locally", call.stdin)
            self.assertNotIn("Inspect only open issues with the configured blocked label", call.stdin)
            self.assertNotIn("Do not add agent:ready to unlabeled issues", call.stdin)
            self.assertNotIn("remove agent:blocked", call.stdin)
            self.assertEqual(run["state"], "done")
            self.assertEqual(run["stage"], "finished")

    def test_auto_finish_prompt_accepts_recorded_no_ci_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            store.update_run(
                run_id,
                pr_ci_status="no_ci",
                pr_ci_summary="no checks reported on the 'agent/issue-7' branch",
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"merged","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).finish_after_ci_success(run_id)
            call = runner.calls[0]

        self.assertTrue(result.ok)
        self.assertEqual(call.cwd, worktree)
        self.assertIn("Agent Desk reports this pull request is eligible for automatic closeout", call.stdin)
        self.assertIn("PR gate status: no_ci", call.stdin)
        self.assertIn("no checks reported on the 'agent/issue-7' branch", call.stdin)
        self.assertIn("If the gate is no_ci, confirm there are no required GitHub checks", call.stdin)
        self.assertNotIn("GitHub CI has passed", call.stdin)

    def test_approve_finish_uses_configured_closeout_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            config = AgentDeskConfig(
                data_dir=config.data_dir,
                repos=[
                    RepoConfig(
                        name="octo/example",
                        local_path=root / "repo",
                        base_branch="main",
                        closeout_sandbox="danger-full-access",
                    )
                ],
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"merged and cleaned up","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).approve_finish(run_id)
            call = runner.calls[0]

            self.assertTrue(result.ok)
            self.assertEqual(call.cwd, worktree)
            self.assertEqual(call.argv[0], "codex")
            self.assertEqual(call.argv[call.argv.index("--sandbox") + 1], "danger-full-access")
            self.assertEqual(call.argv[call.argv.index("-C") + 1], str(worktree))
            self.assertLess(call.argv.index("-C"), call.argv.index("exec"))

    def test_request_changes_keeps_workspace_write_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            config = AgentDeskConfig(
                data_dir=config.data_dir,
                repos=[
                    RepoConfig(
                        name="octo/example",
                        local_path=root / "repo",
                        base_branch="main",
                        closeout_sandbox="danger-full-access",
                    )
                ],
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"updated PR","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).request_changes(run_id, "Please update docs.")
            call = runner.calls[0]

            self.assertTrue(result.ok)
            self.assertEqual(call.cwd, worktree)
            self.assertEqual(call.argv[0], "codex")
            self.assertEqual(call.argv[call.argv.index("--sandbox") + 1], "workspace-write")
            self.assertEqual(call.argv[call.argv.index("-C") + 1], str(worktree))
            self.assertLess(call.argv.index("-C"), call.argv.index("exec"))

    def test_open_pull_request_resumes_thread_and_requires_pr_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            store.update_run(run_id, state="running", stage="codex done; resuming to open pull request", pr_url="")
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"opened PR","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/10","decision_log":[]}',
                        "",
                    )
                ]
            )

            github = FakeGitHub(existing_pr_urls={"https://github.com/octo/example/pull/10"})

            result = ContinuationRunner(config, store, runner, github=github).open_pull_request(run_id)
            call = runner.calls[0]
            run = store.get_run(run_id)

            self.assertTrue(result.ok)
            self.assertEqual(call.cwd, worktree)
            self.assertIn("Implementation work is complete", call.stdin)
            self.assertIn("create a draft pull request", call.stdin)
            self.assertIn("Do not merge", call.stdin)
            self.assertEqual(run["state"], "pr_open")
            self.assertEqual(run["stage"], "pull request opened")
            self.assertEqual(run["pr_url"], "https://github.com/octo/example/pull/10")
            self.assertEqual(github.checked, [("octo/example", "https://github.com/octo/example/pull/10")])

    def test_open_pull_request_blocks_when_reported_pr_url_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree = self._store_with_pr_run(root)
            store.update_run(run_id, state="running", stage="codex done; resuming to open pull request", pr_url="")
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"opened PR","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/128","decision_log":[]}',
                        "",
                    )
                ]
            )
            github = FakeGitHub(existing_pr_urls=set())

            result = ContinuationRunner(config, store, runner, github=github).open_pull_request(run_id)
            run = store.get_run(run_id)

            self.assertFalse(result.ok)
            self.assertEqual(github.checked, [("octo/example", "https://github.com/octo/example/pull/128")])
            self.assertEqual(run["state"], "blocked")
            self.assertEqual(run["stage"], "blocked")
            self.assertEqual(run["pr_url"], "")
            self.assertEqual(
                run["last_error"],
                "open-pr returned non-existent pr_url: https://github.com/octo/example/pull/128",
            )

    def test_open_pull_request_blocks_when_resume_done_without_pr_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree = self._store_with_pr_run(root)
            store.update_run(run_id, state="running", stage="codex done; resuming to open pull request", pr_url="")
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"finished but no URL","tests":[],"questions":[],"risks":[],"pr_url":"","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).open_pull_request(run_id)
            run = store.get_run(run_id)

            self.assertFalse(result.ok)
            self.assertEqual(run["state"], "blocked")
            self.assertEqual(run["last_error"], "open-pr returned done without pr_url")

    def test_open_pull_request_uses_configured_closeout_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            config = AgentDeskConfig(
                data_dir=config.data_dir,
                repos=[
                    RepoConfig(
                        name="octo/example",
                        local_path=root / "repo",
                        base_branch="main",
                        closeout_sandbox="danger-full-access",
                    )
                ],
            )
            store.update_run(run_id, state="running", stage="codex done; resuming to open pull request", pr_url="")
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"opened PR","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/10","decision_log":[]}',
                        "",
                    )
                ]
            )

            github = FakeGitHub(existing_pr_urls={"https://github.com/octo/example/pull/10"})

            result = ContinuationRunner(config, store, runner, github=github).open_pull_request(run_id)
            call = runner.calls[0]

            self.assertTrue(result.ok)
            self.assertEqual(call.cwd, worktree)
            self.assertEqual(call.argv[0], "codex")
            self.assertEqual(call.argv[call.argv.index("--sandbox") + 1], "danger-full-access")
            self.assertEqual(call.argv[call.argv.index("-C") + 1], str(worktree))
            self.assertLess(call.argv.index("-C"), call.argv.index("exec"))

    def test_approve_finish_backfills_thread_id_from_historical_stdout_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "stdout.jsonl").write_text(
                '{"type":"thread.started","thread_id":"019ed932-fe5d-7391-b856-98b2239a6380"}\n',
                encoding="utf-8",
            )
            store.update_run(run_id, codex_thread_id="")
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"merged and cleaned up","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).approve_finish(run_id)
            call = runner.calls[0]
            run = store.get_run(run_id)

            self.assertTrue(result.ok)
            self.assertEqual(call.cwd, worktree)
            self.assertIn("019ed932-fe5d-7391-b856-98b2239a6380", call.argv)
            self.assertEqual(run["codex_thread_id"], "019ed932-fe5d-7391-b856-98b2239a6380")

    def test_approve_finish_blocks_when_codex_reports_pending_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree = self._store_with_pr_run(root)
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"blocked","summary":"checks still pending","tests":[],"questions":["wait for CI"],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).approve_finish(run_id)
            run = store.get_run(run_id)

            self.assertFalse(result.ok)
            self.assertEqual(run["state"], "blocked")
            self.assertEqual(run["last_error"], "checks still pending")

    def test_fix_ci_resumes_thread_with_failing_check_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"fixed CI","tests":["python -m unittest"],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )
            pr_status = PullRequestChecksStatus(
                state="failure",
                summary="1 failed",
                head_sha="abc123",
                checks=[
                    {
                        "name": "unit",
                        "state": "FAILURE",
                        "bucket": "fail",
                        "description": "AssertionError in parser tests",
                        "link": "https://example.test/checks/1",
                    }
                ],
            )

            result = ContinuationRunner(config, store, runner).fix_ci(
                run_id,
                pr_status,
                attempt=2,
                max_attempts=3,
            )
            call = runner.calls[0]
            run = store.get_run(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(call.cwd, worktree)
        self.assertIn("Automatic CI fix attempt 2 of 3", call.stdin)
        self.assertIn("unit", call.stdin)
        self.assertIn("AssertionError in parser tests", call.stdin)
        self.assertIn("push the updates to the existing PR branch", call.stdin)
        self.assertEqual(run["state"], "pr_open")
        self.assertEqual(run["stage"], "ci fix pushed")

    def test_fix_ci_uses_configured_closeout_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            config = AgentDeskConfig(
                data_dir=config.data_dir,
                repos=[
                    RepoConfig(
                        name="octo/example",
                        local_path=root / "repo",
                        base_branch="main",
                        closeout_sandbox="danger-full-access",
                    )
                ],
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"fixed CI","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )
            pr_status = PullRequestChecksStatus(
                state="failure",
                summary="Pull request has merge conflicts",
                head_sha="abc123",
                checks=[{"name": "mergeable", "state": "CONFLICTING"}],
            )

            result = ContinuationRunner(config, store, runner).fix_ci(
                run_id,
                pr_status,
                attempt=1,
                max_attempts=3,
            )
            call = runner.calls[0]

        self.assertTrue(result.ok)
        self.assertEqual(call.cwd, worktree)
        self.assertEqual(call.argv[0], "codex")
        self.assertEqual(call.argv[call.argv.index("--sandbox") + 1], "danger-full-access")
        self.assertEqual(call.argv[call.argv.index("-C") + 1], str(worktree))
        self.assertLess(call.argv.index("-C"), call.argv.index("exec"))


if __name__ == "__main__":
    unittest.main()
