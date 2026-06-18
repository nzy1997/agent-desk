from __future__ import annotations

import argparse
from pathlib import Path
import threading

from .config import example_config, load_config
from .continuation import ContinuationRunner
from .dashboard import serve_dashboard
from .scheduler import Scheduler
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-desk")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-config", help="write an example repos.toml")
    init.add_argument("--path", default="config/repos.toml")

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

    args = parser.parse_args(argv)
    if args.command == "init-config":
        path = Path(args.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            parser.error(f"{path} already exists")
        path.write_text(example_config(), encoding="utf-8")
        print(f"Wrote {path}")
        return 0

    config = load_config(args.config)
    store = Store(config.data_dir / "agent-desk.sqlite")
    scheduler = Scheduler(config, store)
    if args.command == "run-next":
        result = scheduler.run_next()
        print(result.message)
        return 0 if result.started else 1
    if args.command == "open-pr":
        result = ContinuationRunner(config, store).open_pull_request(args.run_id)
        print(result.message)
        return 0 if result.ok else 1
    if args.command == "serve":
        host = args.host or config.dashboard_host
        port = args.port or config.dashboard_port
        active_scheduler = None if args.no_scheduler else scheduler
        if active_scheduler:
            thread = threading.Thread(target=active_scheduler.serve_forever, daemon=True)
            thread.start()
        print(f"Agent Desk dashboard: http://{host}:{port}")
        serve_dashboard(host, port, store, active_scheduler, Path(args.config).expanduser().resolve())
        return 0
    parser.error("unreachable")
    return 2
