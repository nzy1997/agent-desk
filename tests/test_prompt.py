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


if __name__ == "__main__":
    unittest.main()
