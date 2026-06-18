from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


TERMINAL_STATES = {"done", "failed", "blocked", "pr_open", "needs_review"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists runs (
                    id integer primary key autoincrement,
                    repo_name text not null,
                    issue_number integer not null,
                    issue_title text not null,
                    issue_url text not null,
                    branch_name text not null,
                    state text not null default 'queued',
                    stage text not null default 'queued',
                    attempt integer not null default 1,
                    run_dir text not null default '',
                    worktree_path text not null default '',
                    codex_thread_id text not null default '',
                    pr_url text not null default '',
                    last_error text not null default '',
                    created_at text not null,
                    updated_at text not null,
                    started_at text not null default '',
                    ended_at text not null default ''
                );

                create table if not exists events (
                    id integer primary key autoincrement,
                    run_id integer not null,
                    repo_name text not null,
                    issue_number integer not null,
                    level text not null,
                    event_type text not null,
                    message text not null,
                    payload_json text not null,
                    created_at text not null,
                    foreign key(run_id) references runs(id)
                );

                create index if not exists idx_runs_issue
                    on runs(repo_name, issue_number, state);
                create index if not exists idx_events_run
                    on events(run_id, id desc);
                """
            )
            self._ensure_column(conn, "runs", "run_dir", "text not null default ''")
            self._ensure_column(conn, "runs", "codex_thread_id", "text not null default ''")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def create_run(
        self,
        *,
        repo_name: str,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        branch_name: str,
    ) -> int:
        now = utc_now()
        attempt = self.next_attempt(repo_name, issue_number)
        with self.connect() as conn:
            cur = conn.execute(
                """
                insert into runs (
                    repo_name, issue_number, issue_title, issue_url, branch_name,
                    attempt, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repo_name, issue_number, issue_title, issue_url, branch_name, attempt, now, now),
            )
            run_id = int(cur.lastrowid)
        self.add_event(run_id, "info", "queued", f"Queued issue #{issue_number}", {})
        return run_id

    def next_attempt(self, repo_name: str, issue_number: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "select max(attempt) as attempt from runs where repo_name = ? and issue_number = ?",
                (repo_name, issue_number),
            ).fetchone()
        if not row or row["attempt"] is None:
            return 1
        return int(row["attempt"]) + 1

    def get_run(self, run_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run {run_id} does not exist")
        return dict(row)

    def find_open_run(self, repo_name: str, issue_number: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select * from runs
                where repo_name = ? and issue_number = ?
                  and state not in ('done', 'failed', 'blocked')
                order by id desc
                limit 1
                """,
                (repo_name, issue_number),
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, states: set[str] | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = conn.execute(
                    f"select * from runs where state in ({placeholders}) order by id desc",
                    tuple(sorted(states)),
                ).fetchall()
            else:
                rows = conn.execute("select * from runs order by id desc limit 100").fetchall()
        return [dict(row) for row in rows]

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        if fields.get("state") in {"done", "failed", "blocked", "pr_open", "needs_review"}:
            fields.setdefault("ended_at", utc_now())
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [run_id]
        with self.connect() as conn:
            conn.execute(f"update runs set {assignments} where id = ?", values)

    def add_event(
        self,
        run_id: int,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        run = self.get_run(run_id)
        with self.connect() as conn:
            conn.execute(
                """
                insert into events (
                    run_id, repo_name, issue_number, level, event_type,
                    message, payload_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    run["repo_name"],
                    run["issue_number"],
                    level,
                    event_type,
                    message,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                ),
            )

    def dashboard_state(self) -> dict[str, Any]:
        runs = self.list_runs()
        with self.connect() as conn:
            event_rows = conn.execute(
                "select * from events order by id desc limit 200"
            ).fetchall()
        events = []
        for row in event_rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        stats: dict[str, int] = {}
        for run in runs:
            stats[run["state"]] = stats.get(run["state"], 0) + 1
        return {"runs": runs, "events": events, "stats": stats}
