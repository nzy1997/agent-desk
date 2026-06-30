from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
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


def _is_expected_supervisor(info: ProcessInfo, run_id: int) -> bool:
    command = info.command
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if "agent_desk" not in tokens or "run-job" not in tokens:
        return False
    expected = str(int(run_id))
    for index, token in enumerate(tokens):
        if token == "--run-id" and index + 1 < len(tokens):
            return tokens[index + 1] == expected
        if token.startswith("--run-id="):
            return token.split("=", 1)[1] == expected
    return False


def _int_field(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_run_shutdown_item(
    run: dict[str, Any], controller: ProcessController
) -> dict[str, Any]:
    run_id = int(run["id"])
    warnings: list[str] = []
    supervisor_pid = _int_field(run.get("supervisor_pid"))
    thread_id = recover_thread_id_from_run(run)
    worktree_path = str(run.get("worktree_path") or "")
    resume_command = format_resume_command(thread_id, worktree_path)
    resume_available = bool(thread_id and worktree_path)
    resume_unavailable_reason = ""
    if not thread_id:
        resume_unavailable_reason = "missing Codex thread id"
    elif not worktree_path:
        resume_unavailable_reason = "missing worktree path"

    info = controller.process_info(supervisor_pid) if supervisor_pid else None
    processes: list[ProcessInfo] = []
    pgid = 0
    killable = False
    if not supervisor_pid:
        warnings.append("no supervisor_pid recorded")
    elif info is None:
        warnings.append(f"supervisor PID {supervisor_pid} is not running")
    elif not _is_expected_supervisor(info, run_id):
        warnings.append(
            f"supervisor PID {supervisor_pid} is not an Agent Desk run-job for run #{run_id}"
        )
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
        "affected_runs": [int(item["run_id"]) for item in items],
        "runs": items,
        "manifest_path": str(manifest_path),
    }
    if extra_fields:
        manifest.update(extra_fields)
    _write_json(manifest_path, manifest)

    for item in items:
        run_dir_raw = str(item.get("run_dir") or "")
        if not run_dir_raw:
            continue
        run_dir = Path(run_dir_raw)
        run_dir.mkdir(parents=True, exist_ok=True)
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
        try:
            controller.terminate_group(pgid)
        except ProcessLookupError:
            results.append({"run_id": run_id, "pgid": pgid, "result": "already-exited"})
            continue
        if grace_seconds:
            time.sleep(grace_seconds)
        if supervisor_pid and controller.pid_alive(supervisor_pid):
            try:
                controller.kill_group(pgid)
            except ProcessLookupError:
                results.append({"run_id": run_id, "pgid": pgid, "result": "terminated"})
            else:
                results.append({"run_id": run_id, "pgid": pgid, "result": "killed"})
        else:
            results.append({"run_id": run_id, "pgid": pgid, "result": "terminated"})
    return results
