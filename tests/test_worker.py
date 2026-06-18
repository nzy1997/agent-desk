import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.store import Store
from agent_desk.worker import CommandResult, FakeCommandRunner, Worker


class WorkerTests(unittest.TestCase):
    def test_worker_invokes_codex_non_interactively_and_writes_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            repo_path.mkdir()
            config = AgentDeskConfig(data_dir=root / "data")
            repo = RepoConfig(
                name="octo/example",
                local_path=repo_path,
                base_branch="main",
                test_command="python -m unittest",
                push_pr=False,
            )
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name=repo.name,
                issue_number=7,
                issue_title="Fix parser",
                issue_url="https://github.com/octo/example/issues/7",
                branch_name="agent/issue-7-fix-parser",
            )
            runner = FakeCommandRunner(
                results=[
                    CommandResult(["git", "fetch"], 0, "", ""),
                    CommandResult(["git", "worktree"], 0, "", ""),
                    CommandResult(["codex", "exec"], 0, '{"status":"done","summary":"ok","tests":["python -m unittest"],"questions":[]}', ""),
                ]
            )

            result = Worker(config, store, runner).run_issue(
                run_id=run_id,
                repo=repo,
                issue_number=7,
                issue_title="Fix parser",
                issue_body="Body",
                issue_url="https://github.com/octo/example/issues/7",
                branch_name="agent/issue-7-fix-parser",
            )

            codex_call = runner.calls[2]
            self.assertEqual(codex_call.argv[:4], ["codex", "--ask-for-approval", "never", "exec"])
            self.assertIn("--json", codex_call.argv)
            self.assertIn("--sandbox", codex_call.argv)
            self.assertIn("workspace-write", codex_call.argv)
            self.assertEqual(result.status, "done")
            self.assertTrue((config.data_dir / "runs" / "issue-7" / "run-1" / "prompt.md").exists())
            self.assertTrue((config.data_dir / "runs" / "issue-7" / "run-1" / "stdout.jsonl").exists())

    def test_worker_marks_run_pr_open_when_codex_returns_pr_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            repo_path.mkdir()
            config = AgentDeskConfig(data_dir=root / "data")
            repo = RepoConfig(
                name="octo/example",
                local_path=repo_path,
                base_branch="main",
                test_command="python -m unittest",
            )
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name=repo.name,
                issue_number=8,
                issue_title="Open PR",
                issue_url="https://github.com/octo/example/issues/8",
                branch_name="agent/issue-8-open-pr",
            )
            runner = FakeCommandRunner(
                results=[
                    CommandResult(["git", "fetch"], 0, "", ""),
                    CommandResult(["git", "worktree"], 0, "", ""),
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        '{"status":"done","summary":"ok","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":["chose recommended execution option"]}',
                        "",
                    ),
                ]
            )

            result = Worker(config, store, runner).run_issue(
                run_id=run_id,
                repo=repo,
                issue_number=8,
                issue_title="Open PR",
                issue_body="Body",
                issue_url="https://github.com/octo/example/issues/8",
                branch_name="agent/issue-8-open-pr",
            )
            run = store.get_run(run_id)

            self.assertEqual(result.pr_url, "https://github.com/octo/example/pull/9")
            self.assertEqual(result.decision_log, ["chose recommended execution option"])
            self.assertEqual(run["state"], "pr_open")
            self.assertEqual(run["pr_url"], "https://github.com/octo/example/pull/9")


if __name__ == "__main__":
    unittest.main()
