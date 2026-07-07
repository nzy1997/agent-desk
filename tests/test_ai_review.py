import tempfile
import unittest
from pathlib import Path

from agent_desk.ai_review import AIReviewRunner, parse_ai_review_result, render_ai_review_prompt
from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.github_client import PullRequestChecksStatus
from agent_desk.store import Store
from agent_desk.worker import CommandResult, FakeCommandRunner


class AIReviewTests(unittest.TestCase):
    def _config_store_run(self, root: Path):
        worktree = root / "worktree"
        worktree.mkdir()
        run_dir = root / "run"
        run_dir.mkdir()
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
            run_dir=str(run_dir),
            worktree_path=str(worktree),
            codex_thread_id="thread-1",
            pr_url="https://github.com/octo/example/pull/9",
        )
        store.add_event(
            run_id,
            "info",
            "worker-result",
            "Worker finished with status pr_open",
            {
                "summary": "Implemented parser fix.",
                "tests": ["python3 -m unittest tests.test_parser -v"],
                "questions": [],
                "risks": ["No remote CI is configured."],
                "decision_log": ["Kept the change scoped to parser escaping."],
            },
        )
        return config, store, run_id, worktree, run_dir

    def test_render_ai_review_prompt_is_english_and_read_only(self):
        run = {
            "repo_name": "octo/example",
            "issue_number": 7,
            "issue_title": "Fix parser",
            "issue_url": "https://github.com/octo/example/issues/7",
            "issue_body": "Parser drops escaped commas.",
            "branch_name": "agent/issue-7",
            "pr_url": "https://github.com/octo/example/pull/9",
            "run_dir": "/tmp/run",
            "worktree_path": "/tmp/worktree",
            "events": [],
        }
        pr_status = PullRequestChecksStatus(
            state="no_ci",
            summary="No checks reported",
            head_sha="abc123",
            checks=[],
        )

        prompt = render_ai_review_prompt(run, pr_status)

        self.assertIn("You are an independent AI reviewer", prompt)
        self.assertIn("Do not edit files, commit, push, or merge", prompt)
        self.assertIn("Treat no_ci as a real absence of GitHub CI", prompt)
        self.assertIn('"status": "approved | changes_requested | blocked"', prompt)
        self.assertIn("Return only JSON", prompt)

    def test_parse_ai_review_result_reads_result_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "ai-review-result.json"
            result_path.write_text(
                '{"status":"changes_requested","summary":"Needs tests","findings":["Missing test"],"feedback":"Please add a parser regression test.","risks":["Untested edge case"],"pr_url":"https://github.com/octo/example/pull/9"}',
                encoding="utf-8",
            )

            payload = parse_ai_review_result(result_path, "")

        self.assertEqual(payload.status, "changes_requested")
        self.assertEqual(payload.feedback, "Please add a parser regression test.")
        self.assertEqual(payload.findings, ["Missing test"])

    def test_review_approved_records_review_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree, run_dir = self._config_store_run(root)
            pr_status = PullRequestChecksStatus(
                state="success",
                summary="2 passed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "SUCCESS"}],
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        '{"status":"approved","summary":"Looks good","findings":[],"feedback":"","risks":[],"pr_url":"https://github.com/octo/example/pull/9"}',
                        "",
                    )
                ]
            )

            result = AIReviewRunner(config, store, runner=runner).review(run_id, pr_status)
            run = store.get_run(run_id)
            call = runner.calls[0]
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "approved")
            self.assertEqual(call.cwd, worktree)
            self.assertIn("codex", call.argv)
            self.assertIn("--output-last-message", call.argv)
            self.assertEqual(run["state"], "pr_open")
            self.assertEqual(run["stage"], "ai-review approved")
            self.assertEqual(run["ai_review_status"], "approved")
            self.assertEqual(run["ai_review_summary"], "Looks good")
            self.assertEqual(run["ai_review_head_sha"], "abc123")
            self.assertTrue((run_dir / "ai-review-prompt.md").exists())
            self.assertTrue((run_dir / "ai-review-result.json").exists())

    def test_review_changes_requested_requires_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree, _run_dir = self._config_store_run(root)
            pr_status = PullRequestChecksStatus(state="no_ci", summary="No checks reported", head_sha="abc123", checks=[])
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        '{"status":"changes_requested","summary":"Needs tests","findings":["Missing regression"],"feedback":"Please add a regression test.","risks":[],"pr_url":"https://github.com/octo/example/pull/9"}',
                        "",
                    )
                ]
            )

            result = AIReviewRunner(config, store, runner=runner).review(run_id, pr_status)
            run = store.get_run(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "changes_requested")
        self.assertEqual(run["state"], "pr_open")
        self.assertEqual(run["stage"], "ai-review changes requested")
        self.assertEqual(run["ai_review_feedback"], "Please add a regression test.")

    def test_review_blocks_when_changes_requested_has_no_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree, _run_dir = self._config_store_run(root)
            pr_status = PullRequestChecksStatus(state="success", summary="1 passed", head_sha="abc123", checks=[])
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        '{"status":"changes_requested","summary":"Needs work","findings":["Missing regression"],"feedback":"","risks":[],"pr_url":"https://github.com/octo/example/pull/9"}',
                        "",
                    )
                ]
            )

            result = AIReviewRunner(config, store, runner=runner).review(run_id, pr_status)
            run = store.get_run(run_id)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("feedback is required", run["last_error"])

    def test_review_blocks_without_worktree_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree, _run_dir = self._config_store_run(root)
            store.update_run(run_id, worktree_path="")
            pr_status = PullRequestChecksStatus(state="success", summary="1 passed", head_sha="abc123", checks=[])

            result = AIReviewRunner(config, store, runner=FakeCommandRunner([])).review(run_id, pr_status)
            run = store.get_run(run_id)

        self.assertFalse(result.ok)
        self.assertEqual(run["state"], "blocked")
        self.assertIn("ai-review requires worktree_path", run["last_error"])


if __name__ == "__main__":
    unittest.main()
