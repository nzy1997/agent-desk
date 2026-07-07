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
        self.assertNotIn("--label", args)
        self.assertIn("number,title,body,url,labels", args)
        self.assertEqual([issue["number"] for issue in issues], [1])

    def test_pull_request_exists_uses_pr_view_for_reported_url(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["gh", "pr", "view"],
                0,
                '{"url":"https://github.com/octo/example/pull/9"}',
                "",
            )

            exists = GitHubClient().pull_request_exists(
                "octo/example",
                "https://github.com/octo/example/pull/9",
            )

        self.assertTrue(exists)
        self.assertEqual(
            run.call_args.args[0],
            ["gh", "pr", "view", "9", "--repo", "octo/example", "--json", "url"],
        )

    def test_pull_request_exists_rejects_missing_or_unresolvable_pr(self):
        self.assertFalse(GitHubClient().pull_request_exists("octo/example", ""))

        with patch("agent_desk.github_client.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["gh", "pr", "view"],
                1,
                "",
                "GraphQL: Could not resolve to a PullRequest with the number of 128.",
            )

            exists = GitHubClient().pull_request_exists(
                "octo/example",
                "https://github.com/octo/example/pull/128",
            )

        self.assertFalse(exists)

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

    def test_pr_checks_status_reports_no_ci_for_empty_checks_json(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 0, "[]", ""),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/9",
            )

        self.assertEqual(status.state, "no_ci")
        self.assertEqual(status.summary, "No checks reported")
        self.assertEqual(status.head_sha, "abc123")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_reports_no_ci_for_gh_no_checks_message(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"def456"}', ""),
                subprocess.CompletedProcess(
                    ["gh", "pr", "checks"],
                    1,
                    "",
                    "no checks reported on the 'agent/issue-44' branch",
                ),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/10",
            )

        self.assertEqual(status.state, "no_ci")
        self.assertEqual(status.summary, "no checks reported on the 'agent/issue-44' branch")
        self.assertEqual(status.head_sha, "def456")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_keeps_ambiguous_empty_output_unknown(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 1, "", "GraphQL: timeout"),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/11",
            )

        self.assertEqual(status.state, "unknown")
        self.assertEqual(status.summary, "GraphQL: timeout")

    def test_pr_checks_status_reports_unknown_for_non_list_checks_json(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 0, '{"unexpected":true}', ""),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/12",
            )

        self.assertEqual(status.state, "unknown")
        self.assertEqual(status.summary, "Unexpected PR checks JSON")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_reports_unknown_for_null_checks_json(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 0, "null", ""),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/13",
            )

        self.assertEqual(status.state, "unknown")
        self.assertEqual(status.summary, "Unexpected PR checks JSON")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_reports_unknown_for_invalid_check_entries(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 0, '[null,"bad"]', ""),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/14",
            )

        self.assertEqual(status.state, "unknown")
        self.assertEqual(status.summary, "No valid PR checks reported")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_reports_unknown_for_check_entries_without_state_signal(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 0, '[{"name":"unit"}]', ""),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/15",
            )

        self.assertEqual(status.state, "unknown")
        self.assertEqual(status.summary, "No valid PR checks reported")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_reports_unknown_for_unrecognized_check_state(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 0, '[{"name":"unit","state":"WEIRD"}]', ""),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/16",
            )

        self.assertEqual(status.state, "unknown")
        self.assertEqual(status.summary, "1 unknown")


if __name__ == "__main__":
    unittest.main()
