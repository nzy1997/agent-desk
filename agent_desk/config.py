from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import tomllib

from .ai_settings import DEFAULT_AI_MODEL, DEFAULT_AI_REASONING_EFFORT


DEFAULT_WORKER_TIMEOUT_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class RepoConfig:
    name: str
    local_path: Path
    base_branch: str = "main"
    # Legacy no-op settings accepted from older configs. Desk state is local.
    ready_label: str = "agent:ready"
    running_label: str = "agent:running"
    pr_open_label: str = "agent:pr-open"
    blocked_label: str = "agent:blocked"
    needs_review_label: str = "agent:needs-human-review"
    test_command: str = ""
    auto_start_ready: bool = False
    max_concurrent_runs: int = 1
    requires_human_review: bool = True
    enable_ai_review: bool = False
    single_closeout_per_workspace: bool = True
    # Legacy no-op setting accepted from older configs.
    mutate_github: bool = False
    push_pr: bool = False
    closeout_sandbox: str = "workspace-write"
    default_ai_model: str = DEFAULT_AI_MODEL
    default_ai_reasoning_effort: str = DEFAULT_AI_REASONING_EFFORT


@dataclass(frozen=True)
class AgentDeskConfig:
    data_dir: Path
    poll_interval_seconds: int = 60
    max_concurrent_runs: int = 3
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    worker_timeout_seconds: int = DEFAULT_WORKER_TIMEOUT_SECONDS
    worker_idle_timeout_seconds: int = 600
    clone_root: Path = field(default_factory=lambda: Path.home() / ".agent-desk" / "repos")
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
                enable_ai_review=bool(repo_raw.get("enable_ai_review", False)),
                single_closeout_per_workspace=bool(repo_raw.get("single_closeout_per_workspace", True)),
                mutate_github=bool(repo_raw.get("mutate_github", False)),
                push_pr=bool(repo_raw.get("push_pr", False)),
                closeout_sandbox=repo_raw.get("closeout_sandbox", "workspace-write"),
                default_ai_model=repo_raw.get("default_ai_model", DEFAULT_AI_MODEL),
                default_ai_reasoning_effort=repo_raw.get(
                    "default_ai_reasoning_effort",
                    DEFAULT_AI_REASONING_EFFORT,
                ),
            )
        )
    return AgentDeskConfig(
        data_dir=_resolve_path(root, desk_raw.get("data_dir", ".agent-desk")),
        poll_interval_seconds=int(desk_raw.get("poll_interval_seconds", 60)),
        max_concurrent_runs=int(desk_raw.get("max_concurrent_runs", 3)),
        dashboard_host=desk_raw.get("dashboard_host", "127.0.0.1"),
        dashboard_port=int(desk_raw.get("dashboard_port", 8765)),
        worker_timeout_seconds=int(
            desk_raw.get("worker_timeout_seconds", DEFAULT_WORKER_TIMEOUT_SECONDS)
        ),
        worker_idle_timeout_seconds=int(desk_raw.get("worker_idle_timeout_seconds", 600)),
        clone_root=_resolve_path(root, desk_raw.get("clone_root", "~/.agent-desk/repos")),
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
        test_command=template.test_command,
        auto_start_ready=template.auto_start_ready,
        max_concurrent_runs=template.max_concurrent_runs,
        requires_human_review=template.requires_human_review,
        enable_ai_review=template.enable_ai_review,
        single_closeout_per_workspace=template.single_closeout_per_workspace,
        push_pr=template.push_pr,
        closeout_sandbox=template.closeout_sandbox,
        default_ai_model=template.default_ai_model,
        default_ai_reasoning_effort=template.default_ai_reasoning_effort,
    )
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")
        handle.write(_repo_config_toml(repo))
    return repo


def parse_repo_spec(spec: str) -> str:
    """Normalize an ``OWNER/REPO`` shorthand or a GitHub URL to ``OWNER/REPO``."""
    text = spec.strip().rstrip("/")
    match = re.fullmatch(r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?", text)
    if match:
        return f"{match.group('owner')}/{match.group('repo')}"
    return parse_github_repo_name(text)


def clone_repo(clone_root: str | Path, spec: str, runner=subprocess.run) -> Path:
    """Clone ``spec`` into ``clone_root/OWNER/REPO`` and return the target path.

    If the target already exists it is reused (clone is skipped), so this is
    idempotent. ``runner`` is injected to keep the call testable.
    """
    name = parse_repo_spec(spec)
    if not name:
        raise ValueError(f"could not parse repository: {spec}")
    owner, repo = name.split("/", 1)
    target = Path(clone_root).expanduser() / owner / repo
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"clone target exists and is not a directory: {target}")
        return target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = runner(
        ["gh", "repo", "clone", name, str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(f"clone failed: {completed.stderr.strip() or 'unknown error'}")
    return target.resolve()


def add_remote_repo_to_config(config_path: str | Path, spec: str, runner=subprocess.run) -> RepoConfig:
    """Clone ``spec`` into the configured ``clone_root`` and register it."""
    config = load_config(config_path)
    target = clone_repo(config.clone_root, spec, runner=runner)
    return add_project_to_config(config_path, target, repo_name=parse_repo_spec(spec))


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
            f"test_command = {_toml_string(repo.test_command)}",
            f"auto_start_ready = {_toml_bool(repo.auto_start_ready)}",
            f"max_concurrent_runs = {repo.max_concurrent_runs}",
            f"requires_human_review = {_toml_bool(repo.requires_human_review)}",
            f"enable_ai_review = {_toml_bool(repo.enable_ai_review)}",
            f"single_closeout_per_workspace = {_toml_bool(repo.single_closeout_per_workspace)}",
            f"push_pr = {_toml_bool(repo.push_pr)}",
            f"closeout_sandbox = {_toml_string(repo.closeout_sandbox)}",
            f"default_ai_model = {_toml_string(repo.default_ai_model)}",
            f"default_ai_reasoning_effort = {_toml_string(repo.default_ai_reasoning_effort)}",
        ]
    )


def example_config() -> str:
    return """[agent_desk]
data_dir = ".agent-desk"
poll_interval_seconds = 60
dashboard_host = "127.0.0.1"
dashboard_port = 8765
worker_timeout_seconds = 28800
worker_idle_timeout_seconds = 600
# Where repos cloned from the dashboard are stored (clone_root/OWNER/REPO).
clone_root = "~/.agent-desk/repos"

[[repos]]
name = "OWNER/REPO"
local_path = "/absolute/path/to/local/clone"
base_branch = "main"
test_command = "python -m unittest"
auto_start_ready = false
max_concurrent_runs = 1
requires_human_review = true
enable_ai_review = false
single_closeout_per_workspace = true

# Keep PR publishing disabled until you are comfortable with the loop.
push_pr = false
closeout_sandbox = "workspace-write"
default_ai_model = "gpt-5.5"
default_ai_reasoning_effort = "xhigh"
"""
