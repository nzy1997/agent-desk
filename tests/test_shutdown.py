import json
import tempfile
import unittest
from pathlib import Path

from agent_desk.config import AgentDeskConfig
from agent_desk.shutdown import (
    ProcessInfo,
    build_run_shutdown_item,
    recover_thread_id_from_run,
    stop_verified_process_groups,
    write_shutdown_artifacts,
)


class FakeProcessController:
    def __init__(self, infos):
        self.infos = infos

    def process_info(self, pid):
        return self.infos.get(pid)

    def process_group(self, pgid):
        return [info for info in self.infos.values() if info.pgid == pgid]


class SignalController(FakeProcessController):
    def __init__(self, infos):
        super().__init__(infos)
        self.terminated = []
        self.killed = []
        self.alive = {}

    def terminate_group(self, pgid):
        self.terminated.append(pgid)

    def kill_group(self, pgid):
        self.killed.append(pgid)

    def pid_alive(self, pid):
        return self.alive.get(pid, False)


class ShutdownTests(unittest.TestCase):
    def test_recover_thread_id_prefers_store_value(self):
        run = {"codex_thread_id": "stored", "run_dir": ""}

        self.assertEqual(recover_thread_id_from_run(run), "stored")

    def test_recover_thread_id_scans_known_stdout_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "fix-ci-1.stdout.jsonl").write_text(
                json.dumps({"type": "thread.started", "thread_id": "from-log"}) + "\n",
                encoding="utf-8",
            )
            run = {"codex_thread_id": "", "run_dir": str(run_dir)}

            self.assertEqual(recover_thread_id_from_run(run), "from-log")

    def test_build_run_shutdown_item_verifies_expected_supervisor(self):
        controller = FakeProcessController(
            {
                111: ProcessInfo(
                    pid=111,
                    ppid=1,
                    pgid=111,
                    command=(
                        "python -m agent_desk run-job --config config/repos.toml "
                        "--run-id 7 --kind issue"
                    ),
                ),
                112: ProcessInfo(pid=112, ppid=111, pgid=111, command="codex exec --json"),
            }
        )
        run = {
            "id": 7,
            "repo_name": "octo/example",
            "issue_number": 5,
            "issue_title": "Shutdown",
            "state": "running",
            "stage": "running codex",
            "run_dir": "",
            "worktree_path": "/tmp/worktree",
            "codex_thread_id": "thread",
            "supervisor_pid": 111,
        }

        item = build_run_shutdown_item(run, controller)

        self.assertTrue(item["killable"])
        self.assertEqual(item["pgid"], 111)
        self.assertEqual([proc["pid"] for proc in item["processes"]], [111, 112])
        self.assertEqual(item["resume_available"], True)

    def test_build_run_shutdown_item_skips_unverified_pid(self):
        controller = FakeProcessController(
            {111: ProcessInfo(pid=111, ppid=1, pgid=111, command="python unrelated.py")}
        )
        run = {
            "id": 7,
            "repo_name": "octo/example",
            "issue_number": 5,
            "issue_title": "Shutdown",
            "state": "running",
            "stage": "running codex",
            "run_dir": "",
            "worktree_path": "/tmp/worktree",
            "codex_thread_id": "thread",
            "supervisor_pid": 111,
        }

        item = build_run_shutdown_item(run, controller)

        self.assertFalse(item["killable"])
        self.assertIn("not an Agent Desk run-job", item["warnings"][0])

    def test_write_shutdown_artifacts_writes_global_and_run_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-7"
            run_dir.mkdir(parents=True)
            config = AgentDeskConfig(data_dir=root)
            items = [
                {
                    "run_id": 7,
                    "run_dir": str(run_dir),
                    "repo_name": "octo/example",
                    "issue_number": 5,
                    "issue_title": "Shutdown",
                    "stage": "running codex",
                    "resume_command": "codex resume -C /tmp/w thread",
                    "warnings": [],
                }
            ]

            manifest = write_shutdown_artifacts(
                config=config,
                shutdown_id="2026-06-30T12-00-00Z",
                items=items,
                dashboard_pid=123,
                config_path=Path("config/repos.toml"),
            )

            self.assertTrue(Path(manifest["manifest_path"]).exists())
            self.assertTrue((run_dir / "shutdown-2026-06-30T12-00-00Z.json").exists())
            note = (run_dir / "shutdown-resume-2026-06-30T12-00-00Z.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("codex resume -C /tmp/w thread", note)

    def test_stop_verified_process_groups_terminates_then_kills_live_group(self):
        controller = SignalController({})
        controller.alive = {111: True}
        items = [{"run_id": 7, "pgid": 111, "supervisor_pid": 111, "killable": True}]

        results = stop_verified_process_groups(items, controller, grace_seconds=0)

        self.assertEqual(controller.terminated, [111])
        self.assertEqual(controller.killed, [111])
        self.assertEqual(results[0]["result"], "killed")

    def test_stop_verified_process_groups_skips_unverified_item(self):
        controller = SignalController({})
        items = [{"run_id": 7, "pgid": 111, "supervisor_pid": 111, "killable": False}]

        results = stop_verified_process_groups(items, controller, grace_seconds=0)

        self.assertEqual(controller.terminated, [])
        self.assertEqual(controller.killed, [])
        self.assertEqual(results[0]["result"], "skipped")


if __name__ == "__main__":
    unittest.main()
