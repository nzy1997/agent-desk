import tempfile
import sys
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.store import Store
from agent_desk.worker import CommandResult, CommandRunner, FakeCommandRunner, Worker, extract_thread_id


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
            self.assertEqual(codex_call.idle_timeout, config.worker_idle_timeout_seconds)
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

    def test_worker_resumes_codex_to_open_pr_when_codex_finishes_without_pr_url(self):
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
                push_pr=True,
            )
            config = AgentDeskConfig(data_dir=root / "data", repos=[repo])
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name=repo.name,
                issue_number=9,
                issue_title="Fallback PR",
                issue_url="https://github.com/octo/example/issues/9",
                branch_name="agent/issue-9-fallback-pr",
            )
            runner = FakeCommandRunner(
                results=[
                    CommandResult(["git", "fetch"], 0, "", ""),
                    CommandResult(["git", "worktree"], 0, "", ""),
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        "\n".join(
                            [
                                '{"type":"thread.started","thread_id":"019ed932-fe5d-7391-b856-98b2239a6380"}',
                                '{"status":"done","summary":"ok","tests":[],"questions":[],"risks":["worker could not create PR"],"pr_url":"","decision_log":[]}',
                            ]
                        ),
                        "",
                    ),
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"opened PR","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/10","decision_log":[]}',
                        "",
                    ),
                ]
            )

            Worker(config, store, runner).run_issue(
                run_id=run_id,
                repo=repo,
                issue_number=9,
                issue_title="Fallback PR",
                issue_body="Body",
                issue_url="https://github.com/octo/example/issues/9",
                branch_name="agent/issue-9-fallback-pr",
            )
            run = store.get_run(run_id)
            codex_done_events = [
                event
                for event in store.dashboard_state()["events"]
                if event["run_id"] == run_id and event["event_type"] == "codex-done"
            ]

            self.assertEqual(run["state"], "pr_open")
            self.assertEqual(run["pr_url"], "https://github.com/octo/example/pull/10")
            self.assertEqual(len(runner.calls), 4)
            resume_call = runner.calls[3]
            self.assertIn("resume", resume_call.argv)
            self.assertIn("019ed932-fe5d-7391-b856-98b2239a6380", resume_call.argv)
            self.assertIn("create a draft pull request", resume_call.stdin)
            self.assertNotIn("Created by Agent Desk", resume_call.stdin)
            self.assertEqual(len(codex_done_events), 1)
            self.assertEqual(codex_done_events[0]["message"], "Codex returned done; resuming to open pull request")

    def test_worker_blocks_when_open_pr_resume_cannot_create_pr(self):
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
                push_pr=True,
            )
            config = AgentDeskConfig(data_dir=root / "data", repos=[repo])
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name=repo.name,
                issue_number=10,
                issue_title="PR network failure",
                issue_url="https://github.com/octo/example/issues/10",
                branch_name="agent/issue-10-pr-network-failure",
            )
            runner = FakeCommandRunner(
                results=[
                    CommandResult(["git", "fetch"], 0, "", ""),
                    CommandResult(["git", "worktree"], 0, "", ""),
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        "\n".join(
                            [
                                '{"type":"thread.started","thread_id":"019ed932-fe5d-7391-b856-98b2239a6380"}',
                                '{"status":"done","summary":"ok","tests":[],"questions":[],"risks":[],"pr_url":"","decision_log":[]}',
                            ]
                        ),
                        "",
                    ),
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"blocked","summary":"network denied","tests":[],"questions":["need GitHub access"],"risks":[],"pr_url":"","decision_log":[]}',
                        "",
                    ),
                ]
            )

            Worker(config, store, runner).run_issue(
                run_id=run_id,
                repo=repo,
                issue_number=10,
                issue_title="PR network failure",
                issue_body="Body",
                issue_url="https://github.com/octo/example/issues/10",
                branch_name="agent/issue-10-pr-network-failure",
            )
            run = store.get_run(run_id)

            self.assertEqual(run["state"], "blocked")
            self.assertEqual(run["pr_url"], "")
            self.assertEqual(run["last_error"], "network denied")
            self.assertEqual(len(runner.calls), 4)
            self.assertIn("resume", runner.calls[3].argv)

    def test_worker_records_codex_thread_id_and_resume_log(self):
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
                issue_number=11,
                issue_title="Resume me",
                issue_url="https://github.com/octo/example/issues/11",
                branch_name="agent/issue-11-resume-me",
            )
            thread_id = "019ed932-fe5d-7391-b856-98b2239a6380"
            runner = FakeCommandRunner(
                results=[
                    CommandResult(["git", "fetch"], 0, "", ""),
                    CommandResult(["git", "worktree"], 0, "", ""),
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        "\n".join(
                            [
                                f'{{"type":"thread.started","thread_id":"{thread_id}"}}',
                                '{"type":"turn.started"}',
                                '{"status":"done","summary":"ok","tests":[],"questions":[],"risks":[],"pr_url":"","decision_log":[]}',
                            ]
                        ),
                        "",
                    ),
                ]
            )

            result = Worker(config, store, runner).run_issue(
                run_id=run_id,
                repo=repo,
                issue_number=11,
                issue_title="Resume me",
                issue_body="Body",
                issue_url="https://github.com/octo/example/issues/11",
                branch_name="agent/issue-11-resume-me",
            )
            run = store.get_run(run_id)
            resume_log = config.data_dir / "runs" / "issue-11" / "run-1" / "codex-resume.txt"

            self.assertEqual(result.status, "done")
            self.assertEqual(run["codex_thread_id"], thread_id)
            self.assertIn(thread_id, resume_log.read_text(encoding="utf-8"))
            self.assertIn("codex resume --include-non-interactive", resume_log.read_text(encoding="utf-8"))

    def test_extract_thread_id_ignores_non_thread_events(self):
        stdout = "\n".join(
            [
                '{"type":"turn.started"}',
                '{"not json"',
                '{"type":"thread.started","thread_id":"019ed932-fe5d-7391-b856-98b2239a6380"}',
            ]
        )

        self.assertEqual(extract_thread_id(stdout), "019ed932-fe5d-7391-b856-98b2239a6380")

    def test_command_runner_streams_logs_and_kills_idle_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.log"
            stderr_path = root / "stderr.log"

            result = CommandRunner().run(
                [
                    sys.executable,
                    "-c",
                    "import time; print('first event', flush=True); time.sleep(3)",
                ],
                timeout=10,
                idle_timeout=0.2,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            stdout_text = stdout_path.read_text(encoding="utf-8")
            stderr_text = stderr_path.read_text(encoding="utf-8")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.timeout_reason, "idle")
        self.assertIn("first event", result.stdout)
        self.assertIn("first event", stdout_text)
        self.assertIn("idle timeout", result.stderr)
        self.assertIn("idle timeout", stderr_text)


if __name__ == "__main__":
    unittest.main()
