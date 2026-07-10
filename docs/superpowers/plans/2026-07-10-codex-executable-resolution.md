# Codex Executable Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every real Agent Desk Codex subprocess use an absolute executable path even when the dashboard service PATH does not contain Codex, then resume rstim #437 automatic closeout.

**Architecture:** Add a focused resolver module and apply it once at the shared `CommandRunner` subprocess boundary. Keep business-layer logical argv and fake-runner tests unchanged, while real subprocesses receive a canonical absolute Codex path.

**Tech Stack:** Python 3.11+, standard-library `os`, `pathlib`, `shutil`, `subprocess`, and `unittest`.

## Global Constraints

- Keep runtime dependencies empty.
- Resolution order is `AGENT_DESK_CODEX`, process PATH, then known ChatGPT.app bundle paths.
- Every successful resolver result must be absolute and executable.
- Non-Codex commands and `FakeCommandRunner` behavior must remain unchanged.
- Restore run 181 without losing its PR URL, Codex thread, worktree, CI result, or attempt.
- Do not force GitHub state if automatic closeout reports a new blocker.

---

### Task 1: Centralized Codex executable resolver

**Files:**
- Create: `agent_desk/codex_executable.py`
- Create: `tests/test_codex_executable.py`
- Modify: `agent_desk/worker.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Produces: `resolve_codex_executable(*, environ=None, fallback_candidates=None) -> str`.
- Produces: `resolve_codex_argv(argv: Sequence[str]) -> list[str]`.
- Consumes: logical command argv passed to `CommandRunner.run()`.

- [ ] **Step 1: Write failing resolver and real-runner tests**

Create `tests/test_codex_executable.py`:

```python
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
```

Add `import os` to `tests/test_worker.py`, then add:

```python
def test_command_runner_resolves_codex_to_absolute_executable(self):
    with tempfile.TemporaryDirectory() as tmp:
        executable = Path(tmp) / "bundled-codex"
        executable.write_text(
            "#!/bin/sh\nprintf 'codex-test\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

        with patch.dict(
            os.environ,
            {"PATH": "", "AGENT_DESK_CODEX": str(executable)},
            clear=False,
        ):
            result = CommandRunner().run(["codex", "--version"])

    self.assertEqual(result.returncode, 0)
    self.assertEqual(result.stdout, "codex-test\n")
    self.assertEqual(result.argv[0], str(executable.resolve()))
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_codex_executable tests.test_worker.WorkerTests.test_command_runner_resolves_codex_to_absolute_executable -v`

Expected: import failure because `agent_desk.codex_executable` does not exist.

- [ ] **Step 3: Implement the resolver module**

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil


MACOS_CODEX_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("~/Applications/ChatGPT.app/Contents/Resources/codex"),
)


def _executable_path(value: str, *, search_path: str) -> str:
    candidate = Path(value).expanduser()
    if (candidate.is_absolute() or len(candidate.parts) > 1) and candidate.is_file():
        resolved = candidate.resolve()
        if os.access(resolved, os.X_OK):
            return str(resolved)
    found = shutil.which(value, path=search_path)
    if found:
        return str(Path(found).resolve())
    return ""


def resolve_codex_executable(
    *,
    environ: Mapping[str, str] | None = None,
    fallback_candidates: Sequence[Path] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    search_path = str(env.get("PATH") or "")
    override = str(env.get("AGENT_DESK_CODEX") or "").strip()
    if override:
        resolved = _executable_path(override, search_path=search_path)
        if resolved:
            return resolved
        raise FileNotFoundError(
            f"AGENT_DESK_CODEX does not identify an executable: {override}"
        )
    resolved = _executable_path("codex", search_path=search_path)
    if resolved:
        return resolved
    candidates = MACOS_CODEX_CANDIDATES if fallback_candidates is None else fallback_candidates
    for raw_candidate in candidates:
        candidate = Path(raw_candidate).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise FileNotFoundError(
        "Codex executable not found; set AGENT_DESK_CODEX to its absolute path"
    )


def resolve_codex_argv(argv: Sequence[str]) -> list[str]:
    resolved = list(argv)
    if resolved and resolved[0] == "codex":
        resolved[0] = resolve_codex_executable()
    return resolved
```

- [ ] **Step 4: Resolve argv once in `CommandRunner.run()`**

Add the import:

```python
from .codex_executable import resolve_codex_argv
```

Then begin `CommandRunner.run()` with:

```python
argv = resolve_codex_argv(argv)
started_at = time.monotonic()
```

The method's existing `subprocess.Popen`, `is_codex_json_command`, and
`CommandResult` calls already consume the local `argv` variable, so no other
call-site changes are required.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `python3 -m unittest tests.test_codex_executable tests.test_worker -v`

Expected: all resolver and worker tests pass.

- [ ] **Step 6: Run stripped-PATH smoke verification**

Run:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin /opt/homebrew/bin/python3 -c \
  'from agent_desk.worker import CommandRunner; r = CommandRunner().run(["codex", "--version"]); print(r.argv[0]); print(r.stdout.strip()); raise SystemExit(r.returncode)'
```

Expected: exit zero; printed `result.argv[0]` equals
`/Applications/ChatGPT.app/Contents/Resources/codex`.

- [ ] **Step 7: Run full verification and commit**

Run: `make test`

Expected: every stdlib test passes.

Run: `uvx ruff check agent_desk/codex_executable.py agent_desk/worker.py tests/test_codex_executable.py tests/test_worker.py`

Expected: scoped Ruff check exits zero.

```bash
git add agent_desk/codex_executable.py agent_desk/worker.py tests/test_codex_executable.py tests/test_worker.py docs/superpowers/plans/2026-07-10-codex-executable-resolution.md
git commit -m "fix: resolve Codex executable centrally"
```

### Task 2: Restore and monitor rstim #437 closeout

**Files:**
- Runtime state only: `config/.agent-desk/state/nzy1997__rstim/failed/181.json`

**Interfaces:**
- Consumes: run 181's existing PR URL, thread ID, worktree, and successful CI gate.
- Produces: Agent Desk terminal state `done`, merged PR #447, and closed issue #437.

- [ ] **Step 1: Merge the verified implementation into local `main`**

Fast-forward local `main`, rerun `make test`, then remove the temporary worktree
and branch. Do not push Agent Desk commits unless separately requested.

- [ ] **Step 2: Restore only the closeout state**

Call `Store.update_run` for run 181 with these keyword arguments:

```python
state="pr_open"
stage="retrying automatic closeout after Codex path repair"
last_error=""
ended_at=""
supervisor_pid=""
```

Add an informational `closeout-retry` event. Do not change `attempt`, `pr_url`,
`codex_thread_id`, `worktree_path`, or recorded CI fields.

- [ ] **Step 3: Monitor automatic closeout**

Wait for the dashboard scheduler to discover the restored `pr_open` run. Verify
a new `auto-finish` supervisor and Codex subprocess appear, then monitor until
the run becomes `done`, `blocked`, or `failed`.

- [ ] **Step 4: Verify external outcome**

Run:

```bash
gh pr view 447 --repo nzy1997/rstim --json state,mergedAt,url
gh issue view 437 --repo nzy1997/rstim --json state,url
```

Expected: PR state `MERGED`, issue state `CLOSED`, and Agent Desk run 181 state
`done`. If any differs, report the actual state and evidence.
