import multiprocessing
import tempfile
import unittest
from pathlib import Path

from agent_desk.repository_setup import fcntl, repository_setup_lock


def hold_repository_lock(data_dir, repo_path, acquired, release):
    with repository_setup_lock(Path(data_dir), Path(repo_path)):
        acquired.set()
        release.wait(timeout=5)


class RepositorySetupLockTests(unittest.TestCase):
    @unittest.skipIf(fcntl is None, "fcntl is unavailable")
    def test_same_repository_is_serialized_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            context = multiprocessing.get_context("spawn")
            first_acquired = context.Event()
            first_release = context.Event()
            second_acquired = context.Event()
            second_release = context.Event()
            first = context.Process(
                target=hold_repository_lock,
                args=(root / "data", repo, first_acquired, first_release),
            )
            second = context.Process(
                target=hold_repository_lock,
                args=(root / "data", repo, second_acquired, second_release),
            )
            try:
                first.start()
                self.assertTrue(first_acquired.wait(timeout=3))
                second.start()
                self.assertFalse(second_acquired.wait(timeout=0.2))
                first_release.set()
                self.assertTrue(second_acquired.wait(timeout=3))
            finally:
                first_release.set()
                second_release.set()
                first.join(timeout=3)
                second.join(timeout=3)
                if first.is_alive():
                    first.terminate()
                if second.is_alive():
                    second.terminate()
