import tempfile
import types
import unittest
from pathlib import Path

from agent_desk.config import (
    add_remote_repo_to_config,
    clone_repo,
    load_config,
    parse_repo_spec,
)

TEMPLATE_CONFIG = """
[agent_desk]
data_dir = "data"
clone_root = "{clone_root}"

[[repos]]
name = "octo/example"
local_path = "existing"
base_branch = "main"
test_command = "python -m unittest"
""".strip()


def make_runner(create: bool = True, returncode: int = 0, stderr: str = ""):
    calls = []

    def runner(cmd, capture_output, text):
        calls.append(cmd)
        if create and returncode == 0:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    runner.calls = calls
    return runner


class ParseRepoSpecTests(unittest.TestCase):
    def test_plain_owner_repo(self):
        self.assertEqual(parse_repo_spec("octo/Example"), "octo/Example")

    def test_strips_git_suffix_and_trailing_slash(self):
        self.assertEqual(parse_repo_spec("octo/Example.git/"), "octo/Example")

    def test_https_and_ssh_urls(self):
        self.assertEqual(parse_repo_spec("https://github.com/octo/Example.git"), "octo/Example")
        self.assertEqual(parse_repo_spec("git@github.com:octo/Example.git"), "octo/Example")

    def test_unparseable_returns_empty(self):
        self.assertEqual(parse_repo_spec("not a repo"), "")


class CloneRepoTests(unittest.TestCase):
    def test_reuses_existing_target_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "octo" / "new").mkdir(parents=True)
            runner = make_runner()
            target = clone_repo(root, "octo/new", runner=runner)
            self.assertEqual(target, (root / "octo" / "new").resolve())
            self.assertEqual(runner.calls, [])

    def test_runs_clone_for_missing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = make_runner()
            target = clone_repo(root, "octo/new", runner=runner)
            self.assertTrue(target.is_dir())
            self.assertEqual(runner.calls[0][:3], ["gh", "repo", "clone"])

    def test_unparseable_spec_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                clone_repo(Path(tmp), "not a repo")

    def test_target_that_is_a_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "octo").mkdir()
            (root / "octo" / "new").write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                clone_repo(root, "octo/new")

    def test_clone_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = make_runner(create=False, returncode=1, stderr="boom")
            with self.assertRaises(ValueError) as ctx:
                clone_repo(Path(tmp), "octo/new", runner=runner)
            self.assertIn("boom", str(ctx.exception))


class AddRemoteRepoToConfigTests(unittest.TestCase):
    def test_clones_and_registers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            clones = root / "clones"
            clones.mkdir()
            config_path = root / "repos.toml"
            config_path.write_text(
                TEMPLATE_CONFIG.format(clone_root=clones), encoding="utf-8"
            )
            repo = add_remote_repo_to_config(config_path, "octo/new", runner=make_runner())
            self.assertEqual(repo.name, "octo/new")
            self.assertEqual(repo.local_path, (clones / "octo" / "new").resolve())
            names = {item.name for item in load_config(config_path).repos}
            self.assertIn("octo/new", names)


if __name__ == "__main__":
    unittest.main()
