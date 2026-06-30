import json
import unittest

from agent_desk.dependencies import parse_dependency_result, render_dependency_prompt


class DependencyTests(unittest.TestCase):
    def test_render_dependency_prompt_contains_schema_and_issue_payload(self):
        prompt = render_dependency_prompt(
            "octo/example",
            [
                {
                    "number": 12,
                    "title": "Follow-up",
                    "body": "Depends on #10",
                    "url": "https://example.test/12",
                }
            ],
        )

        self.assertIn("You are Agent Desk's dependency extractor.", prompt)
        self.assertIn('"depends_on"', prompt)
        self.assertIn('"repo": "octo/example"', prompt)
        self.assertIn('"number": 12', prompt)
        self.assertIn("Return JSON only", prompt)

    def test_parse_dependency_result_normalizes_dependencies(self):
        payload = {
            "repo": "octo/example",
            "issues": [
                {
                    "number": 12,
                    "depends_on": [
                        {
                            "repo": "octo/example",
                            "number": 10,
                            "evidence": "Depends on #10",
                            "confidence": "high",
                        }
                    ],
                    "notes": "clear",
                }
            ],
            "warnings": ["ignored vague roadmap order"],
        }

        graph = parse_dependency_result(json.dumps(payload), default_repo="octo/example")

        self.assertEqual(graph.repo, "octo/example")
        self.assertEqual(graph.warnings, ["ignored vague roadmap order"])
        self.assertEqual(len(graph.issues), 1)
        issue = graph.issues[0]
        self.assertEqual(issue.number, 12)
        self.assertEqual(issue.notes, "clear")
        self.assertEqual(len(issue.depends_on), 1)
        dep = issue.depends_on[0]
        self.assertEqual(dep.repo, "octo/example")
        self.assertEqual(dep.number, 10)
        self.assertEqual(dep.evidence, "Depends on #10")
        self.assertEqual(dep.confidence, "high")

    def test_parse_dependency_result_rejects_invalid_json(self):
        with self.assertRaises(ValueError):
            parse_dependency_result("not json", default_repo="octo/example")

    def test_parse_dependency_result_defaults_missing_dependency_repo(self):
        payload = {
            "repo": "octo/example",
            "issues": [
                {
                    "number": 12,
                    "depends_on": [{"number": 10, "evidence": "#10", "confidence": "medium"}],
                }
            ],
        }

        graph = parse_dependency_result(json.dumps(payload), default_repo="octo/example")

        self.assertEqual(graph.issues[0].depends_on[0].repo, "octo/example")


if __name__ == "__main__":
    unittest.main()
