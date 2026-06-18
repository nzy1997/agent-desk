import tempfile
import unittest
from pathlib import Path

from agent_desk.config import add_project_to_config, load_config, parse_github_repo_name


class ConfigTests(unittest.TestCase):
    def test_loads_repo_defaults_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repos.toml"
            repo_path = Path(tmp) / "target"
            config_path.write_text(
                """
[agent_desk]
data_dir = ".agent-desk"
poll_interval_seconds = 15
worker_idle_timeout_seconds = 33

[[repos]]
name = "octo/example"
local_path = "target"
base_branch = "main"
test_command = "python -m unittest"
closeout_sandbox = "danger-full-access"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.data_dir, Path(tmp) / ".agent-desk")
        self.assertEqual(config.poll_interval_seconds, 15)
        self.assertEqual(config.worker_idle_timeout_seconds, 33)
        self.assertEqual(len(config.repos), 1)
        repo = config.repos[0]
        self.assertEqual(repo.name, "octo/example")
        self.assertEqual(repo.local_path, repo_path)
        self.assertEqual(repo.base_branch, "main")
        self.assertEqual(repo.ready_label, "agent:ready")
        self.assertEqual(repo.running_label, "agent:running")
        self.assertEqual(repo.test_command, "python -m unittest")
        self.assertEqual(repo.closeout_sandbox, "danger-full-access")

    def test_parse_github_repo_name_from_common_remote_urls(self):
        self.assertEqual(parse_github_repo_name("git@github.com:octo/example.git"), "octo/example")
        self.assertEqual(parse_github_repo_name("https://github.com/octo/example.git"), "octo/example")
        self.assertEqual(parse_github_repo_name("https://github.com/octo/example"), "octo/example")

    def test_add_project_to_config_appends_repo_from_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "example"
            project.mkdir()
            config_path = root / "repos.toml"
            config_path.write_text(
                """
[agent_desk]
data_dir = ".agent-desk"

[[repos]]
name = "octo/template"
local_path = "/template"
base_branch = "main"
test_command = "python -m unittest"
mutate_github = true
push_pr = true
closeout_sandbox = "danger-full-access"
""".strip(),
                encoding="utf-8",
            )

            added = add_project_to_config(config_path, project, repo_name="octo/example")
            config = load_config(config_path)

        self.assertEqual(added.name, "octo/example")
        self.assertEqual(added.local_path, project.resolve())
        self.assertEqual(len(config.repos), 2)
        self.assertEqual(config.repos[1].name, "octo/example")
        self.assertEqual(config.repos[1].local_path, project.resolve())
        self.assertEqual(config.repos[1].test_command, "python -m unittest")
        self.assertTrue(config.repos[1].mutate_github)
        self.assertTrue(config.repos[1].push_pr)
        self.assertEqual(config.repos[1].closeout_sandbox, "danger-full-access")

    def test_add_project_to_config_is_idempotent_for_existing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "example"
            project.mkdir()
            config_path = root / "repos.toml"
            config_path.write_text(
                f"""
[agent_desk]
data_dir = ".agent-desk"

[[repos]]
name = "octo/example"
local_path = "{project}"
""".strip(),
                encoding="utf-8",
            )

            add_project_to_config(config_path, project, repo_name="octo/example")
            config = load_config(config_path)

        self.assertEqual(len(config.repos), 1)


if __name__ == "__main__":
    unittest.main()
