import tempfile
import unittest
from pathlib import Path

from agent_desk.config import load_config


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

[[repos]]
name = "octo/example"
local_path = "target"
base_branch = "main"
test_command = "python -m unittest"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.data_dir, Path(tmp) / ".agent-desk")
        self.assertEqual(config.poll_interval_seconds, 15)
        self.assertEqual(len(config.repos), 1)
        repo = config.repos[0]
        self.assertEqual(repo.name, "octo/example")
        self.assertEqual(repo.local_path, repo_path)
        self.assertEqual(repo.base_branch, "main")
        self.assertEqual(repo.ready_label, "agent:ready")
        self.assertEqual(repo.running_label, "agent:running")
        self.assertEqual(repo.test_command, "python -m unittest")


if __name__ == "__main__":
    unittest.main()
