import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.continuation import ContinuationRunner
from agent_desk.store import Store
from agent_desk.worker import CommandResult, FakeCommandRunner


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
            self.assertEqual(call.argv[:8], ["codex", "--ask-for-approval", "never", "--sandbox", "workspace-write", "-C", str(worktree), "exec"])
            self.assertEqual(call.idle_timeout, config.worker_idle_timeout_seconds)
            self.assertIn("resume", call.argv)
            self.assertIn("019ed932-fe5d-7391-b856-98b2239a6380", call.argv)
            self.assertIn("Please rename the CI job to Full Suite.", call.stdin)
            self.assertIn("push the updates to the existing PR branch", call.stdin)
            self.assertEqual(run["state"], "pr_open")
            self.assertEqual(run["stage"], "changes addressed")

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
            self.assertIn("determine which open issues are now unblocked", call.stdin)
            self.assertIn("agent:ready", call.stdin)
            self.assertEqual(run["state"], "done")
            self.assertEqual(run["stage"], "finished")

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
            self.assertEqual(
                call.argv[:8],
                ["codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "-C", str(worktree), "exec"],
            )

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
            self.assertEqual(
                call.argv[:8],
                ["codex", "--ask-for-approval", "never", "--sandbox", "workspace-write", "-C", str(worktree), "exec"],
            )

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

            result = ContinuationRunner(config, store, runner).open_pull_request(run_id)
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

            result = ContinuationRunner(config, store, runner).open_pull_request(run_id)
            call = runner.calls[0]

            self.assertTrue(result.ok)
            self.assertEqual(call.cwd, worktree)
            self.assertEqual(
                call.argv[:8],
                ["codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "-C", str(worktree), "exec"],
            )

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


if __name__ == "__main__":
    unittest.main()
