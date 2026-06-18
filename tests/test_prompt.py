import unittest
from pathlib import Path

from agent_desk.config import RepoConfig
from agent_desk.prompt import render_worker_prompt


class PromptTests(unittest.TestCase):
    def test_prompt_contains_non_interactive_contract_and_issue_context(self):
        repo = RepoConfig(
            name="octo/example",
            local_path=Path("/repo"),
            base_branch="main",
            test_command="python -m unittest",
        )

        prompt = render_worker_prompt(
            repo=repo,
            issue_number=7,
            issue_title="Fix flaky parser",
            issue_body="Parser drops escaped commas.",
            issue_url="https://github.com/octo/example/issues/7",
            branch_name="agent/issue-7-fix-flaky-parser",
        )

        self.assertIn("Do not ask interactive questions", prompt)
        self.assertIn("status", prompt)
        self.assertIn("blocked", prompt)
        self.assertIn("Parser drops escaped commas.", prompt)
        self.assertIn("python -m unittest", prompt)
        self.assertIn("agent/issue-7-fix-flaky-parser", prompt)

    def test_prompt_drives_superpowers_flow_with_recommended_answers(self):
        repo = RepoConfig(
            name="octo/example",
            local_path=Path("/repo"),
            base_branch="main",
            test_command="python -m unittest",
            push_pr=True,
        )

        prompt = render_worker_prompt(
            repo=repo,
            issue_number=7,
            issue_title="Fix flaky parser",
            issue_body="Parser drops escaped commas.",
            issue_url="https://github.com/octo/example/issues/7",
            branch_name="agent/issue-7-fix-flaky-parser",
        )

        self.assertIn("superpowers:brainstorming", prompt)
        self.assertIn("superpowers:writing-plans", prompt)
        self.assertIn("choose the option currently marked recommended", prompt)
        self.assertNotIn("Choose: Subagent-Driven", prompt)
        self.assertIn("After implementation and verification are complete, create a pull request", prompt)
        self.assertIn("Push and create a Pull Request", prompt)
        self.assertIn("decision_log", prompt)
        self.assertIn("Stop after the pull request is created", prompt)
        self.assertIn("only failed step is creating the pull request", prompt)
        self.assertIn('return status "done" with pr_url set to an empty string', prompt)

    def test_prompt_keeps_branch_local_when_push_pr_is_disabled(self):
        repo = RepoConfig(
            name="octo/example",
            local_path=Path("/repo"),
            base_branch="main",
            test_command="python -m unittest",
            push_pr=False,
        )

        prompt = render_worker_prompt(
            repo=repo,
            issue_number=7,
            issue_title="Fix flaky parser",
            issue_body="Parser drops escaped commas.",
            issue_url="https://github.com/octo/example/issues/7",
            branch_name="agent/issue-7-fix-flaky-parser",
        )

        self.assertIn("Keep the branch as-is", prompt)
        self.assertIn("Do not push, open a pull request, or merge", prompt)


if __name__ == "__main__":
    unittest.main()
