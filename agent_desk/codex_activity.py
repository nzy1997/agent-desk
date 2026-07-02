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
                return
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    visit(json.loads(stripped))
                except json.JSONDecodeError:
                    return

    visit(payload)
    return found


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
        self._root_thread_id: str | None = None

    def poll(self, *, now: float | None = None) -> ActivitySignal:
        if self._disabled:
            return ActivitySignal(False)
        current = time.monotonic() if now is None else now
        if current - self._last_poll_at < self.poll_interval_seconds:
            return ActivitySignal(False)
        self._last_poll_at = current
        try:
            pending = sorted(self.thread_ids)
            pending.extend(sorted(self._discover_from_stdout()))
            queued: set[str] = set(pending)
            active_signals: list[ActivitySignal] = []

            while pending:
                thread_id = pending.pop(0)
                if thread_id not in self.thread_ids:
                    self.thread_ids.add(thread_id)

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
                    active_signals.append(ActivitySignal(True, "child thread activity", thread_id))

                for child_thread_id in sorted(self._discover_from_rollout(path)):
                    if child_thread_id not in self.thread_ids:
                        self.thread_ids.add(child_thread_id)
                    if child_thread_id not in queued:
                        queued.add(child_thread_id)
                        pending.append(child_thread_id)

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
                found.update(self._ids_from_stdout_line(line))
            self._stdout_offset = handle.tell()
        return found

    def _discover_from_rollout(self, path: Path) -> set[str]:
        found: set[str] = set()
        offset = self._rollout_offsets.get(path, 0)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if offset > 0:
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

    def _ids_from_stdout_line(self, line: str) -> set[str]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return set()
        found = extract_thread_ids_from_payload(payload)
        if isinstance(payload, dict) and payload.get("type") == "thread.started":
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and _looks_like_thread_id(thread_id):
                self._root_thread_id = thread_id
                found.discard(thread_id)
        if self._root_thread_id is not None:
            found.discard(self._root_thread_id)
        return found

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
