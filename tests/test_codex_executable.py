import tempfile
import unittest
from pathlib import Path

from agent_desk.codex_executable import resolve_codex_executable


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class CodexExecutableTests(unittest.TestCase):
    def test_environment_override_returns_absolute_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = make_executable(Path(tmp) / "custom-codex")

            resolved = resolve_codex_executable(
                environ={"PATH": "", "AGENT_DESK_CODEX": str(executable)},
                fallback_candidates=[],
            )

        self.assertEqual(resolved, str(executable.resolve()))

    def test_path_lookup_returns_absolute_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            executable = make_executable(bin_dir / "codex")

            resolved = resolve_codex_executable(
                environ={"PATH": str(bin_dir)},
                fallback_candidates=[],
            )

        self.assertEqual(resolved, str(executable.resolve()))

    def test_falls_back_to_bundled_executable_when_path_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = make_executable(Path(tmp) / "ChatGPT.app" / "codex")

            resolved = resolve_codex_executable(
                environ={"PATH": ""},
                fallback_candidates=[executable],
            )

        self.assertEqual(resolved, str(executable.resolve()))

    def test_missing_executable_names_environment_override(self):
        with self.assertRaisesRegex(FileNotFoundError, "AGENT_DESK_CODEX"):
            resolve_codex_executable(environ={"PATH": ""}, fallback_candidates=[])
