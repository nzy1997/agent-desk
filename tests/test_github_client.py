import subprocess
import unittest
from unittest.mock import patch

from agent_desk.github_client import GitHubClient, PullRequestChecksStatus


class GitHubClientTests(unittest.TestCase):
    def test_list_open_issues_requests_bodies_without_label_filter(self):
        payload = '[{"number":1,"title":"One","body":"b1","url":"u1","labels":[]}]'
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh", "issue", "list"], 0, payload, "")
            issues = GitHubClient().list_open_issues("octo/example")
        args = run.call_args.args[0]
        # No --label (that search index lags); bodies are fetched for the picker.
        self.assertNotIn("--label", args)
        self.assertIn("number,title,body,url,labels", args)
        self.assertEqual([issue["number"] for issue in issues], [1])

    def test_pr_checks_status_parses_failed_checks_even_when_gh_exits_nonzero(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    ["gh", "pr", "view"],
                    0,
                    '{"headRefOid":"abc123"}',
                    "",
                ),
                subprocess.CompletedProcess(
                    ["gh", "pr", "checks"],
                    1,
                    '[{"name":"unit","state":"FAILURE","bucket":"fail","description":"tests failed","link":"https://example.test/check"}]',
                    "",
                ),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/9",
            )

        self.assertEqual(status.state, "failure")
        self.assertEqual(status.head_sha, "abc123")
        self.assertEqual(status.summary, "1 failed")
        self.assertEqual(status.checks[0]["name"], "unit")
        self.assertEqual(status.checks[0]["state"], "FAILURE")

    def test_pr_checks_status_reports_pending_when_any_check_is_running(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"def456"}', ""),
                subprocess.CompletedProcess(
                    ["gh", "pr", "checks"],
                    8,
                    '[{"name":"unit","state":"SUCCESS","bucket":"pass"},{"name":"integration","state":"PENDING","bucket":"pending"}]',
                    "",
                ),
            ]

            status = GitHubClient().pr_checks_status("octo/example", "https://github.com/octo/example/pull/10")

        self.assertEqual(status.state, "pending")
        self.assertEqual(status.summary, "1 passed, 1 pending")

    def test_pr_checks_status_reports_failure_for_conflicting_pr_without_checks(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    ["gh", "pr", "view"],
                    0,
                    '{"headRefOid":"abc123","mergeable":"CONFLICTING","mergeStateStatus":"DIRTY"}',
                    "",
                ),
            ]

            status = GitHubClient().pr_checks_status("octo/example", "https://github.com/octo/example/pull/28")

        self.assertEqual(status.state, "failure")
        self.assertEqual(status.summary, "Pull request has merge conflicts")
        self.assertEqual(status.head_sha, "abc123")
        self.assertEqual(status.checks[0]["name"], "mergeable")
        self.assertEqual(status.checks[0]["state"], "CONFLICTING")
        self.assertEqual(run.call_count, 1)

    def test_pr_checks_status_reports_unknown_for_missing_pr_number(self):
        status = GitHubClient().pr_checks_status("octo/example", "")

        self.assertEqual(
            status,
            PullRequestChecksStatus(state="unknown", summary="No pull request URL", head_sha="", checks=[]),
        )


if __name__ == "__main__":
    unittest.main()
