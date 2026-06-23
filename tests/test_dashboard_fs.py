import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_desk.config import load_config
from agent_desk.dashboard import HTML, serve_dashboard
from agent_desk.scheduler import Scheduler
from agent_desk.store import Store

CLONE_CONFIG = """
[agent_desk]
data_dir = "{data_dir}"
clone_root = "{clone_root}"

[[repos]]
name = "octo/example"
local_path = "{existing}"
base_branch = "main"
test_command = "python -m unittest"
""".strip()


def _free_port(host: str) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class DashboardFsTests(unittest.TestCase):
    def _serve(self, **kwargs) -> tuple[str, int]:
        host = "127.0.0.1"
        bound: dict[str, int] = {}
        ready = threading.Event()

        def on_serving(_host: str, port: int) -> None:
            bound["port"] = port
            ready.set()

        thread = threading.Thread(
            target=serve_dashboard,
            kwargs={"host": host, "port": _free_port(host), "on_serving": on_serving, **kwargs},
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(timeout=5), "dashboard never bound")
        return host, bound["port"]

    def _get(self, host: str, port: int, path: str) -> dict:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as response:
            return json.loads(response.read())

    def _post(self, host: str, port: int, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"http://{host}:{port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_fs_listing_marks_git_repos_and_skips_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "plain").mkdir()
            (root / "repo" / ".git").mkdir(parents=True)
            (root / ".hidden").mkdir()
            store = Store(root / "desk.sqlite")
            host, port = self._serve(store=store)

            data = self._get(host, port, "/api/fs?path=" + str(root))

            entries = {entry["name"]: entry for entry in data["entries"]}
            self.assertIn("plain", entries)
            self.assertIn("repo", entries)
            self.assertNotIn(".hidden", entries)
            self.assertTrue(entries["repo"]["is_git"])
            self.assertFalse(entries["plain"]["is_git"])
            self.assertEqual(data["path"], str(root))
            self.assertEqual(data["parent"], str(root.parent))

    def test_fs_listing_rejects_non_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            target.write_text("x", encoding="utf-8")
            store = Store(Path(tmp) / "desk.sqlite")
            host, port = self._serve(store=store)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get(host, port, "/api/fs?path=" + str(target))
            self.assertEqual(ctx.exception.code, 400)

    def test_clone_endpoint_registers_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            clones = root / "clones"
            (clones / "octo" / "new").mkdir(parents=True)
            config_path = root / "repos.toml"
            config_path.write_text(
                CLONE_CONFIG.format(
                    data_dir=root, clone_root=clones, existing=root / "existing"
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            store = Store(config.data_dir / "desk.sqlite")
            scheduler = Scheduler(config, store)
            host, port = self._serve(store=store, scheduler=scheduler, config_path=config_path)

            data = self._post(host, port, "/api/projects/clone", {"repo": "octo/new"})

            self.assertTrue(data["ok"])
            self.assertEqual(data["repo"]["name"], "octo/new")
            names = {item.name for item in load_config(config_path).repos}
            self.assertIn("octo/new", names)

    def test_clone_endpoint_requires_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config_path = root / "repos.toml"
            config_path.write_text(
                CLONE_CONFIG.format(
                    data_dir=root, clone_root=root / "clones", existing=root / "existing"
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            store = Store(config.data_dir / "desk.sqlite")
            scheduler = Scheduler(config, store)
            host, port = self._serve(store=store, scheduler=scheduler, config_path=config_path)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post(host, port, "/api/projects/clone", {"repo": "  "})
            self.assertEqual(ctx.exception.code, 400)

    def test_html_exposes_clone_and_browse_controls(self):
        self.assertIn("/api/projects/clone", HTML)
        self.assertIn('id="clone-spec"', HTML)
        self.assertIn('id="fs-browser"', HTML)
        self.assertIn("browseTo", HTML)


if __name__ == "__main__":
    unittest.main()
