# Subagent-Aware Activity Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Agent Desk from idle-killing a parent Codex run while its spawned child threads are still producing local Codex session activity.

**Architecture:** Add a stdlib-only `CodexThreadActivityMonitor` that tails the parent Codex JSONL stream, discovers child thread ids, watches corresponding local Codex rollout files, and reports activity to `CommandRunner`. `CommandRunner` keeps the existing total timeout and idle timeout behavior, but refreshes idle activity when a known descendant rollout file changes.

**Tech Stack:** Python 3.11+ standard library only; `unittest`; local Codex JSONL rollout files under `$CODEX_HOME/sessions/**/rollout-*<thread_id>.jsonl`.

## Global Constraints

- Keep Agent Desk dependency-free: no new Python package dependencies.
- Preserve `worker_timeout_seconds` as a hard total runtime cap.
- Preserve the default idle window and existing stdout/stderr-only behavior when no child threads are discovered.
- Activate descendant monitoring only for `codex exec --json` and `codex exec resume --json` commands.
- Monitor failures must not keep a run alive.
- Do not depend on Codex app MCP tools or local sqlite schema stability.

---

### Task 1: Codex Thread Activity Monitor

**Files:**
- Create: `agent_desk/codex_activity.py`
- Create: `tests/test_codex_activity.py`

**Interfaces:**
- Consumes: parent Codex stdout JSONL lines and local Codex rollout files.
- Produces:
  - `ActivitySignal(active: bool, source: str = "", detail: str = "")`
  - `CodexThreadActivityMonitor(stdout_path: Path, codex_home: Path | None = None, poll_interval_seconds: float = 5.0)`
  - `CodexThreadActivityMonitor.poll(now: float | None = None) -> ActivitySignal`
  - `extract_thread_ids_from_payload(payload: Any) -> set[str]`

- [ ] **Step 1: Write failing monitor parser tests**

Add `tests/test_codex_activity.py`:

```python
import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_desk.codex_activity import (
    CodexThreadActivityMonitor,
    extract_thread_ids_from_payload,
)


class CodexActivityTests(unittest.TestCase):
    def test_extracts_spawn_agent_thread_ids_from_nested_payloads(self):
        child = "019f1e7f-2c4c-7063-af43-6e97371de397"
        payload = {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "receiver_thread_ids": [child],
            },
        }

        self.assertEqual(extract_thread_ids_from_payload(payload), {child})

    def test_extracts_thread_id_from_json_string_tool_output(self):
        child = "019f1e7f-2c4c-7063-af43-6e97371de397"
        payload = {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": json.dumps({"agent_id": child, "nickname": "Sartre"}),
            },
        }

        self.assertEqual(extract_thread_ids_from_payload(payload), {child})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run parser tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_codex_activity.CodexActivityTests.test_extracts_spawn_agent_thread_ids_from_nested_payloads tests.test_codex_activity.CodexActivityTests.test_extracts_thread_id_from_json_string_tool_output -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_desk.codex_activity'`.

- [ ] **Step 3: Implement parser and data classes**

Create `agent_desk/codex_activity.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any


THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class ActivitySignal:
    active: bool
    source: str = ""
    detail: str = ""


@dataclass(frozen=True)
class FileState:
    mtime_ns: int
    size: int


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _looks_like_thread_id(value: str) -> bool:
    return bool(THREAD_ID_RE.match(value))


def extract_thread_ids_from_payload(payload: Any) -> set[str]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"agent_id", "child_thread_id", "thread_id"} and isinstance(item, str):
                    if _looks_like_thread_id(item):
                        found.add(item)
                elif key in {"receiver_thread_ids", "targets"} and isinstance(item, list):
                    for candidate in item:
                        if isinstance(candidate, str) and _looks_like_thread_id(candidate):
                            found.add(candidate)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            stripped = value.strip()
            if _looks_like_thread_id(stripped):
                found.add(stripped)
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    visit(json.loads(stripped))
                except json.JSONDecodeError:
                    return

    visit(payload)
    return found
```

- [ ] **Step 4: Run parser tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_codex_activity.CodexActivityTests.test_extracts_spawn_agent_thread_ids_from_nested_payloads tests.test_codex_activity.CodexActivityTests.test_extracts_thread_id_from_json_string_tool_output -v
```

Expected: PASS.

- [ ] **Step 5: Write failing rollout activity tests**

Append these tests to `CodexActivityTests`:

```python
    def test_monitor_discovers_child_rollout_and_reports_activity_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            stdout_path.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "receiver_thread_ids": [child],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )

            first = monitor.poll(now=time.monotonic())
            with child_rollout.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"agent_message","text":"still working"}\n')
            second = monitor.poll(now=time.monotonic())

        self.assertTrue(first.active)
        self.assertIn(child, first.detail)
        self.assertTrue(second.active)
        self.assertIn("child thread", second.source)

    def test_monitor_discovers_grandchild_from_child_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            grandchild = "019f1e80-1111-7222-8333-444455556666"
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text(
                json.dumps({"payload": {"output": json.dumps({"agent_id": grandchild})}}) + "\n",
                encoding="utf-8",
            )
            grandchild_rollout = sessions / f"rollout-2026-07-02T00-25-00-{grandchild}.jsonl"
            grandchild_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            stdout_path.write_text(
                json.dumps({"item": {"receiver_thread_ids": [child]}}) + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )

            monitor.poll(now=time.monotonic())
            with grandchild_rollout.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"agent_message","text":"grandchild update"}\n')
            signal = monitor.poll(now=time.monotonic())

        self.assertTrue(signal.active)
        self.assertIn(grandchild, signal.detail)
```

- [ ] **Step 6: Run rollout tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_codex_activity.CodexActivityTests.test_monitor_discovers_child_rollout_and_reports_activity_on_change tests.test_codex_activity.CodexActivityTests.test_monitor_discovers_grandchild_from_child_rollout -v
```

Expected: FAIL with `NameError` or `AttributeError` because `CodexThreadActivityMonitor` is not implemented yet.

- [ ] **Step 7: Implement `CodexThreadActivityMonitor`**

Append this implementation to `agent_desk/codex_activity.py`:

```python
class CodexThreadActivityMonitor:
    def __init__(
        self,
        stdout_path: Path,
        *,
        codex_home: Path | None = None,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self.stdout_path = Path(stdout_path)
        self.codex_home = codex_home if codex_home is not None else default_codex_home()
        self.poll_interval_seconds = poll_interval_seconds
        self.thread_ids: set[str] = set()
        self._stdout_offset = 0
        self._rollout_offsets: dict[Path, int] = {}
        self._rollout_paths: dict[str, Path] = {}
        self._file_states: dict[Path, FileState] = {}
        self._last_poll_at = 0.0
        self._disabled = False

    def poll(self, *, now: float | None = None) -> ActivitySignal:
        if self._disabled:
            return ActivitySignal(False)
        current = time.monotonic() if now is None else now
        if current - self._last_poll_at < self.poll_interval_seconds:
            return ActivitySignal(False)
        self._last_poll_at = current
        try:
            new_ids = self._discover_from_stdout()
            for thread_id in list(self.thread_ids):
                path = self._rollout_path_for_thread(thread_id)
                if path is not None:
                    new_ids.update(self._discover_from_rollout(path))
            for thread_id in new_ids:
                self.thread_ids.add(thread_id)

            active_signals: list[ActivitySignal] = []
            for thread_id in sorted(self.thread_ids):
                path = self._rollout_path_for_thread(thread_id)
                if path is None:
                    continue
                state = self._stat_file(path)
                previous = self._file_states.get(path)
                self._file_states[path] = state
                if previous is None:
                    active_signals.append(
                        ActivitySignal(True, "child thread discovered", thread_id)
                    )
                elif previous != state:
                    active_signals.append(
                        ActivitySignal(True, "child thread activity", thread_id)
                    )
            return active_signals[-1] if active_signals else ActivitySignal(False)
        except OSError:
            self._disabled = True
            return ActivitySignal(False)

    def _discover_from_stdout(self) -> set[str]:
        if not self.stdout_path.exists():
            return set()
        found: set[str] = set()
        with self.stdout_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._stdout_offset)
            for line in handle:
                found.update(self._ids_from_line(line))
            self._stdout_offset = handle.tell()
        return found

    def _discover_from_rollout(self, path: Path) -> set[str]:
        found: set[str] = set()
        offset = self._rollout_offsets.get(path, 0)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            for line in handle:
                found.update(self._ids_from_line(line))
            self._rollout_offsets[path] = handle.tell()
        return found

    def _ids_from_line(self, line: str) -> set[str]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return set()
        return extract_thread_ids_from_payload(payload)

    def _rollout_path_for_thread(self, thread_id: str) -> Path | None:
        cached = self._rollout_paths.get(thread_id)
        if cached is not None and cached.exists():
            return cached
        sessions = self.codex_home / "sessions"
        if not sessions.exists():
            return None
        matches = sorted(sessions.rglob(f"rollout-*{thread_id}.jsonl"))
        if not matches:
            return None
        self._rollout_paths[thread_id] = matches[-1]
        return matches[-1]

    def _stat_file(self, path: Path) -> FileState:
        stat = path.stat()
        return FileState(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
```

- [ ] **Step 8: Run monitor tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_codex_activity -v
```

Expected: PASS.

- [ ] **Step 9: Commit monitor**

```bash
git add agent_desk/codex_activity.py tests/test_codex_activity.py
git commit -m "feat: monitor codex child thread activity"
```

---

### Task 2: CommandRunner Activity Integration

**Files:**
- Modify: `agent_desk/worker.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Consumes: `CodexThreadActivityMonitor.poll() -> ActivitySignal`.
- Produces:
  - `is_codex_json_command(argv: list[str]) -> bool`
  - `CommandRunner.run(..., activity_monitor: CodexThreadActivityMonitor | None = None) -> CommandResult`
  - idle timeout messages that include `last activity: <source>`.

- [ ] **Step 1: Write failing command-runner integration tests**

Modify imports in `tests/test_worker.py`:

```python
import json
import threading
import time
```

Change the worker import line to include `is_codex_json_command`:

```python
from agent_desk.codex_activity import CodexThreadActivityMonitor
from agent_desk.worker import (
    CommandResult,
    CommandRunner,
    FakeCommandRunner,
    Worker,
    extract_thread_id,
    is_codex_json_command,
)
```

Add these tests near the existing `test_command_runner_streams_logs_and_kills_idle_process`:

```python
    def test_is_codex_json_command_detects_exec_and_resume(self):
        self.assertTrue(is_codex_json_command(["codex", "exec", "--json"]))
        self.assertTrue(is_codex_json_command(["codex", "exec", "resume", "--json"]))
        self.assertFalse(is_codex_json_command(["codex", "exec"]))
        self.assertFalse(is_codex_json_command([sys.executable, "-c", "print('x')"]))

    def test_command_runner_counts_child_thread_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            stderr_path = root / "stderr.log"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")

            def write_child_activity():
                time.sleep(0.08)
                for index in range(5):
                    with child_rollout.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"event": index}) + "\n")
                    time.sleep(0.08)

            writer = threading.Thread(target=write_child_activity)
            writer.start()
            script = (
                "import json, time; "
                f"print(json.dumps({{'item': {{'receiver_thread_ids': ['{child}']}}}}), flush=True); "
                "time.sleep(0.55)"
            )

            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0.02,
            )
            result = CommandRunner().run(
                [sys.executable, "-c", script],
                timeout=5,
                idle_timeout=0.2,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                activity_monitor=monitor,
            )
            writer.join(timeout=1)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.timeout_reason, "")

    def test_command_runner_times_out_when_child_thread_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            stderr_path = root / "stderr.log"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            script = (
                "import json, time; "
                f"print(json.dumps({{'item': {{'receiver_thread_ids': ['{child}']}}}}), flush=True); "
                "time.sleep(3)"
            )

            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0.02,
            )
            result = CommandRunner().run(
                [sys.executable, "-c", script],
                timeout=5,
                idle_timeout=0.2,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                activity_monitor=monitor,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.timeout_reason, "idle")
        self.assertIn("last activity:", result.stderr)
```

- [ ] **Step 2: Run integration tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_worker.WorkerTests.test_is_codex_json_command_detects_exec_and_resume tests.test_worker.WorkerTests.test_command_runner_counts_child_thread_activity tests.test_worker.WorkerTests.test_command_runner_times_out_when_child_thread_is_stale -v
```

Expected: FAIL because `is_codex_json_command` and `activity_monitor` do not exist on the runner yet.

- [ ] **Step 3: Implement command detection and runner activity updates**

Modify `agent_desk/worker.py` imports:

```python
from collections.abc import Sequence
```

Add:

```python
from .codex_activity import CodexThreadActivityMonitor
```

Add this helper above `CommandRunner`:

```python
def is_codex_json_command(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    if Path(argv[0]).name != "codex":
        return False
    return "exec" in argv and "--json" in argv
```

Change `CommandRunner.run()` signature:

```python
        activity_monitor: CodexThreadActivityMonitor | None = None,
        activity_monitor_poll_interval: float = 5.0,
```

Replace `last_output_at` with activity helpers:

```python
        last_activity_at = started_at
        last_activity_source = "process start"
```

Inside `run()`, add:

```python
        def mark_activity(source: str) -> None:
            nonlocal last_activity_at, last_activity_source
            with lock:
                last_activity_at = time.monotonic()
                last_activity_source = source
```

Update `append_output`:

```python
        def append_output(chunks: list[str], handle: Any, text: str, *, counts_as_activity: bool = True) -> None:
            nonlocal last_activity_at, last_activity_source
            with lock:
                chunks.append(text)
                if counts_as_activity:
                    last_activity_at = time.monotonic()
                    last_activity_source = "parent output"
                if handle:
                    handle.write(text)
                    handle.flush()
```

Update `read_stream` to pass `counts_as_activity=True`.

After starting reader threads and closing stdin, create the monitor:

```python
        if (
            activity_monitor is None
            and stdout_path is not None
            and is_codex_json_command(argv)
        ):
            activity_monitor = CodexThreadActivityMonitor(
                stdout_path,
                poll_interval_seconds=activity_monitor_poll_interval,
            )
```

Inside the poll loop, before timeout checks:

```python
            if activity_monitor is not None:
                signal = activity_monitor.poll(now=now)
                if signal.active:
                    mark_activity(f"{signal.source} {signal.detail}".strip())
```

Change idle checks and timeout message to use activity:

```python
            if idle_timeout is not None and now - last_activity_at >= idle_timeout:
                timeout_reason = "idle"
                break
```

```python
            idle_for = time.monotonic() - last_activity_at
            append_output(
                stderr_chunks,
                stderr_handle,
                f"\nagent-desk: {timeout_reason} timeout killed process after {elapsed:.1f}s"
                f" (idle for {idle_for:.1f}s; last activity: {last_activity_source})\n",
                counts_as_activity=False,
            )
```

- [ ] **Step 4: Run integration tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_worker.WorkerTests.test_is_codex_json_command_detects_exec_and_resume tests.test_worker.WorkerTests.test_command_runner_counts_child_thread_activity tests.test_worker.WorkerTests.test_command_runner_times_out_when_child_thread_is_stale tests.test_worker.WorkerTests.test_command_runner_streams_logs_and_kills_idle_process -v
```

Expected: PASS.

- [ ] **Step 5: Commit runner integration**

```bash
git add agent_desk/worker.py tests/test_worker.py
git commit -m "feat: count child codex activity for idle timeout"
```

---

### Task 3: Compatibility And Existing Timeout Semantics

**Files:**
- Modify: `agent_desk/worker.py`
- Modify: `tests/test_worker.py`
- Verify: `tests/test_continuation.py`

**Interfaces:**
- Consumes: new `CommandRunner.run()` optional keyword parameters.
- Produces: unchanged `FakeCommandRunner` behavior and unchanged worker/continuation call recording.

- [ ] **Step 1: Write failing compatibility assertions**

In `tests/test_worker.py`, add this assertion to `test_command_runner_streams_logs_and_kills_idle_process`:

```python
        self.assertIn("last activity:", result.stderr)
```

In `tests/test_continuation.py`, add no new test body; the existing
`test_request_changes_resumes_original_codex_thread_with_feedback` must still pass and prove `FakeCommandRunner` accepts the unchanged continuation call path.

- [ ] **Step 2: Run focused compatibility tests**

Run:

```bash
python3 -m unittest tests.test_worker.WorkerTests.test_command_runner_streams_logs_and_kills_idle_process tests.test_continuation.ContinuationTests.test_request_changes_resumes_original_codex_thread_with_feedback -v
```

Expected: PASS.

- [ ] **Step 3: Update fake runner signature**

Update `FakeCommandRunner.run()` in `agent_desk/worker.py` so tests and future callers can pass the same optional activity-monitor keywords as `CommandRunner.run()`:

```python
        activity_monitor: CodexThreadActivityMonitor | None = None,
        activity_monitor_poll_interval: float = 5.0,
```

Do not add these fields to `CommandCall`; tests only need to verify `timeout` and `idle_timeout`.

- [ ] **Step 4: Run broader worker and continuation tests**

Run:

```bash
python3 -m unittest tests.test_worker tests.test_continuation -v
```

Expected: PASS.

- [ ] **Step 5: Commit compatibility cleanup**

```bash
git add agent_desk/worker.py tests/test_worker.py
git commit -m "test: preserve timeout runner compatibility"
```

Task 3 always commits the compatibility assertion and fake-runner signature alignment.

---

### Task 4: Final Verification And Documentation Check

**Files:**
- Verify: `agent_desk/codex_activity.py`
- Verify: `agent_desk/worker.py`
- Verify: `tests/test_codex_activity.py`
- Verify: `tests/test_worker.py`
- Verify: `tests/test_continuation.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: final verified branch.

- [ ] **Step 1: Run full test suite**

Run:

```bash
make test
```

Expected: PASS.

- [ ] **Step 2: Inspect final diff**

Run:

```bash
git status --short
git diff --stat HEAD~3..HEAD
```

Expected: only the monitor, runner integration, tests, spec, and plan changes are present.

- [ ] **Step 3: Manual sanity-check live run artifacts**

Run:

```bash
python3 -m unittest tests.test_codex_activity tests.test_worker.WorkerTests.test_command_runner_counts_child_thread_activity -v
```

Expected: PASS. This proves the fake child rollout activity prevents idle kill while parent stdout is quiet.

- [ ] **Step 4: Final notes**

Record in the implementation summary:

```text
The hard worker_timeout_seconds cap is unchanged.
The idle timeout still fires when parent output and known descendant rollout files are all quiet.
Monitoring is stdlib-only and uses local Codex rollout JSONL file activity.
```
