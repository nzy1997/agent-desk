# Dashboard Shutdown All Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a controlled `Shutdown all` dashboard action that records interrupted work, stops Agent Desk CLI process groups, and provides a working dashboard `Resume` button for recoverable interrupted runs.

**Architecture:** Add an `interrupted` run state and a focused `agent_desk.shutdown` module for shutdown preview, manifest writing, resume metadata recovery, and process-group signaling through an injectable controller. Scheduler owns state transitions and detached resume dispatch; ContinuationRunner owns the `codex exec resume` worker-contract flow. Dashboard exposes preview, shutdown, and resume endpoints plus header/run-card UI.

**Tech Stack:** Python 3.11+ standard library only, filesystem-backed Store JSON records, `unittest`, existing dashboard HTML/JS, detached `agent-desk run-job` supervisors.

## Global Constraints

- Keep the project stdlib-only; do not add runtime dependencies.
- Do not delete run directories, worktrees, branches, logs, or PR metadata during shutdown.
- Treat user-initiated shutdown as `interrupted`, not ordinary `failed`.
- Do not show a dashboard `Resume` button unless both `codex_thread_id` and `worktree_path` are available.
- All process signaling must be injectable in tests; unit tests must not kill real processes.
- Preserve existing `Restart` behavior: restart must not kill detached jobs.
- The current worktree may contain unrelated dashboard UI edits; stage only files intentionally changed by each task.

---

## File Structure

- `agent_desk/store.py`: add `interrupted` to valid states and terminal states.
- `agent_desk/dashboard.py`: expose resume metadata in state payload, add shutdown/resume HTTP routes, wire shutdown callback to server exit.
- `agent_desk/static/dashboard.html`: add the `Shutdown all` header button.
- `agent_desk/static/dashboard.js`: add shutdown preview/confirm flow and interrupted run-card actions.
- `agent_desk/shutdown.py`: new focused module for process snapshots, resume metadata recovery, shutdown previews, manifests, and injectable process-group termination.
- `agent_desk/scheduler.py`: add `shutdown_preview()`, `shutdown_all()`, `resume_interrupted()`, `_run_resume_interrupted()`, and `run-job` dispatch for `resume-interrupted`.
- `agent_desk/continuation.py`: add `resume_interrupted()` and shared helpers for running `codex exec resume` with worker-result handling.
- `tests/test_shutdown.py`: new tests for shutdown helper functions and mocked process signaling.
- `tests/test_scheduler.py`: scheduler state, shutdown, reconciliation, and resume dispatch tests.
- `tests/test_dashboard.py`: state payload and HTTP route tests.
- `tests/test_continuation.py`: interrupted resume prompt and worker-result handling tests.

---

### Task 1: Add Interrupted State And Resume Availability

**Files:**
- Modify: `agent_desk/store.py`
- Modify: `agent_desk/dashboard.py`
- Modify: `agent_desk/static/dashboard.js`
- Test: `tests/test_store.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: Store accepts `state="interrupted"`.
- Produces: `build_state_payload()` adds `resume_available: bool` and `resume_unavailable_reason: str` to every run.
- Produces: dashboard attention list includes `interrupted` runs.

- [ ] **Step 1: Write failing Store test for interrupted as terminal**

Add this test to `tests/test_store.py`:

```python
def test_interrupted_is_terminal_and_counted(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=5,
            issue_title="Shutdown",
            issue_url="https://example.test/5",
            branch_name="agent/issue-5",
        )

        store.update_run(run_id, state="interrupted", stage="interrupted by shutdown")
        run = store.get_run(run_id)
        state = store.dashboard_state()

    self.assertEqual(run["state"], "interrupted")
    self.assertEqual(run["stage"], "interrupted by shutdown")
    self.assertTrue(run["ended_at"])
    self.assertEqual(state["stats"]["interrupted"], 1)
```

- [ ] **Step 2: Run Store test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_store.StoreTests.test_interrupted_is_terminal_and_counted -v
```

Expected: FAIL or ERROR because `interrupted` is not a valid terminal state and/or no `ended_at` is set.

- [ ] **Step 3: Implement interrupted state in Store**

In `agent_desk/store.py`, change the constants to:

```python
TERMINAL_STATES = {"done", "failed", "blocked", "pr_open", "needs_review", "interrupted"}

RUN_STATES = (
    "queued",
    "ready",
    "running",
    "pr_open",
    "needs_review",
    "blocked",
    "done",
    "failed",
    "interrupted",
)
```

Leave `_OPEN_EXCLUDE` unchanged unless a test proves otherwise; `interrupted`
must continue to block duplicate issue intake until an explicit resume/retry
path is chosen.

- [ ] **Step 4: Run Store test to verify it passes**

Run:

```bash
python3 -m unittest tests.test_store.StoreTests.test_interrupted_is_terminal_and_counted -v
```

Expected: PASS.

- [ ] **Step 5: Write failing dashboard state payload tests**

Add these tests to `tests/test_dashboard.py` near the existing resume command tests:

```python
def test_state_payload_marks_interrupted_run_resume_available(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        worktree = root / "worktree"
        worktree.mkdir()
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=5,
            issue_title="Shutdown",
            issue_url="https://example.test/5",
            branch_name="agent/issue-5",
        )
        store.update_run(
            run_id,
            state="interrupted",
            stage="interrupted by shutdown",
            codex_thread_id="019ed932-fe5d-7391-b856-98b2239a6380",
            worktree_path=str(worktree),
        )

        payload = build_state_payload(store)
        run = payload["runs"][0]

    self.assertTrue(run["resume_available"])
    self.assertEqual(run["resume_unavailable_reason"], "")
    self.assertIn("codex resume", run["resume_command"])

def test_state_payload_marks_interrupted_run_resume_unavailable_without_thread(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        worktree = root / "worktree"
        worktree.mkdir()
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=5,
            issue_title="Shutdown",
            issue_url="https://example.test/5",
            branch_name="agent/issue-5",
        )
        store.update_run(
            run_id,
            state="interrupted",
            stage="interrupted by shutdown",
            worktree_path=str(worktree),
        )

        payload = build_state_payload(store)
        run = payload["runs"][0]

    self.assertFalse(run["resume_available"])
    self.assertEqual(run["resume_unavailable_reason"], "no Codex thread captured")
```

- [ ] **Step 6: Run dashboard state tests to verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_dashboard.DashboardTests.test_state_payload_marks_interrupted_run_resume_available \
  tests.test_dashboard.DashboardTests.test_state_payload_marks_interrupted_run_resume_unavailable_without_thread \
  -v
```

Expected: FAIL because `resume_available` and `resume_unavailable_reason` are missing.

- [ ] **Step 7: Implement resume availability in state payload**

In `agent_desk/dashboard.py`, add helpers near `run_display_key`:

```python
def resume_unavailable_reason(run: dict[str, Any]) -> str:
    if str(run.get("state") or "") != "interrupted":
        return ""
    if not str(run.get("codex_thread_id") or ""):
        return "no Codex thread captured"
    if not str(run.get("worktree_path") or ""):
        return "no worktree path recorded"
    return ""


def enrich_resume_fields(run: dict[str, Any]) -> None:
    reason = resume_unavailable_reason(run)
    run["resume_available"] = str(run.get("state") or "") == "interrupted" and not reason
    run["resume_unavailable_reason"] = reason
```

Then call `enrich_resume_fields(run)` in `build_state_payload()` after setting
`resume_command`.

Update `RUN_DISPLAY_ORDER`:

```python
RUN_DISPLAY_ORDER = {
    "running": 0,
    "interrupted": 1,
    "pr_open": 2,
    "needs_review": 3,
    "failed": 4,
    "ready": 5,
    "blocked": 6,
    "done": 7,
}
```

- [ ] **Step 8: Show interrupted runs in attention UI**

In `agent_desk/static/dashboard.js`, change the attention filter to:

```javascript
.filter(run => ['blocked','failed','interrupted','needs_review'].includes(run.state))
```

- [ ] **Step 9: Run focused tests**

Run:

```bash
python3 -m unittest \
  tests.test_store.StoreTests.test_interrupted_is_terminal_and_counted \
  tests.test_dashboard.DashboardTests.test_state_payload_marks_interrupted_run_resume_available \
  tests.test_dashboard.DashboardTests.test_state_payload_marks_interrupted_run_resume_unavailable_without_thread \
  -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add agent_desk/store.py agent_desk/dashboard.py agent_desk/static/dashboard.js tests/test_store.py tests/test_dashboard.py
git commit -m "Add interrupted run state"
```

---

### Task 2: Add Shutdown Preview, Artifacts, And Safe Process Signaling Helpers

**Files:**
- Create: `agent_desk/shutdown.py`
- Test: `tests/test_shutdown.py`

**Interfaces:**
- Produces: `ProcessInfo(pid: int, ppid: int, pgid: int, command: str)`
- Produces: `ProcessController` with `process_info(pid)`, `process_group(pgid)`, `terminate_group(pgid)`, `kill_group(pgid)`, and `pid_alive(pid)`.
- Produces: `recover_thread_id_from_run(run: dict[str, Any]) -> str`
- Produces: `build_run_shutdown_item(run, controller) -> dict[str, Any]`
- Produces: `write_shutdown_artifacts(config, shutdown_id, items, dashboard_pid, config_path, extra_fields=None) -> dict[str, Any]`
- Produces: `stop_verified_process_groups(items, controller, grace_seconds=3.0) -> list[dict[str, Any]]`

- [ ] **Step 1: Write failing tests for thread recovery**

Create `tests/test_shutdown.py` with:

```python
import json
import tempfile
import unittest
from pathlib import Path

from agent_desk.shutdown import recover_thread_id_from_run


class ShutdownTests(unittest.TestCase):
    def test_recover_thread_id_prefers_store_value(self):
        run = {"codex_thread_id": "stored", "run_dir": ""}

        self.assertEqual(recover_thread_id_from_run(run), "stored")

    def test_recover_thread_id_scans_known_stdout_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "fix-ci-1.stdout.jsonl").write_text(
                json.dumps({"type": "thread.started", "thread_id": "from-log"}) + "\n",
                encoding="utf-8",
            )
            run = {"codex_thread_id": "", "run_dir": str(run_dir)}

            self.assertEqual(recover_thread_id_from_run(run), "from-log")
```

- [ ] **Step 2: Run thread recovery tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_shutdown.ShutdownTests.test_recover_thread_id_prefers_store_value tests.test_shutdown.ShutdownTests.test_recover_thread_id_scans_known_stdout_logs -v
```

Expected: ERROR because `agent_desk.shutdown` does not exist.

- [ ] **Step 3: Implement thread recovery helper**

Create `agent_desk/shutdown.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Protocol

from .config import AgentDeskConfig
from .worker import extract_thread_id, format_resume_command


THREAD_LOG_NAMES = (
    "stdout.jsonl",
    "request-changes.stdout.jsonl",
    "approve-finish.stdout.jsonl",
    "auto-finish.stdout.jsonl",
    "open-pr.stdout.jsonl",
    "fix-ci-1.stdout.jsonl",
    "fix-ci-2.stdout.jsonl",
    "fix-ci-3.stdout.jsonl",
    "resume-interrupted.stdout.jsonl",
)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    pgid: int
    command: str


class ProcessController(Protocol):
    def process_info(self, pid: int) -> ProcessInfo | None: ...
    def process_group(self, pgid: int) -> list[ProcessInfo]: ...
    def terminate_group(self, pgid: int) -> None: ...
    def kill_group(self, pgid: int) -> None: ...
    def pid_alive(self, pid: int) -> bool: ...


def shutdown_id(now: datetime | None = None) -> str:
    stamp = now or datetime.now(UTC)
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def recover_thread_id_from_run(run: dict[str, Any]) -> str:
    thread_id = str(run.get("codex_thread_id") or "")
    if thread_id:
        return thread_id
    run_dir = Path(str(run.get("run_dir") or ""))
    if not run_dir.is_dir():
        return ""
    for name in THREAD_LOG_NAMES:
        path = run_dir / name
        if not path.exists():
            continue
        found = extract_thread_id(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            return found
    return ""
```

- [ ] **Step 4: Run thread recovery tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_shutdown.ShutdownTests.test_recover_thread_id_prefers_store_value tests.test_shutdown.ShutdownTests.test_recover_thread_id_scans_known_stdout_logs -v
```

Expected: PASS.

- [ ] **Step 5: Write failing tests for preview item safety**

Append to `tests/test_shutdown.py`:

```python
from agent_desk.shutdown import ProcessInfo, build_run_shutdown_item


class FakeProcessController:
    def __init__(self, infos):
        self.infos = infos

    def process_info(self, pid):
        return self.infos.get(pid)

    def process_group(self, pgid):
        return [info for info in self.infos.values() if info.pgid == pgid]


class ShutdownTests(unittest.TestCase):
    # keep existing tests in this class

    def test_build_run_shutdown_item_verifies_expected_supervisor(self):
        controller = FakeProcessController(
            {
                111: ProcessInfo(
                    pid=111,
                    ppid=1,
                    pgid=111,
                    command="python -m agent_desk run-job --config config/repos.toml --run-id 7 --kind issue",
                ),
                112: ProcessInfo(pid=112, ppid=111, pgid=111, command="codex exec --json"),
            }
        )
        run = {
            "id": 7,
            "repo_name": "octo/example",
            "issue_number": 5,
            "issue_title": "Shutdown",
            "state": "running",
            "stage": "running codex",
            "run_dir": "",
            "worktree_path": "/tmp/worktree",
            "codex_thread_id": "thread",
            "supervisor_pid": 111,
        }

        item = build_run_shutdown_item(run, controller)

        self.assertTrue(item["killable"])
        self.assertEqual(item["pgid"], 111)
        self.assertEqual([proc["pid"] for proc in item["processes"]], [111, 112])
        self.assertEqual(item["resume_available"], True)

    def test_build_run_shutdown_item_skips_unverified_pid(self):
        controller = FakeProcessController(
            {111: ProcessInfo(pid=111, ppid=1, pgid=111, command="python unrelated.py")}
        )
        run = {
            "id": 7,
            "repo_name": "octo/example",
            "issue_number": 5,
            "issue_title": "Shutdown",
            "state": "running",
            "stage": "running codex",
            "run_dir": "",
            "worktree_path": "/tmp/worktree",
            "codex_thread_id": "thread",
            "supervisor_pid": 111,
        }

        item = build_run_shutdown_item(run, controller)

        self.assertFalse(item["killable"])
        self.assertIn("not an Agent Desk run-job", item["warnings"][0])
```

If the file now has duplicate `ShutdownTests` class declarations, merge the
methods into a single class before running.

- [ ] **Step 6: Run preview item tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_shutdown.ShutdownTests.test_build_run_shutdown_item_verifies_expected_supervisor tests.test_shutdown.ShutdownTests.test_build_run_shutdown_item_skips_unverified_pid -v
```

Expected: ERROR because `build_run_shutdown_item` is missing.

- [ ] **Step 7: Implement preview item and process controller**

Append to `agent_desk/shutdown.py`:

```python
class LocalProcessController:
    def process_info(self, pid: int) -> ProcessInfo | None:
        for info in self._processes():
            if info.pid == int(pid):
                return info
        return None

    def process_group(self, pgid: int) -> list[ProcessInfo]:
        return [info for info in self._processes() if info.pgid == int(pgid)]

    def terminate_group(self, pgid: int) -> None:
        os.killpg(int(pgid), signal.SIGTERM)

    def kill_group(self, pgid: int) -> None:
        os.killpg(int(pgid), signal.SIGKILL)

    def pid_alive(self, pid: int) -> bool:
        if int(pid) <= 0:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _processes(self) -> list[ProcessInfo]:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        processes = []
        for line in completed.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            pid, ppid, pgid, command = parts
            processes.append(ProcessInfo(int(pid), int(ppid), int(pgid), command))
        return processes


def _is_expected_supervisor(info: ProcessInfo, run_id: int) -> bool:
    command = info.command
    return (
        "agent_desk" in command
        and "run-job" in command
        and "--run-id" in command
        and str(int(run_id)) in command
    )


def build_run_shutdown_item(
    run: dict[str, Any], controller: ProcessController
) -> dict[str, Any]:
    run_id = int(run["id"])
    warnings: list[str] = []
    pid_raw = run.get("supervisor_pid")
    supervisor_pid = int(pid_raw) if str(pid_raw or "").isdigit() else 0
    thread_id = recover_thread_id_from_run(run)
    worktree_path = str(run.get("worktree_path") or "")
    resume_command = format_resume_command(thread_id, worktree_path)
    resume_available = bool(thread_id and worktree_path)
    resume_unavailable_reason = ""
    if not thread_id:
        resume_unavailable_reason = "no Codex thread captured"
    elif not worktree_path:
        resume_unavailable_reason = "no worktree path recorded"
    info = controller.process_info(supervisor_pid) if supervisor_pid else None
    processes: list[ProcessInfo] = []
    pgid = 0
    killable = False
    if not supervisor_pid:
        warnings.append("no supervisor_pid recorded")
    elif info is None:
        warnings.append(f"supervisor PID {supervisor_pid} is not running")
    elif not _is_expected_supervisor(info, run_id):
        warnings.append(f"supervisor PID {supervisor_pid} is not an Agent Desk run-job for run #{run_id}")
    else:
        pgid = int(info.pgid)
        processes = controller.process_group(pgid)
        killable = True
    return {
        "run_id": run_id,
        "repo_name": str(run.get("repo_name") or ""),
        "issue_number": int(run.get("issue_number") or 0),
        "issue_title": str(run.get("issue_title") or ""),
        "state": str(run.get("state") or ""),
        "stage": str(run.get("stage") or ""),
        "run_dir": str(run.get("run_dir") or ""),
        "worktree_path": worktree_path,
        "codex_thread_id": thread_id,
        "resume_command": resume_command,
        "resume_available": resume_available,
        "resume_unavailable_reason": resume_unavailable_reason,
        "supervisor_pid": supervisor_pid,
        "pgid": pgid,
        "killable": killable,
        "processes": [asdict(process) for process in processes],
        "warnings": warnings,
    }
```

- [ ] **Step 8: Run preview item tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_shutdown.ShutdownTests.test_build_run_shutdown_item_verifies_expected_supervisor tests.test_shutdown.ShutdownTests.test_build_run_shutdown_item_skips_unverified_pid -v
```

Expected: PASS.

- [ ] **Step 9: Write failing artifact and signaling tests**

Append to `tests/test_shutdown.py`:

```python
from agent_desk.config import AgentDeskConfig
from agent_desk.shutdown import stop_verified_process_groups, write_shutdown_artifacts


class SignalController(FakeProcessController):
    def __init__(self, infos):
        super().__init__(infos)
        self.terminated = []
        self.killed = []
        self.alive = {}

    def terminate_group(self, pgid):
        self.terminated.append(pgid)

    def kill_group(self, pgid):
        self.killed.append(pgid)

    def pid_alive(self, pid):
        return self.alive.get(pid, False)


class ShutdownTests(unittest.TestCase):
    # keep existing tests in this class

    def test_write_shutdown_artifacts_writes_global_and_run_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-7"
            run_dir.mkdir(parents=True)
            config = AgentDeskConfig(data_dir=root)
            items = [
                {
                    "run_id": 7,
                    "run_dir": str(run_dir),
                    "repo_name": "octo/example",
                    "issue_number": 5,
                    "issue_title": "Shutdown",
                    "stage": "running codex",
                    "resume_command": "codex resume -C /tmp/w thread",
                    "warnings": [],
                }
            ]

            manifest = write_shutdown_artifacts(
                config=config,
                shutdown_id="2026-06-30T12-00-00Z",
                items=items,
                dashboard_pid=123,
                config_path=Path("config/repos.toml"),
            )

            self.assertTrue(Path(manifest["manifest_path"]).exists())
            self.assertTrue((run_dir / "shutdown-2026-06-30T12-00-00Z.json").exists())
            note = (run_dir / "shutdown-resume-2026-06-30T12-00-00Z.md").read_text(encoding="utf-8")
            self.assertIn("codex resume -C /tmp/w thread", note)

    def test_stop_verified_process_groups_terminates_then_kills_live_group(self):
        controller = SignalController({})
        controller.alive = {111: True}
        items = [{"run_id": 7, "pgid": 111, "supervisor_pid": 111, "killable": True}]

        results = stop_verified_process_groups(items, controller, grace_seconds=0)

        self.assertEqual(controller.terminated, [111])
        self.assertEqual(controller.killed, [111])
        self.assertEqual(results[0]["result"], "killed")

    def test_stop_verified_process_groups_skips_unverified_item(self):
        controller = SignalController({})
        items = [{"run_id": 7, "pgid": 111, "supervisor_pid": 111, "killable": False}]

        results = stop_verified_process_groups(items, controller, grace_seconds=0)

        self.assertEqual(controller.terminated, [])
        self.assertEqual(controller.killed, [])
        self.assertEqual(results[0]["result"], "skipped")
```

Merge class declarations if needed so `ShutdownTests` is declared once.

- [ ] **Step 10: Run artifact and signaling tests to verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_shutdown.ShutdownTests.test_write_shutdown_artifacts_writes_global_and_run_files \
  tests.test_shutdown.ShutdownTests.test_stop_verified_process_groups_terminates_then_kills_live_group \
  tests.test_shutdown.ShutdownTests.test_stop_verified_process_groups_skips_unverified_item \
  -v
```

Expected: ERROR because artifact and signal helpers are missing.

- [ ] **Step 11: Implement artifact and signal helpers**

Append to `agent_desk/shutdown.py`:

```python
def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_shutdown_artifacts(
    *,
    config: AgentDeskConfig,
    shutdown_id: str,
    items: list[dict[str, Any]],
    dashboard_pid: int,
    config_path: Path | None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_path = config.data_dir / "shutdowns" / f"{shutdown_id}.json"
    manifest = {
        "shutdown_id": shutdown_id,
        "dashboard_pid": int(dashboard_pid),
        "config_path": str(config_path or ""),
        "affected_runs": [item["run_id"] for item in items],
        "runs": items,
        "manifest_path": str(manifest_path),
    }
    if extra_fields:
        manifest.update(extra_fields)
    _write_json(manifest_path, manifest)
    for item in items:
        run_dir = Path(str(item.get("run_dir") or ""))
        if not run_dir:
            continue
        run_manifest_path = run_dir / f"shutdown-{shutdown_id}.json"
        run_note_path = run_dir / f"shutdown-resume-{shutdown_id}.md"
        _write_json(run_manifest_path, {"shutdown_id": shutdown_id, "run": item})
        note = "\n".join(
            [
                f"# Shutdown Resume for run #{item['run_id']}",
                "",
                f"- Repo: {item.get('repo_name', '')}",
                f"- Issue: #{item.get('issue_number', '')} {item.get('issue_title', '')}",
                f"- Stage: {item.get('stage', '')}",
                f"- Resume available: {item.get('resume_available', False)}",
                "",
                "```bash",
                str(item.get("resume_command") or ""),
                "```",
                "",
            ]
        )
        run_note_path.write_text(note, encoding="utf-8")
        item["shutdown_manifest"] = str(run_manifest_path)
        item["shutdown_resume_note"] = str(run_note_path)
    _write_json(manifest_path, manifest)
    return manifest


def stop_verified_process_groups(
    items: list[dict[str, Any]],
    controller: ProcessController,
    *,
    grace_seconds: float = 3.0,
) -> list[dict[str, Any]]:
    results = []
    for item in items:
        run_id = int(item["run_id"])
        pgid = int(item.get("pgid") or 0)
        supervisor_pid = int(item.get("supervisor_pid") or 0)
        if not item.get("killable") or not pgid:
            results.append({"run_id": run_id, "pgid": pgid, "result": "skipped"})
            continue
        controller.terminate_group(pgid)
        if grace_seconds:
            time.sleep(grace_seconds)
        if supervisor_pid and controller.pid_alive(supervisor_pid):
            controller.kill_group(pgid)
            results.append({"run_id": run_id, "pgid": pgid, "result": "killed"})
        else:
            results.append({"run_id": run_id, "pgid": pgid, "result": "terminated"})
    return results
```

- [ ] **Step 12: Run all shutdown helper tests**

Run:

```bash
python3 -m unittest tests.test_shutdown -v
```

Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add agent_desk/shutdown.py tests/test_shutdown.py
git commit -m "Add shutdown preview helpers"
```

---

### Task 3: Add Scheduler Shutdown All Flow

**Files:**
- Modify: `agent_desk/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `Scheduler.shutdown_preview(controller: ProcessController | None = None) -> dict[str, Any]`
- Produces: `Scheduler.shutdown_all(controller: ProcessController | None = None, dashboard_pid: int | None = None) -> dict[str, Any]`
- Consumes: `build_run_shutdown_item()`, `write_shutdown_artifacts()`, `stop_verified_process_groups()`.

- [ ] **Step 1: Write failing scheduler preview test**

Add to `tests/test_scheduler.py`:

```python
from agent_desk.shutdown import ProcessInfo


class FakeShutdownController:
    def __init__(self):
        self.infos = {
            111: ProcessInfo(
                pid=111,
                ppid=1,
                pgid=111,
                command="python -m agent_desk run-job --config config/repos.toml --run-id 1 --kind issue",
            )
        }

    def process_info(self, pid):
        return self.infos.get(pid)

    def process_group(self, pgid):
        return [info for info in self.infos.values() if info.pgid == pgid]

    def terminate_group(self, pgid):
        raise AssertionError("preview must not signal")

    def kill_group(self, pgid):
        raise AssertionError("preview must not signal")

    def pid_alive(self, pid):
        return True
```

Then add this method to `SchedulerTests`:

```python
def test_shutdown_preview_lists_running_runs_without_mutation(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/one",
            issue_number=5,
            issue_title="Shutdown",
            issue_url="u5",
            branch_name="b5",
        )
        store.update_run(
            run_id,
            state="running",
            stage="running codex",
            supervisor_pid=111,
            codex_thread_id="thread",
            worktree_path=str(root / "worktree"),
        )
        scheduler = Scheduler(self._config(root), store, github=FakeGitHub(), config_path=root / "repos.toml")

        preview = scheduler.shutdown_preview(controller=FakeShutdownController())

        self.assertEqual(preview["affected_runs"], [run_id])
        self.assertEqual(preview["runs"][0]["run_id"], run_id)
        self.assertTrue(preview["runs"][0]["killable"])
        self.assertEqual(store.get_run(run_id)["state"], "running")
```

- [ ] **Step 2: Run scheduler preview test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_shutdown_preview_lists_running_runs_without_mutation -v
```

Expected: ERROR because `shutdown_preview` is missing.

- [ ] **Step 3: Implement scheduler preview**

In `agent_desk/scheduler.py`, import:

```python
from .shutdown import (
    LocalProcessController,
    ProcessController,
    build_run_shutdown_item,
    shutdown_id,
    stop_verified_process_groups,
    write_shutdown_artifacts,
)
```

Add methods to `Scheduler`:

```python
def shutdown_preview(self, controller: ProcessController | None = None) -> dict[str, Any]:
    process_controller = controller or LocalProcessController()
    runs = self.store.list_runs({"running"})
    items = [build_run_shutdown_item(run, process_controller) for run in runs]
    return {
        "shutdown_id": shutdown_id(),
        "affected_runs": [item["run_id"] for item in items],
        "runs": items,
    }
```

- [ ] **Step 4: Run scheduler preview test to verify it passes**

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_shutdown_preview_lists_running_runs_without_mutation -v
```

Expected: PASS.

- [ ] **Step 5: Write failing shutdown_all test**

Add to `FakeShutdownController`:

```python
def __init__(self):
    self.infos = {
        111: ProcessInfo(
            pid=111,
            ppid=1,
            pgid=111,
            command="python -m agent_desk run-job --config config/repos.toml --run-id 1 --kind issue",
        )
    }
    self.terminated = []
    self.killed = []

def terminate_group(self, pgid):
    self.terminated.append(pgid)

def kill_group(self, pgid):
    self.killed.append(pgid)
```

Add this test:

```python
def test_shutdown_all_marks_running_runs_interrupted_and_records_artifacts(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/one",
            issue_number=5,
            issue_title="Shutdown",
            issue_url="u5",
            branch_name="b5",
        )
        run_dir = root / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        store.update_run(
            run_id,
            state="running",
            stage="running codex",
            run_dir=str(run_dir),
            supervisor_pid=111,
            codex_thread_id="thread",
            worktree_path=str(root / "worktree"),
        )
        controller = FakeShutdownController()
        scheduler = Scheduler(self._config(root), store, github=FakeGitHub(), config_path=root / "repos.toml")

        result = scheduler.shutdown_all(controller=controller, dashboard_pid=999, grace_seconds=0)
        run = store.get_run(run_id)

        self.assertEqual(run["state"], "interrupted")
        self.assertEqual(run["stage"], "interrupted by shutdown")
        self.assertEqual(run["codex_thread_id"], "thread")
        self.assertTrue(any(event["event_type"] == "shutdown-interrupted" for event in run["events"]))
        self.assertTrue(Path(result["manifest_path"]).exists())
        self.assertEqual(controller.terminated, [111])
```

- [ ] **Step 6: Run shutdown_all test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_shutdown_all_marks_running_runs_interrupted_and_records_artifacts -v
```

Expected: ERROR because `shutdown_all` is missing.

- [ ] **Step 7: Implement scheduler shutdown_all**

Add method:

```python
def shutdown_all(
    self,
    controller: ProcessController | None = None,
    *,
    dashboard_pid: int | None = None,
    grace_seconds: float = 3.0,
) -> dict[str, Any]:
    process_controller = controller or LocalProcessController()
    runs = self.store.list_runs({"running"})
    items = [build_run_shutdown_item(run, process_controller) for run in runs]
    sid = shutdown_id()
    manifest = write_shutdown_artifacts(
        config=self.config,
        shutdown_id=sid,
        items=items,
        dashboard_pid=dashboard_pid or os.getpid(),
        config_path=self.config_path,
    )
    for item in items:
        run_id = int(item["run_id"])
        fields = {
            "state": "interrupted",
            "stage": "interrupted by shutdown",
            "last_error": "Interrupted by user shutdown; resume from dashboard",
        }
        if item.get("codex_thread_id"):
            fields["codex_thread_id"] = item["codex_thread_id"]
        self.store.update_run(run_id, **fields)
        self.store.add_event(
            run_id,
            "warning",
            "shutdown-interrupted",
            "Run interrupted by dashboard shutdown",
            {
                "shutdown_id": manifest["shutdown_id"],
                "manifest_path": manifest["manifest_path"],
                "supervisor_pid": item.get("supervisor_pid"),
                "pgid": item.get("pgid"),
                "resume_available": item.get("resume_available"),
                "warnings": item.get("warnings", []),
            },
        )
    signal_results = stop_verified_process_groups(items, process_controller, grace_seconds=grace_seconds)
    return write_shutdown_artifacts(
        config=self.config,
        shutdown_id=sid,
        items=items,
        dashboard_pid=dashboard_pid or os.getpid(),
        config_path=self.config_path,
        extra_fields={"signal_results": signal_results},
    )
```

- [ ] **Step 8: Run scheduler shutdown tests**

Run:

```bash
python3 -m unittest \
  tests.test_scheduler.SchedulerTests.test_shutdown_preview_lists_running_runs_without_mutation \
  tests.test_scheduler.SchedulerTests.test_shutdown_all_marks_running_runs_interrupted_and_records_artifacts \
  -v
```

Expected: PASS.

- [ ] **Step 9: Update orphan reconciliation test for interrupted runs**

Add this test:

```python
def test_reconcile_orphans_ignores_interrupted_runs(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        scheduler = Scheduler(self._config(root), store, github=FakeGitHub())
        run_id = store.create_run(
            repo_name="octo/one",
            issue_number=9,
            issue_title="Interrupted",
            issue_url="u9",
            branch_name="b9",
        )
        store.update_run(run_id, state="interrupted", stage="interrupted by shutdown", supervisor_pid=222)
        scheduler._pid_alive = staticmethod(lambda pid: False)

        failed = scheduler.reconcile_orphans()

        self.assertEqual(failed, [])
        self.assertEqual(store.get_run(run_id)["state"], "interrupted")
```

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_reconcile_orphans_ignores_interrupted_runs -v
```

Expected: PASS because reconciliation only lists `running` runs.

- [ ] **Step 10: Commit**

```bash
git add agent_desk/scheduler.py tests/test_scheduler.py agent_desk/shutdown.py tests/test_shutdown.py
git commit -m "Add shutdown all scheduler flow"
```

---

### Task 4: Add Resume Interrupted Continuation Flow

**Files:**
- Modify: `agent_desk/continuation.py`
- Modify: `agent_desk/scheduler.py`
- Test: `tests/test_continuation.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `ContinuationRunner.resume_interrupted(run_id: int) -> ContinuationResult`
- Produces: `render_resume_interrupted_prompt(run: dict[str, Any]) -> str`
- Produces: `Scheduler.resume_interrupted(run_id: int) -> RunNextResult`
- Produces: detached job kind `resume-interrupted`.

- [ ] **Step 1: Write failing continuation prompt test**

Add to `tests/test_continuation.py`:

```python
from agent_desk.continuation import render_resume_interrupted_prompt


def test_render_resume_interrupted_prompt_points_to_shutdown_context(self):
    run = {
        "id": 7,
        "repo_name": "octo/example",
        "issue_number": 5,
        "issue_title": "Shutdown",
        "run_dir": "/tmp/run-7",
        "worktree_path": "/tmp/worktree",
        "stage": "interrupted by shutdown",
    }

    prompt = render_resume_interrupted_prompt(run)

    self.assertIn("controlled Agent Desk shutdown", prompt)
    self.assertIn("/tmp/run-7", prompt)
    self.assertIn("/tmp/worktree", prompt)
    self.assertIn("worker-result.schema.json", prompt)
```

- [ ] **Step 2: Run prompt test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_continuation.test_render_resume_interrupted_prompt_points_to_shutdown_context -v
```

Expected: ERROR because `render_resume_interrupted_prompt` is missing.

- [ ] **Step 3: Implement resume interrupted prompt**

In `agent_desk/continuation.py`, add:

```python
def render_resume_interrupted_prompt(run: dict[str, Any]) -> str:
    return f"""You are resuming an Agent Desk worker run after a controlled Agent Desk shutdown.

Original run:
- run id: {run['id']}
- repo: {run['repo_name']}
- issue: #{run['issue_number']} {run.get('issue_title') or ''}
- interrupted stage: {run.get('stage') or ''}
- run directory: {run.get('run_dir') or ''}
- worktree: {run.get('worktree_path') or ''}

Before changing code, inspect the worktree and the run directory logs, including
the original prompt and any shutdown-resume markdown file. Continue the original
issue from the current worktree state. Do not restart from scratch unless the
worktree state makes continuation impossible.

Return a final response that satisfies schemas/worker-result.schema.json: include
status, summary, tests, questions, risks, pr_url, and decision_log.
"""
```

- [ ] **Step 4: Run prompt test to verify it passes**

Run:

```bash
python3 -m unittest tests.test_continuation.test_render_resume_interrupted_prompt_points_to_shutdown_context -v
```

Expected: PASS.

- [ ] **Step 5: Write failing continuation execution test**

Add a test beside existing continuation runner tests:

```python
def test_resume_interrupted_resumes_thread_and_opens_pr_state(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_dir = root / "run"
        run_dir.mkdir()
        worktree = root / "worktree"
        worktree.mkdir()
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=5,
            issue_title="Shutdown",
            issue_url="u5",
            branch_name="b5",
        )
        store.update_run(
            run_id,
            state="interrupted",
            stage="interrupted by shutdown",
            run_dir=str(run_dir),
            worktree_path=str(worktree),
            codex_thread_id="thread",
        )
        runner = RecordingRunner(
            stdout='{"status":"done","summary":"resumed","pr_url":"https://example.test/pr/1"}',
            result={"status": "done", "summary": "resumed", "pr_url": "https://example.test/pr/1"},
        )
        config = AgentDeskConfig(data_dir=root, repos=[RepoConfig(name="octo/example", local_path=root)])

        result = ContinuationRunner(config, store, runner=runner).resume_interrupted(run_id)
        run = store.get_run(run_id)

    self.assertTrue(result.ok)
    self.assertEqual(run["state"], "pr_open")
    self.assertEqual(run["pr_url"], "https://example.test/pr/1")
    self.assertIn("resume-interrupted", runner.calls[0].argv)
```

Adjust `RecordingRunner` construction to match the helper class already present
in `tests/test_continuation.py`; the key assertions are that state becomes
`pr_open` and the argv includes `exec resume`.

- [ ] **Step 6: Run continuation execution test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_continuation.ContinuationTests.test_resume_interrupted_resumes_thread_and_opens_pr_state -v
```

Expected: ERROR because `resume_interrupted` is missing.

- [ ] **Step 7: Implement ContinuationRunner.resume_interrupted**

Add method:

```python
def resume_interrupted(self, run_id: int) -> ContinuationResult:
    run = self.store.get_run(run_id)
    repo = self._repo_for_run(run)
    prompt = render_resume_interrupted_prompt(run)
    result = self._resume(
        run_id,
        "resume-interrupted",
        prompt,
        success_state="pr_open",
        success_stage="resumed after shutdown",
        sandbox=repo.closeout_sandbox,
    )
    refreshed = self.store.get_run(run_id)
    if result.ok and not refreshed.get("pr_url") and repo.push_pr:
        return self.open_pull_request(run_id)
    return result
```

Then adjust `_resume()` so when `success_state == "pr_open"` and status is
`done` without a PR URL, it does not incorrectly set `state="pr_open"` with an
empty URL. Replace the success block with:

```python
if status == "done":
    pr_url = str(payload.get("pr_url") or run.get("pr_url") or "")
    if success_state == "pr_open" and not pr_url:
        self.store.update_run(run_id, state="running", stage=f"{action} done; opening pull request", last_error="")
        self.store.add_event(run_id, "info", action, summary, payload)
        return ContinuationResult(True, summary, run_id)
    if require_pr_url and not pr_url:
        message = f"{action} returned done without pr_url"
        self.store.update_run(run_id, state="blocked", stage="blocked", last_error=message)
        self.store.add_event(run_id, "warning", action, message, payload)
        return ContinuationResult(False, message, run_id)
    if require_pr_url and not self.github.pull_request_exists(str(run["repo_name"]), pr_url):
        message = f"{action} returned non-existent pr_url: {pr_url}"
        self.store.update_run(run_id, state="blocked", stage="blocked", pr_url="", last_error=message)
        self.store.add_event(run_id, "warning", action, message, payload)
        return ContinuationResult(False, message, run_id)
    self.store.update_run(run_id, state=success_state, stage=success_stage, pr_url=pr_url, last_error="")
    self.store.add_event(run_id, "info", action, summary, payload)
    return ContinuationResult(True, summary, run_id)
```

Keep the non-`done` branch after this success block unchanged.

- [ ] **Step 8: Run continuation tests**

Run:

```bash
python3 -m unittest tests.test_continuation -v
```

Expected: PASS.

- [ ] **Step 9: Write failing scheduler resume dispatch test**

Add to `tests/test_scheduler.py`:

```python
def test_resume_interrupted_dispatches_detached_job(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/one",
            issue_number=5,
            issue_title="Shutdown",
            issue_url="u5",
            branch_name="b5",
        )
        worktree = root / "worktree"
        worktree.mkdir()
        store.update_run(
            run_id,
            state="interrupted",
            stage="interrupted by shutdown",
            codex_thread_id="thread",
            worktree_path=str(worktree),
        )
        scheduler = SpawnRecordingScheduler(
            self._config(root),
            store,
            github=FakeGitHub(),
            config_path=root / "repos.toml",
            detach_jobs=True,
        )

        result = scheduler.resume_interrupted(run_id)

        self.assertTrue(result.started)
        self.assertEqual(store.get_run(run_id)["state"], "running")
        self.assertEqual(store.get_run(run_id)["stage"], "resume-interrupted queued")
        self.assertEqual(scheduler.spawned[-1]["kind"], "resume-interrupted")
```

Use or extend the existing detached-job recording test helper so the test can
inspect the kind passed into `_spawn_detached_job()`.

- [ ] **Step 10: Run scheduler dispatch test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_resume_interrupted_dispatches_detached_job -v
```

Expected: ERROR because `resume_interrupted` and job kind are missing.

- [ ] **Step 11: Implement scheduler resume dispatch and run-job kind**

In `agent_desk/scheduler.py`:

```python
JOB_KIND_BY_TARGET["_run_resume_interrupted"] = "resume-interrupted"
```

Add:

```python
def resume_interrupted(self, run_id: int) -> RunNextResult:
    with self._lock:
        if self._paused:
            return RunNextResult(False, "Scheduler is paused", run_id)
        run = self.store.get_run(run_id)
        if run["state"] != "interrupted":
            return RunNextResult(False, f"Run #{run_id} is not interrupted", run_id)
        if not str(run.get("codex_thread_id") or ""):
            return RunNextResult(False, "resume requires codex_thread_id", run_id)
        if not str(run.get("worktree_path") or ""):
            return RunNextResult(False, "resume requires worktree_path", run_id)
        self.store.update_run(run_id, state="running", stage="resume-interrupted queued", last_error="")
        self.store.add_event(run_id, "info", "resume-interrupted", "Starting interrupted run resume", {})
        self._start_daemon_thread(self._run_resume_interrupted, {"run_id": run_id})
        return RunNextResult(True, "Resume interrupted started", run_id)

def _run_resume_interrupted(self, run_id: int) -> None:
    try:
        result = self.continuation_factory(self.config, self.store).resume_interrupted(run_id)
        if result.ok:
            self._start_ci_fix_if_closeout_blocked_by_failed_checks(run_id, result)
    except Exception as exc:
        self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
        self.store.add_event(run_id, "error", "resume-interrupted", "Interrupted run resume failed", {"detail": str(exc)})
```

In `run_job()` add:

```python
elif kind == "resume-interrupted":
    self._run_resume_interrupted(run_id=run_id)
```

- [ ] **Step 12: Run scheduler resume tests**

Run:

```bash
python3 -m unittest \
  tests.test_scheduler.SchedulerTests.test_resume_interrupted_dispatches_detached_job \
  tests.test_scheduler.DetachedJobTests.test_run_job_rejects_unknown_kind \
  -v
```

Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add agent_desk/continuation.py agent_desk/scheduler.py tests/test_continuation.py tests/test_scheduler.py
git commit -m "Add interrupted run resume flow"
```

---

### Task 5: Add Dashboard API And UI

**Files:**
- Modify: `agent_desk/dashboard.py`
- Modify: `agent_desk/static/dashboard.html`
- Modify: `agent_desk/static/dashboard.js`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `Scheduler.shutdown_preview()`, `Scheduler.shutdown_all()`, `Scheduler.resume_interrupted()`.
- Produces: `GET /api/actions/shutdown-preview`
- Produces: `POST /api/actions/shutdown-all`
- Produces: `POST /api/run/<id>/resume-interrupted`

- [ ] **Step 1: Write failing dashboard route tests**

Add a fake scheduler to `tests/test_dashboard.py`:

```python
class _ShutdownScheduler:
    paused = False

    def __init__(self):
        self.shutdown_called = False
        self.resume_calls = []

    def shutdown_preview(self):
        return {
            "shutdown_id": "2026-06-30T12-00-00Z",
            "affected_runs": [7],
            "runs": [{"run_id": 7, "issue_number": 5, "stage": "running codex"}],
        }

    def shutdown_all(self):
        self.shutdown_called = True
        return {
            "shutdown_id": "2026-06-30T12-00-00Z",
            "manifest_path": "/tmp/shutdown.json",
            "affected_runs": [7],
            "runs": [{"run_id": 7, "issue_number": 5, "stage": "running codex"}],
        }

    def resume_interrupted(self, run_id):
        self.resume_calls.append(run_id)
        return RunNextResult(True, "Resume interrupted started", run_id)
```

Add tests:

```python
def _serve_for_test(self, host, store, scheduler):
    bound: dict[str, int] = {}
    ready = threading.Event()
    thread = threading.Thread(
        target=serve_dashboard,
        kwargs={
            "host": host,
            "port": 0,
            "store": store,
            "scheduler": scheduler,
            "on_serving": lambda _h, port: (bound.update(port=port), ready.set()),
        },
        daemon=True,
    )
    thread.start()
    self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")
    return bound["port"]

def test_shutdown_preview_route_returns_scheduler_preview(self):
    host = "127.0.0.1"
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "desk.sqlite")
        scheduler = _ShutdownScheduler()
        port = self._serve_for_test(host, store, scheduler)

        with urllib.request.urlopen(f"http://{host}:{port}/api/actions/shutdown-preview", timeout=5) as response:
            payload = json.loads(response.read())

    self.assertEqual(response.status, 200)
    self.assertEqual(payload["affected_runs"], [7])

def test_resume_interrupted_route_dispatches_scheduler(self):
    host = "127.0.0.1"
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "desk.sqlite")
        scheduler = _ShutdownScheduler()
        port = self._serve_for_test(host, store, scheduler)
        request = urllib.request.Request(
            f"http://{host}:{port}/api/run/7/resume-interrupted",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())

    self.assertTrue(payload["started"])
    self.assertEqual(scheduler.resume_calls, [7])
```

Add `_serve_for_test()` as a method on `DashboardTests` before these route
tests, and use it only for tests that need an actual HTTP server.

- [ ] **Step 2: Run route tests to verify they fail**

Run with local binding permission:

```bash
python3 -m unittest \
  tests.test_dashboard.DashboardTests.test_shutdown_preview_route_returns_scheduler_preview \
  tests.test_dashboard.DashboardTests.test_resume_interrupted_route_dispatches_scheduler \
  -v
```

Expected: FAIL or ERROR because routes are missing.

- [ ] **Step 3: Implement dashboard routes**

In `make_handler().do_GET()` add before `NOT_FOUND`:

```python
if path == "/api/actions/shutdown-preview":
    if not scheduler:
        self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "scheduler disabled")
        return
    self._send_json(scheduler.shutdown_preview())
    return
```

In `do_POST()` add:

```python
if path == "/api/actions/shutdown-all":
    result = scheduler.shutdown_all()
    self._send_json(result)
    if shutdown_callback is not None:
        threading.Thread(target=shutdown_callback, daemon=False).start()
    return
```

Add a new `shutdown_callback` parameter to `make_handler()` instead of
overloading `restart_callback`:

```python
shutdown_callback: Callable[[], None] | None = None
```

Add a service shutdown helper next to `restart_process()`:

```python
def shutdown_process(scheduler: Scheduler | None, server: ThreadingHTTPServer) -> None:
    if scheduler is not None:
        scheduler.stop()
    server.shutdown()
```

In `serve_dashboard()`, create the default callback before constructing the
handler:

```python
actual_shutdown_callback = shutdown_callback or (
    lambda: shutdown_process(scheduler, server)
)
handler = make_handler(
    store,
    scheduler,
    config_path,
    actual_restart_callback,
    actual_shutdown_callback,
)
```

Route interrupted resume:

```python
if path.startswith("/api/run/") and path.endswith("/resume-interrupted"):
    run_id = self._run_id_from_path(path)
    self._send_json(scheduler.resume_interrupted(run_id).__dict__)
    return
```

If `_run_id_from_path()` does not exist, implement:

```python
def _run_id_from_path(self, path: str) -> int:
    try:
        return int(path.split("/")[3])
    except (IndexError, ValueError):
        raise ValueError("run id must be a number")
```

Handle `ValueError` with `HTTPStatus.BAD_REQUEST`.

- [ ] **Step 4: Run route tests to verify they pass**

Run:

```bash
python3 -m unittest \
  tests.test_dashboard.DashboardTests.test_shutdown_preview_route_returns_scheduler_preview \
  tests.test_dashboard.DashboardTests.test_resume_interrupted_route_dispatches_scheduler \
  -v
```

Expected: PASS.

- [ ] **Step 5: Write failing HTML/JS tests**

Add to `tests/test_dashboard.py`:

```python
def test_dashboard_html_renders_shutdown_all_control(self):
    self.assertIn("Shutdown all", HTML)
    self.assertIn("shutdownAll()", HTML)
    self.assertIn("/api/actions/shutdown-preview", HTML)
    self.assertIn("/api/actions/shutdown-all", HTML)

def test_dashboard_html_renders_interrupted_resume_controls(self):
    self.assertIn("resumeInterrupted(", HTML)
    self.assertIn("/resume-interrupted", HTML)
    self.assertIn("resume_available", HTML)
    self.assertIn("resume_unavailable_reason", HTML)
```

- [ ] **Step 6: Run HTML/JS tests to verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_dashboard.DashboardTests.test_dashboard_html_renders_shutdown_all_control \
  tests.test_dashboard.DashboardTests.test_dashboard_html_renders_interrupted_resume_controls \
  -v
```

Expected: FAIL because UI strings are absent.

- [ ] **Step 7: Implement header shutdown button**

In `agent_desk/static/dashboard.html`, add:

```html
<button onclick="shutdownAll()">Shutdown all</button>
```

Keep `Restart` as a separate button.

- [ ] **Step 8: Implement shutdownAll and resumeInterrupted JS**

In `agent_desk/static/dashboard.js`, add:

```javascript
async function shutdownAll() {
  const previewRes = await fetch('/api/actions/shutdown-preview');
  if (!previewRes.ok) {
    alert(await previewRes.text());
    return;
  }
  const preview = await previewRes.json();
  const runs = preview.runs || [];
  const lines = runs.map(run => `#${run.issue_number} ${run.stage} pid=${run.supervisor_pid || 'none'}`);
  const message = `Shutdown Agent Desk and stop ${runs.length} running job(s)?\n\n${lines.join('\n')}\n\nThis records interrupted runs for dashboard Resume.`;
  if (!confirm(message)) return;
  const shutdownRes = await fetch('/api/actions/shutdown-all', { method: 'POST' });
  if (!shutdownRes.ok) {
    alert(await shutdownRes.text());
    return;
  }
  const result = await shutdownRes.json();
  const health = document.getElementById('health');
  if (health) health.textContent = `Shutdown recorded: ${result.shutdown_id}`;
}

async function resumeInterrupted(runId) {
  await postJson(`/api/run/${runId}/resume-interrupted`, {});
}
```

In `runActions(run)`, before the `pr_open` branch, add:

```javascript
if (run.state === 'interrupted') {
  if (run.resume_available) {
    return `<div class="run-actions">
      <button class="primary" onclick="resumeInterrupted(${run.id})">Resume</button>
    </div>`;
  }
  return `<div class="muted">${esc(run.resume_unavailable_reason || 'Resume unavailable')}</div>`;
}
```

- [ ] **Step 9: Run HTML/JS tests to verify they pass**

Run:

```bash
python3 -m unittest \
  tests.test_dashboard.DashboardTests.test_dashboard_html_renders_shutdown_all_control \
  tests.test_dashboard.DashboardTests.test_dashboard_html_renders_interrupted_resume_controls \
  -v
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add agent_desk/dashboard.py agent_desk/static/dashboard.html agent_desk/static/dashboard.js tests/test_dashboard.py
git commit -m "Add shutdown all dashboard controls"
```

---

### Task 6: Final Integration And Regression Pass

**Files:**
- Modify: `agent_desk/store.py` only if state migration or terminal-state handling regresses.
- Modify: `agent_desk/shutdown.py` only if process preview, manifest, or signal tests fail.
- Modify: `agent_desk/scheduler.py` only if shutdown/resume dispatch tests fail.
- Modify: `agent_desk/continuation.py` only if resume worker-result handling tests fail.
- Modify: `agent_desk/dashboard.py` only if HTTP route tests fail.
- Modify: `agent_desk/static/dashboard.html` only if header control tests fail.
- Modify: `agent_desk/static/dashboard.js` only if UI string or run-card tests fail.
- Test: full suite.

**Interfaces:**
- Consumes all prior tasks.
- Produces a fully wired shutdown/resume feature.

- [ ] **Step 1: Run targeted shutdown and resume tests**

Run:

```bash
python3 -m unittest \
  tests.test_shutdown \
  tests.test_scheduler.SchedulerTests.test_shutdown_preview_lists_running_runs_without_mutation \
  tests.test_scheduler.SchedulerTests.test_shutdown_all_marks_running_runs_interrupted_and_records_artifacts \
  tests.test_scheduler.SchedulerTests.test_resume_interrupted_dispatches_detached_job \
  tests.test_continuation.ContinuationTests.test_resume_interrupted_resumes_thread_and_opens_pr_state \
  tests.test_dashboard.DashboardTests.test_shutdown_preview_route_returns_scheduler_preview \
  tests.test_dashboard.DashboardTests.test_resume_interrupted_route_dispatches_scheduler \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: PASS. In the Codex sandbox this command may need escalation because
dashboard tests bind to `127.0.0.1`.

- [ ] **Step 3: Smoke test safe non-killing pieces**

Start dashboard with a temporary config that has no running jobs, then request
preview:

```bash
python3 -m agent_desk serve --config config/repos.toml --no-scheduler --port 9876
```

In a second terminal:

```bash
curl -fsSL http://127.0.0.1:9876/api/state
```

Expected: HTTP 200. Do not run real `shutdown-all` against active user work in
the smoke test.

- [ ] **Step 4: Inspect git diff**

Run:

```bash
git status --short
git diff --check
```

Expected: only intended files changed; `git diff --check` exits 0.

- [ ] **Step 5: Commit integration fixes only when Step 4 shows changes**

After Step 4, if `git status --short` shows only `agent_desk/shutdown.py` and
`tests/test_shutdown.py` changed, stage those files:

```bash
git add agent_desk/shutdown.py tests/test_shutdown.py
git commit -m "Wire shutdown all integration"
```

If Step 4 shows no changes, do not create an integration commit. If Step 4 shows
different files, write the exact filenames from `git status --short` into a
fresh `git add file1 file2` command and do not use `git add .`.
