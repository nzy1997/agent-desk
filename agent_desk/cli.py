from __future__ import annotations

import argparse
from pathlib import Path
import threading

from .config import (
    add_project_to_config,
    add_remote_repo_to_config,
    example_config,
    load_config,
)
from .continuation import ContinuationRunner
from .dashboard import serve_dashboard
from .scheduler import Scheduler
from .store import Store


def _require_config(parser: argparse.ArgumentParser, config_arg: str) -> Path:
    config_path = Path(config_arg).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    if not config_path.exists():
        parser.error(f"{config_arg} not found; run 'agent-desk init-config' first")
    return config_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-desk")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-config", help="write an example repos.toml")
    init.add_argument("--path", default="config/repos.toml")

    add_repo = sub.add_parser("add-repo", help="register a repository in the config")
    add_repo.add_argument("--config", default="config/repos.toml")
    add_repo_source = add_repo.add_mutually_exclusive_group(required=True)
    add_repo_source.add_argument("--path", help="path to an existing local clone")
    add_repo_source.add_argument(
        "--clone", metavar="OWNER/REPO", help="clone OWNER/REPO (or a URL) into clone_root, then register"
    )
    add_repo.add_argument(
        "--name", default="", help="OWNER/REPO for --path (inferred from the git origin remote if omitted)"
    )

    serve = sub.add_parser("serve", help="start dashboard and scheduler")
    serve.add_argument("--config", default="config/repos.toml")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-scheduler", action="store_true")

    run_next = sub.add_parser("run-next", help="claim and run the next ready issue")
    run_next.add_argument("--config", default="config/repos.toml")

    open_pr = sub.add_parser("open-pr", help="resume a Codex thread to open a pull request for a run")
    open_pr.add_argument("--config", default="config/repos.toml")
    open_pr.add_argument("--run-id", type=int, required=True)

    # Internal: a detached supervisor process for one run, spawned by the server.
    run_job = sub.add_parser("run-job", help="(internal) run one detached job for a run")
    run_job.add_argument("--config", default="config/repos.toml")
    run_job.add_argument("--run-id", type=int, required=True)
    run_job.add_argument("--kind", required=True)

    args = parser.parse_args(argv)
    if args.command == "init-config":
        path = Path(args.path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            print(f"{path} already exists")
            return 0
        path.write_text(example_config(), encoding="utf-8")
        print(f"Wrote {path}")
        return 0

    if args.command == "add-repo":
        config_path = _require_config(parser, args.config)
        existing = {repo.name for repo in load_config(config_path).repos}
        try:
            if args.clone:
                repo = add_remote_repo_to_config(config_path, args.clone)
            else:
                repo = add_project_to_config(config_path, args.path, repo_name=args.name)
        except ValueError as error:
            print(f"error: {error}")
            return 1
        if repo.name in existing:
            print(f"{repo.name} is already configured ({repo.local_path})")
        else:
            print(f"Added {repo.name} -> {repo.local_path}")
            print(f"Review base_branch and test_command in {config_path} before serving.")
        return 0

    config_path = _require_config(parser, args.config)
    config = load_config(config_path)
    config_path = config_path.resolve()
    store = Store(config.data_dir / "agent-desk.sqlite")
    # serve and run-job dispatch work as detached processes that outlive the
    # server; the one-shot commands run their work inline.
    detach_jobs = args.command in {"serve", "run-job"}
    scheduler = Scheduler(config, store, config_path=config_path, detach_jobs=detach_jobs)
    if args.command == "run-next":
        result = scheduler.run_next()
        print(result.message)
        return 0 if result.started else 1
    if args.command == "open-pr":
        result = ContinuationRunner(config, store).open_pull_request(args.run_id)
        print(result.message)
        return 0 if result.ok else 1
    if args.command == "run-job":
        scheduler.run_job(args.run_id, args.kind)
        return 0
    if args.command == "serve":
        host = args.host or config.dashboard_host
        port = args.port or config.dashboard_port
        active_scheduler = None if args.no_scheduler else scheduler
        if active_scheduler:
            thread = threading.Thread(target=active_scheduler.serve_forever, daemon=True)
            thread.start()
        serve_dashboard(
            host,
            port,
            store,
            active_scheduler,
            config_path,
            on_serving=lambda h, p: print(f"Agent Desk dashboard: http://{h}:{p}", flush=True),
        )
        return 0
    parser.error("unreachable")
    return 2
