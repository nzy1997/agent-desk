# Repository Setup Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent detached Agent Desk workers sharing one checkout from failing when concurrent Git setup operations race on remote-tracking references.

**Architecture:** Add a dependency-free `fcntl.flock` context manager keyed by the canonical repository path, then hold it around each worker's fetch and worktree creation. Add a narrow three-attempt retry for the observed reference-lock error while preserving immediate failure for unrelated Git errors.

**Tech Stack:** Python 3.11+, standard-library `contextlib`, `fcntl`, `hashlib`, `multiprocessing`, and `unittest`.

## Global Constraints

- Keep runtime dependencies empty.
- Serialize only repository preparation; do not serialize Codex execution.
- Retry only reference-lock races, at most three total fetch attempts.
- Preserve existing terminal behavior for unrelated fetch and worktree errors.
- Use stdlib `unittest`, not pytest.

---

### Task 1: Cross-process repository setup lock

**Files:**
- Create: `agent_desk/repository_setup.py`
- Create: `tests/test_repository_setup.py`

**Interfaces:**
- Consumes: `AgentDeskConfig.data_dir` and `RepoConfig.local_path` as `Path` values.
- Produces: `repository_setup_lock(data_dir: Path, repo_path: Path) -> ContextManager[Path]` and `repository_setup_lock_path(data_dir: Path, repo_path: Path) -> Path`.

- [x] **Step 1: Write the failing cross-process lock test**

```python
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from agent_desk.repository_setup import fcntl, repository_setup_lock


def hold_repository_lock(data_dir, repo_path, acquired, release):
    with repository_setup_lock(Path(data_dir), Path(repo_path)):
        acquired.set()
        release.wait(timeout=5)


class RepositorySetupLockTests(unittest.TestCase):
    @unittest.skipIf(fcntl is None, "fcntl is unavailable")
    def test_same_repository_is_serialized_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            context = multiprocessing.get_context("spawn")
            first_acquired = context.Event()
            first_release = context.Event()
            second_acquired = context.Event()
            second_release = context.Event()
            first = context.Process(
                target=hold_repository_lock,
                args=(root / "data", repo, first_acquired, first_release),
            )
            second = context.Process(
                target=hold_repository_lock,
                args=(root / "data", repo, second_acquired, second_release),
            )
            try:
                first.start()
                self.assertTrue(first_acquired.wait(timeout=3))
                second.start()
                self.assertFalse(second_acquired.wait(timeout=0.2))
                first_release.set()
                self.assertTrue(second_acquired.wait(timeout=3))
            finally:
                first_release.set()
                second_release.set()
                first.join(timeout=3)
                second.join(timeout=3)
                if first.is_alive():
                    first.terminate()
                if second.is_alive():
                    second.terminate()
```

- [x] **Step 2: Run the new test and verify RED**

Run: `python3 -m unittest tests.test_repository_setup -v`

Expected: import failure because `agent_desk.repository_setup` does not exist.

- [x] **Step 3: Implement the lock helper**

```python
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def repository_setup_lock_path(data_dir: Path, repo_path: Path) -> Path:
    canonical = str(Path(repo_path).resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Path(data_dir) / "locks" / "repository-setup" / f"{digest}.lock"


@contextmanager
def repository_setup_lock(data_dir: Path, repo_path: Path) -> Iterator[Path]:
    lock_path = repository_setup_lock_path(data_dir, repo_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield lock_path
    finally:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
```

- [x] **Step 4: Run the lock tests and verify GREEN**

Run: `python3 -m unittest tests.test_repository_setup -v`

Expected: the second process remains blocked until the first releases the lock; all tests pass.

- [x] **Step 5: Commit the lock helper**

```bash
git add agent_desk/repository_setup.py tests/test_repository_setup.py
git commit -m "feat: serialize repository setup across workers"
```

### Task 2: Targeted fetch retry inside the setup lock

**Files:**
- Modify: `agent_desk/worker.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Consumes: `repository_setup_lock`, `CommandRunner.run()`, and Git stderr.
- Produces: `is_retryable_ref_lock_error(stderr: str) -> bool` and `Worker._fetch_base(repo, run_id, run_dir) -> CommandResult`.

- [x] **Step 1: Write failing worker retry tests**

```python
def test_worker_retries_reference_lock_fetch_failure(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_path = root / "repo"
        repo_path.mkdir()
        config = AgentDeskConfig(data_dir=root / "data")
        repo = RepoConfig(name="octo/example", local_path=repo_path, push_pr=False)
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name=repo.name,
            issue_number=20,
            issue_title="Retry fetch",
            issue_url="https://github.com/octo/example/issues/20",
            branch_name="agent/issue-20-retry-fetch",
        )
        runner = FakeCommandRunner(
            [
                CommandResult(
                    ["git", "fetch"],
                    1,
                    "",
                    "error: cannot lock ref 'refs/remotes/origin/main': "
                    "is at new but expected old\n"
                    "(unable to update local ref)",
                ),
                CommandResult(["git", "fetch"], 0, "", ""),
                CommandResult(["git", "worktree"], 0, "", ""),
                CommandResult(
                    ["codex", "exec"],
                    0,
                    '{"status":"done","summary":"ok","tests":[],"questions":[]}',
                    "",
                ),
            ]
        )

        with patch("agent_desk.worker.time.sleep") as sleep:
            result = Worker(config, store, runner).run_issue(
                run_id=run_id,
                repo=repo,
                issue_number=20,
                issue_title="Retry fetch",
                issue_body="Body",
                issue_url="https://github.com/octo/example/issues/20",
                branch_name="agent/issue-20-retry-fetch",
            )

        fetch_calls = [call for call in runner.calls if "fetch" in call.argv]
        retry_events = [
            event
            for event in store.dashboard_state()["events"]
            if event["run_id"] == run_id and event["event_type"] == "git-fetch-retry"
        ]
        self.assertEqual(result.status, "done")
        self.assertEqual(store.get_run(run_id)["state"], "done")
        self.assertEqual(len(fetch_calls), 2)
        self.assertEqual(len(retry_events), 1)
        sleep.assert_called_once_with(0.1)

def test_worker_does_not_retry_unrelated_fetch_failure(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_path = root / "repo"
        repo_path.mkdir()
        config = AgentDeskConfig(data_dir=root / "data")
        repo = RepoConfig(name="octo/example", local_path=repo_path, push_pr=False)
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name=repo.name,
            issue_number=21,
            issue_title="Fail fetch",
            issue_url="https://github.com/octo/example/issues/21",
            branch_name="agent/issue-21-fail-fetch",
        )
        runner = FakeCommandRunner(
            [CommandResult(["git", "fetch"], 128, "", "fatal: Authentication failed")]
        )

        result = Worker(config, store, runner).run_issue(
            run_id=run_id,
            repo=repo,
            issue_number=21,
            issue_title="Fail fetch",
            issue_body="Body",
            issue_url="https://github.com/octo/example/issues/21",
            branch_name="agent/issue-21-fail-fetch",
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(store.get_run(run_id)["last_error"], "git fetch failed")
        self.assertEqual(len(runner.calls), 1)
```

- [x] **Step 2: Run the worker tests and verify RED**

Run: `python3 -m unittest tests.test_worker.WorkerTests.test_worker_retries_reference_lock_fetch_failure tests.test_worker.WorkerTests.test_worker_does_not_retry_unrelated_fetch_failure -v`

Expected: the first test fails because the worker immediately marks the run failed instead of consuming the successful retry result.

- [x] **Step 3: Add the retry classifier and fetch helper**

```python
GIT_FETCH_MAX_ATTEMPTS = 3
GIT_FETCH_RETRY_DELAYS = (0.1, 0.25)


def is_retryable_ref_lock_error(stderr: str) -> bool:
    detail = str(stderr or "").lower()
    return "cannot lock ref" in detail and (
        "but expected" in detail or "unable to update local ref" in detail
    )
```

Add this method to `Worker`:

```python
def _fetch_base(
    self, repo: RepoConfig, run_id: int, run_dir: Path
) -> CommandResult:
    for attempt in range(1, GIT_FETCH_MAX_ATTEMPTS + 1):
        fetch = self.runner.run(
            ["git", "-C", str(repo.local_path), "fetch", "origin", repo.base_branch],
            stdout_path=run_dir / "git-fetch.stdout.log",
            stderr_path=run_dir / "git-fetch.stderr.log",
        )
        if (
            fetch.returncode == 0
            or not is_retryable_ref_lock_error(fetch.stderr)
            or attempt == GIT_FETCH_MAX_ATTEMPTS
        ):
            return fetch
        delay = GIT_FETCH_RETRY_DELAYS[attempt - 1]
        self.store.add_event(
            run_id,
            "warning",
            "git-fetch-retry",
            "Retrying git fetch after reference lock conflict",
            {
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_attempts": GIT_FETCH_MAX_ATTEMPTS,
                "delay_seconds": delay,
                "detail": fetch.stderr[-4000:],
            },
        )
        time.sleep(delay)
    raise AssertionError("unreachable")
```

- [x] **Step 4: Hold the repository lock around fetch and worktree creation**

Replace the current fetch/worktree block in `Worker.run_issue()` with:

```python
self.store.add_event(
    run_id,
    "info",
    "repository-setup-lock",
    "Waiting for repository setup lock",
    {"path": str(repo.local_path)},
)
with repository_setup_lock(self.config.data_dir, repo.local_path):
    self.store.add_event(
        run_id,
        "info",
        "repository-setup-lock",
        "Acquired repository setup lock",
        {"path": str(repo.local_path)},
    )
    self.store.add_event(run_id, "info", "worktree", "Fetching base branch", {})
    fetch = self._fetch_base(repo, run_id, run_dir)
    if fetch.returncode != 0:
        return self._fail(
            run_id, run_dir, "failed", "git fetch failed", fetch.stderr
        )

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    self.store.add_event(
        run_id,
        "info",
        "worktree",
        "Creating worktree",
        {"path": str(worktree_path)},
    )
    add = self.runner.run(
        [
            "git",
            "-C",
            str(repo.local_path),
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            f"origin/{repo.base_branch}",
        ],
        stdout_path=run_dir / "git-worktree.stdout.log",
        stderr_path=run_dir / "git-worktree.stderr.log",
    )
    if add.returncode != 0:
        return self._fail(
            run_id, run_dir, "failed", "git worktree add failed", add.stderr
        )
```

Release the context before changing the stage to `running codex`.

- [x] **Step 5: Run focused tests and verify GREEN**

Run: `python3 -m unittest tests.test_repository_setup tests.test_worker -v`

Expected: all repository setup and worker tests pass.

- [x] **Step 6: Run repository verification**

Run: `make test`

Expected: all stdlib tests pass with zero failures.

Run: `ruff check . && ruff format --check .`

Expected: both commands exit zero without modifying files.

Observed baseline note: `make test` passed all 231 tests. Full-repository Ruff
is not baseline-clean because `tests/test_dashboard.py:494` already contains an
unused assignment and 27 pre-existing files do not match the currently resolved
Ruff formatter. Scoped `ruff check` passed for all four Python files involved in
this change, and `ruff format --check` passed for both newly created files.

- [x] **Step 7: Commit the worker integration**

```bash
git add agent_desk/worker.py tests/test_worker.py docs/superpowers/plans/2026-07-10-repository-setup-lock.md
git commit -m "fix: prevent concurrent repository setup races"
```
