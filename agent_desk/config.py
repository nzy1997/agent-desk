from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
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
    auto_start_ready: bool = False
    max_concurrent_runs: int = 1
    requires_human_review: bool = True
    single_closeout_per_workspace: bool = True
    mutate_github: bool = False
    push_pr: bool = False
    closeout_sandbox: str = "workspace-write"


@dataclass(frozen=True)
class AgentDeskConfig:
    data_dir: Path
    poll_interval_seconds: int = 60
    max_concurrent_runs: int = 3
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
                auto_start_ready=bool(repo_raw.get("auto_start_ready", False)),
                max_concurrent_runs=max(1, int(repo_raw.get("max_concurrent_runs", 1))),
                requires_human_review=bool(repo_raw.get("requires_human_review", True)),
                single_closeout_per_workspace=bool(repo_raw.get("single_closeout_per_workspace", True)),
                mutate_github=bool(repo_raw.get("mutate_github", False)),
                push_pr=bool(repo_raw.get("push_pr", False)),
                closeout_sandbox=repo_raw.get("closeout_sandbox", "workspace-write"),
            )
        )
    return AgentDeskConfig(
        data_dir=_resolve_path(root, desk_raw.get("data_dir", ".agent-desk")),
        poll_interval_seconds=int(desk_raw.get("poll_interval_seconds", 60)),
        max_concurrent_runs=int(desk_raw.get("max_concurrent_runs", 3)),
        dashboard_host=desk_raw.get("dashboard_host", "127.0.0.1"),
        dashboard_port=int(desk_raw.get("dashboard_port", 8765)),
        worker_timeout_seconds=int(desk_raw.get("worker_timeout_seconds", 7200)),
        worker_idle_timeout_seconds=int(desk_raw.get("worker_idle_timeout_seconds", 600)),
        repos=repos,
    )


def parse_github_repo_name(remote_url: str) -> str:
    patterns = [
        r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, remote_url.strip())
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return ""


def infer_repo_name(local_path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(local_path), "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return parse_github_repo_name(completed.stdout.strip())


def add_project_to_config(config_path: str | Path, local_path: str | Path, repo_name: str = "") -> RepoConfig:
    path = Path(local_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"project folder does not exist: {path}")

    config_path = Path(config_path).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    config = load_config(config_path)
    for repo in config.repos:
        if repo.local_path.resolve() == path or (repo_name and repo.name == repo_name):
            return repo

    name = repo_name or infer_repo_name(path)
    if not name:
        raise ValueError("could not infer GitHub repo name from origin remote")

    template = config.repos[0] if config.repos else RepoConfig(name=name, local_path=path)
    repo = RepoConfig(
        name=name,
        local_path=path,
        base_branch=template.base_branch,
        ready_label=template.ready_label,
        running_label=template.running_label,
        pr_open_label=template.pr_open_label,
        blocked_label=template.blocked_label,
        needs_review_label=template.needs_review_label,
        test_command=template.test_command,
        auto_start_ready=template.auto_start_ready,
        max_concurrent_runs=template.max_concurrent_runs,
        requires_human_review=template.requires_human_review,
        single_closeout_per_workspace=template.single_closeout_per_workspace,
        mutate_github=template.mutate_github,
        push_pr=template.push_pr,
        closeout_sandbox=template.closeout_sandbox,
    )
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")
        handle.write(_repo_config_toml(repo))
    return repo


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value))


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _repo_config_toml(repo: RepoConfig) -> str:
    return "\n".join(
        [
            "[[repos]]",
            f"name = {_toml_string(repo.name)}",
            f"local_path = {_toml_string(repo.local_path)}",
            f"base_branch = {_toml_string(repo.base_branch)}",
            f"ready_label = {_toml_string(repo.ready_label)}",
            f"running_label = {_toml_string(repo.running_label)}",
            f"pr_open_label = {_toml_string(repo.pr_open_label)}",
            f"blocked_label = {_toml_string(repo.blocked_label)}",
            f"needs_review_label = {_toml_string(repo.needs_review_label)}",
            f"test_command = {_toml_string(repo.test_command)}",
            f"auto_start_ready = {_toml_bool(repo.auto_start_ready)}",
            f"max_concurrent_runs = {repo.max_concurrent_runs}",
            f"requires_human_review = {_toml_bool(repo.requires_human_review)}",
            f"single_closeout_per_workspace = {_toml_bool(repo.single_closeout_per_workspace)}",
            f"mutate_github = {_toml_bool(repo.mutate_github)}",
            f"push_pr = {_toml_bool(repo.push_pr)}",
            f"closeout_sandbox = {_toml_string(repo.closeout_sandbox)}",
        ]
    )


def example_config() -> str:
    return """[agent_desk]
data_dir = ".agent-desk"
poll_interval_seconds = 60
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
auto_start_ready = false
max_concurrent_runs = 1
requires_human_review = true
single_closeout_per_workspace = true

# Keep both false until you are comfortable with the loop.
mutate_github = false
push_pr = false
closeout_sandbox = "workspace-write"
"""
