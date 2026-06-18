from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class RepoConfig:
    name: str
    local_path: Path
    base_branch: str = "main"
    ready_label: str = "agent:ready"
    running_label: str = "agent:running"
    pr_open_label: str = "agent:pr-open"
    blocked_label: str = "agent:blocked"
    needs_review_label: str = "agent:needs-human-review"
    test_command: str = ""
    mutate_github: bool = False
    push_pr: bool = False
    closeout_sandbox: str = "workspace-write"


@dataclass(frozen=True)
class AgentDeskConfig:
    data_dir: Path
    poll_interval_seconds: int = 60
    max_concurrent_runs: int = 1
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    worker_timeout_seconds: int = 7200
    worker_idle_timeout_seconds: int = 600
    repos: list[RepoConfig] = field(default_factory=list)


def _resolve_path(base: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base / path


def load_config(path: str | Path) -> AgentDeskConfig:
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent
    desk_raw = raw.get("agent_desk", {})
    repos = []
    for repo_raw in raw.get("repos", []):
        repos.append(
            RepoConfig(
                name=repo_raw["name"],
                local_path=_resolve_path(root, repo_raw["local_path"]),
                base_branch=repo_raw.get("base_branch", "main"),
                ready_label=repo_raw.get("ready_label", "agent:ready"),
                running_label=repo_raw.get("running_label", "agent:running"),
                pr_open_label=repo_raw.get("pr_open_label", "agent:pr-open"),
                blocked_label=repo_raw.get("blocked_label", "agent:blocked"),
                needs_review_label=repo_raw.get("needs_review_label", "agent:needs-human-review"),
                test_command=repo_raw.get("test_command", ""),
                mutate_github=bool(repo_raw.get("mutate_github", False)),
                push_pr=bool(repo_raw.get("push_pr", False)),
                closeout_sandbox=repo_raw.get("closeout_sandbox", "workspace-write"),
            )
        )
    return AgentDeskConfig(
        data_dir=_resolve_path(root, desk_raw.get("data_dir", ".agent-desk")),
        poll_interval_seconds=int(desk_raw.get("poll_interval_seconds", 60)),
        max_concurrent_runs=int(desk_raw.get("max_concurrent_runs", 1)),
        dashboard_host=desk_raw.get("dashboard_host", "127.0.0.1"),
        dashboard_port=int(desk_raw.get("dashboard_port", 8765)),
        worker_timeout_seconds=int(desk_raw.get("worker_timeout_seconds", 7200)),
        worker_idle_timeout_seconds=int(desk_raw.get("worker_idle_timeout_seconds", 600)),
        repos=repos,
    )


def example_config() -> str:
    return """[agent_desk]
data_dir = ".agent-desk"
poll_interval_seconds = 60
max_concurrent_runs = 1
dashboard_host = "127.0.0.1"
dashboard_port = 8765
worker_timeout_seconds = 7200
worker_idle_timeout_seconds = 600

[[repos]]
name = "OWNER/REPO"
local_path = "/absolute/path/to/local/clone"
base_branch = "main"
ready_label = "agent:ready"
running_label = "agent:running"
pr_open_label = "agent:pr-open"
blocked_label = "agent:blocked"
needs_review_label = "agent:needs-human-review"
test_command = "python -m unittest"

# Keep both false until you are comfortable with the loop.
mutate_github = false
push_pr = false
closeout_sandbox = "workspace-write"
"""
