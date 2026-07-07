from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import threading
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


# Run states that mean "no further automated work" (used by find_open_run).
TERMINAL_STATES = {"done", "failed", "blocked", "pr_open", "needs_review", "interrupted"}

# Valid run states (folders). "available" is a synced issue that is not yet a run.
RUN_STATES = (
    "queued",
    "ready",
    "waiting_dependencies",
    "running",
    "pr_open",
    "needs_review",
    "blocked",
    "interrupted",
    "done",
    "failed",
)
ALL_STATES = ("available",) + RUN_STATES

# find_open_run looks for an active run, so it ignores synced-but-not-queued
# issues and finished work.
_OPEN_EXCLUDE = {"available", "done", "failed", "blocked", "waiting_dependencies"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """Filesystem-backed run store.

    Each run/issue is a single JSON file. Its ``state`` is the folder it lives
    in, under ``<base>/state/<owner>__<repo>/<state>/<id>.json``. A state
    transition rewrites the file into the new folder and removes the old copy.
    All mutations take a process-wide lock and use atomic writes.

    The constructor accepts the legacy SQLite path for compatibility; if the
    path has a suffix (e.g. ``agent-desk.sqlite``) its parent directory is used
    as the base, otherwise the path itself is the base directory.
    """

    def __init__(self, path: Path):
        p = Path(path)
        self.base = p.parent if p.suffix else p
        self.state_dir = self.base / "state"
        self.counters_path = self.base / "counters.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _slug(repo_name: str) -> str:
        return repo_name.replace("/", "__")

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    @contextlib.contextmanager
    def _file_lock(self):
        """Cross-process advisory lock around the shared counters file.

        Detached supervisor processes share ``counters.json`` for the global
        ``id``/``event`` sequence; ``self._lock`` only serializes within one
        process. ``flock`` serializes the read-modify-write across processes too.
        Falls back to a no-op where ``fcntl`` is unavailable (non-POSIX).
        """
        if fcntl is None:
            yield
            return
        self.base.mkdir(parents=True, exist_ok=True)
        handle = open(self.base / "counters.lock", "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def _next(self, name: str) -> int:
        with self._lock, self._file_lock():
            data: dict[str, int] = {}
            if self.counters_path.exists():
                data = json.loads(self.counters_path.read_text(encoding="utf-8") or "{}")
            value = int(data.get(name, 0)) + 1
            data[name] = value
            self._atomic_write(self.counters_path, json.dumps(data))
            return value

    def _path_for(self, repo_name: str, state: str, run_id: int) -> Path:
        return self.state_dir / self._slug(repo_name) / state / f"{run_id}.json"

    def _find_path(self, run_id: int) -> Path | None:
        for path in self.state_dir.glob(f"*/*/{run_id}.json"):
            return path
        return None

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_record(self, record: dict[str, Any]) -> Path:
        path = self._path_for(record["repo_name"], record["state"], record["id"])
        self._atomic_write(path, json.dumps(record))
        return path

    def _all_records(self) -> list[dict[str, Any]]:
        records = []
        for path in self.state_dir.glob("*/*/*.json"):
            if path.name.endswith(".tmp"):
                continue
            records.append(self._read(path))
        return records

    def _records_for_repo(self, repo_name: str) -> list[dict[str, Any]]:
        repo_dir = self.state_dir / self._slug(repo_name)
        records = []
        for path in repo_dir.glob("*/*.json"):
            if path.name.endswith(".tmp"):
                continue
            records.append(self._read(path))
        return records

    @staticmethod
    def _new_record(run_id: int, repo_name: str, issue_number: int, **overrides: Any) -> dict[str, Any]:
        now = utc_now()
        record = {
            "id": run_id,
            "repo_name": repo_name,
            "issue_number": int(issue_number),
            "issue_title": "",
            "issue_body": "",
            "issue_url": "",
            "branch_name": "",
            "state": "queued",
            "stage": "queued",
            "attempt": 1,
            "run_dir": "",
            "worktree_path": "",
            "codex_thread_id": "",
            "pr_url": "",
            "pr_ci_status": "",
            "pr_ci_summary": "",
            "pr_ci_checked_at": "",
            "ci_fix_attempts": 0,
            "ci_fix_last_sha": "",
            "ai_review_status": "",
            "ai_review_summary": "",
            "ai_review_feedback": "",
            "ai_review_checked_at": "",
            "ai_review_head_sha": "",
            "last_error": "",
            "dependencies": [],
            "blocked_by": [],
            "dependency_state": "",
            "dependency_overrides": [],
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "ended_at": "",
            "events": [],
        }
        record.update(overrides)
        return record

    # --------------------------------------------------------------- run API
    def create_run(
        self,
        *,
        repo_name: str,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        branch_name: str,
        issue_body: str = "",
    ) -> int:
        with self._lock:
            run_id = self._next("id")
            attempt = self.next_attempt(repo_name, issue_number)
            record = self._new_record(
                run_id,
                repo_name,
                issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                issue_url=issue_url,
                branch_name=branch_name,
                attempt=attempt,
            )
            self._write_record(record)
            self.add_event(run_id, "info", "queued", f"Queued issue #{issue_number}", {})
            return run_id

    def create_available(
        self,
        *,
        repo_name: str,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        issue_body: str = "",
    ) -> int:
        """Create a synced-issue record in the ``available`` state (not a run)."""
        with self._lock:
            run_id = self._next("id")
            record = self._new_record(
                run_id,
                repo_name,
                issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                issue_url=issue_url,
                state="available",
                stage="",
            )
            self._write_record(record)
            return run_id

    def next_attempt(self, repo_name: str, issue_number: int) -> int:
        attempts = [
            int(record["attempt"])
            for record in self._records_for_repo(repo_name)
            if record["issue_number"] == int(issue_number)
        ]
        return (max(attempts) + 1) if attempts else 1

    def get_run(self, run_id: int) -> dict[str, Any]:
        path = self._find_path(int(run_id))
        if path is None:
            raise KeyError(f"run {run_id} does not exist")
        return self._read(path)

    def find_open_run(self, repo_name: str, issue_number: int) -> dict[str, Any] | None:
        candidates = [
            record
            for record in self._records_for_repo(repo_name)
            if record["issue_number"] == int(issue_number) and record["state"] not in _OPEN_EXCLUDE
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record["id"])

    def get_record(self, repo_name: str, issue_number: int) -> dict[str, Any] | None:
        """Return the latest record for an issue in any state (including available)."""
        candidates = [
            record
            for record in self._records_for_repo(repo_name)
            if record["issue_number"] == int(issue_number)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record["id"])

    def list_records(self, repo_name: str) -> list[dict[str, Any]]:
        """Return the latest record per issue for a repo (for the issue picker)."""
        latest: dict[int, dict[str, Any]] = {}
        for record in self._records_for_repo(repo_name):
            number = record["issue_number"]
            if number not in latest or record["id"] > latest[number]["id"]:
                latest[number] = record
        return sorted(latest.values(), key=lambda record: record["issue_number"], reverse=True)

    def list_runs(self, states: set[str] | None = None) -> list[dict[str, Any]]:
        records = self._all_records()
        if states:
            selected = [record for record in records if record["state"] in states]
            selected.sort(key=lambda record: record["id"], reverse=True)
            return selected
        selected = [record for record in records if record["state"] != "available"]
        selected.sort(key=lambda record: record["id"], reverse=True)
        return selected[:100]

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        with self._lock:
            path = self._find_path(int(run_id))
            if path is None:
                raise KeyError(f"run {run_id} does not exist")
            record = self._read(path)
            old_state = record["state"]
            fields["updated_at"] = utc_now()
            if fields.get("state") in TERMINAL_STATES:
                fields.setdefault("ended_at", utc_now())
            record.update(fields)
            self._write_record(record)
            if record["state"] != old_state:
                old_path = self._path_for(record["repo_name"], old_state, record["id"])
                if old_path.exists():
                    old_path.unlink()

    def add_event(
        self,
        run_id: int,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            path = self._find_path(int(run_id))
            if path is None:
                raise KeyError(f"run {run_id} does not exist")
            record = self._read(path)
            record["events"].append(
                {
                    "seq": self._next("event"),
                    "run_id": int(run_id),
                    "repo_name": record["repo_name"],
                    "issue_number": record["issue_number"],
                    "level": level,
                    "event_type": event_type,
                    "message": message,
                    "payload": payload,
                    "created_at": utc_now(),
                }
            )
            self._write_record(record)

    def dashboard_state(self) -> dict[str, Any]:
        runs = self.list_runs()
        events = []
        for record in self._all_records():
            events.extend(record.get("events", []))
        events.sort(key=lambda event: event.get("seq", 0), reverse=True)
        events = events[:200]
        stats: dict[str, int] = {}
        for run in runs:
            stats[run["state"]] = stats.get(run["state"], 0) + 1
        return {"runs": runs, "events": events, "stats": stats}
