import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from agent_desk.cli import main
from agent_desk.config import load_config

MINIMAL_CONFIG = """
[agent_desk]
data_dir = ".agent-desk"
clone_root = "{clone_root}"

[[repos]]
name = "octo/example"
local_path = "existing"
base_branch = "main"
test_command = "python -m unittest"
""".strip()


def run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


def run_cli_with_exit(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class AddRepoCliTests(unittest.TestCase):
    def _write_config(self, root: Path) -> Path:
        config_path = root / "repos.toml"
        config_path.write_text(
            MINIMAL_CONFIG.format(clone_root=root / "clones"), encoding="utf-8"
        )
        return config_path

    def test_add_repo_appends_new_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            project = root / "newproj"
            project.mkdir()

            code, out = run_cli(
                ["add-repo", "--config", str(config_path), "--path", str(project), "--name", "octo/new"]
            )

            self.assertEqual(code, 0)
            self.assertIn("Added octo/new", out)
            names = {repo.name for repo in load_config(config_path).repos}
            self.assertIn("octo/new", names)

    def test_add_repo_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            project = root / "newproj"
            project.mkdir()
            argv = ["add-repo", "--config", str(config_path), "--path", str(project), "--name", "octo/new"]

            run_cli(argv)
            code, out = run_cli(argv)

            self.assertEqual(code, 0)
            self.assertIn("already configured", out)
            self.assertEqual(config_path.read_text(encoding="utf-8").count('name = "octo/new"'), 1)

    def test_add_repo_missing_folder_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)

            code, out = run_cli(
                ["add-repo", "--config", str(config_path), "--path", str(root / "absent"), "--name", "octo/new"]
            )

            self.assertEqual(code, 1)
            self.assertIn("error:", out)

    def test_add_repo_cannot_infer_name_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            project = root / "no_git"
            project.mkdir()

            code, out = run_cli(
                ["add-repo", "--config", str(config_path), "--path", str(project)]
            )

            self.assertEqual(code, 1)
            self.assertIn("error:", out)

    def test_add_repo_clone_registers_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            (root / "clones" / "octo" / "fresh").mkdir(parents=True)

            code, out = run_cli(
                ["add-repo", "--config", str(config_path), "--clone", "octo/fresh"]
            )

            self.assertEqual(code, 0)
            self.assertIn("Added octo/fresh", out)
            names = {repo.name for repo in load_config(config_path).repos}
            self.assertIn("octo/fresh", names)

    def test_add_repo_requires_path_or_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp))
            with self.assertRaises(SystemExit) as ctx:
                run_cli(["add-repo", "--config", str(config_path)])
            self.assertEqual(ctx.exception.code, 2)

    def test_add_repo_missing_config_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    ["add-repo", "--config", str(Path(tmp) / "absent.toml"), "--path", str(project), "--name", "o/r"]
                )
            self.assertEqual(ctx.exception.code, 2)


class InitConfigCliTests(unittest.TestCase):
    def test_init_config_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repos.toml"

            first_code, first_out = run_cli(["init-config", "--path", str(config_path)])
            second_code, second_out = run_cli(["init-config", "--path", str(config_path)])

            self.assertEqual(first_code, 0)
            self.assertIn("Wrote", first_out)
            self.assertEqual(second_code, 0)
            self.assertIn("already exists", second_out)


class ServeCliTests(unittest.TestCase):
    def test_serve_missing_config_exits_with_setup_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _out, err = run_cli_with_exit(
                ["serve", "--config", str(Path(tmp) / "absent.toml"), "--no-scheduler"]
            )

            self.assertEqual(code, 2)
            self.assertIn("not found; run 'agent-desk init-config' first", err)


class RunJobCliTests(unittest.TestCase):
    def test_run_job_dispatches_to_scheduler(self):
        from agent_desk.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "existing").mkdir()
            config_path = root / "repos.toml"
            config_path.write_text(MINIMAL_CONFIG.format(clone_root=root / "clones"), encoding="utf-8")
            calls = []
            original = Scheduler.run_job
            Scheduler.run_job = lambda self, run_id, kind: calls.append((run_id, kind))
            try:
                code, _ = run_cli(["run-job", "--config", str(config_path), "--run-id", "12", "--kind", "issue"])
            finally:
                Scheduler.run_job = original

            self.assertEqual(code, 0)
            self.assertEqual(calls, [(12, "issue")])


if __name__ == "__main__":
    unittest.main()
