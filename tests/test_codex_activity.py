import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_desk.codex_activity import (
    CodexThreadActivityMonitor,
    extract_thread_ids_from_payload,
)


class CodexActivityTests(unittest.TestCase):
    def test_extracts_spawn_agent_thread_ids_from_nested_payloads(self):
        child = "019f1e7f-2c4c-7063-af43-6e97371de397"
        payload = {
            "type": "item.completed",
            "item": {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "receiver_thread_ids": [child],
            },
        }

        self.assertEqual(extract_thread_ids_from_payload(payload), {child})

    def test_extracts_thread_id_from_json_string_tool_output(self):
        child = "019f1e7f-2c4c-7063-af43-6e97371de397"
        payload = {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": json.dumps({"agent_id": child, "nickname": "Sartre"}),
            },
        }

        self.assertEqual(extract_thread_ids_from_payload(payload), {child})

    def test_monitor_discovers_child_rollout_and_reports_activity_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            stdout_path.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "receiver_thread_ids": [child],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )

            first = monitor.poll(now=time.monotonic())
            with child_rollout.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"agent_message","text":"still working"}\n')
            second = monitor.poll(now=time.monotonic())

        self.assertTrue(first.active)
        self.assertIn(child, first.detail)
        self.assertTrue(second.active)
        self.assertIn("child thread", second.source)

    def test_monitor_discovers_grandchild_from_child_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            grandchild = "019f1e80-1111-7222-8333-444455556666"
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text(
                json.dumps({"payload": {"output": json.dumps({"agent_id": grandchild})}}) + "\n",
                encoding="utf-8",
            )
            grandchild_rollout = sessions / f"rollout-2026-07-02T00-25-00-{grandchild}.jsonl"
            grandchild_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            stdout_path.write_text(
                json.dumps({"item": {"receiver_thread_ids": [child]}}) + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )

            monitor.poll(now=time.monotonic())
            with grandchild_rollout.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"agent_message","text":"grandchild update"}\n')
            signal = monitor.poll(now=time.monotonic())

        self.assertTrue(signal.active)
        self.assertIn(grandchild, signal.detail)

    def test_monitor_does_not_rewatch_root_thread_from_child_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp)
            stdout_path = root_dir / "stdout.jsonl"
            codex_home = root_dir / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            root_thread = "019f1e7f-2c4c-7063-af43-6e97371de397"
            child = "019f1e80-1111-7222-8333-444455556666"
            parent_rollout = sessions / f"rollout-2026-07-02T00-24-37-{root_thread}.jsonl"
            parent_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text(json.dumps({"thread_id": root_thread}) + "\n", encoding="utf-8")
            stdout_path.write_text(
                json.dumps(
                    {
                        "type": "thread.started",
                        "thread_id": root_thread,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "receiver_thread_ids": [child],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )

            signal = monitor.poll(now=time.monotonic())

        self.assertTrue(signal.active)
        self.assertIn(child, signal.detail)
        self.assertNotIn(root_thread, signal.detail)
        self.assertIn(child, monitor.thread_ids)
        self.assertNotIn(root_thread, monitor.thread_ids)

    def test_parent_thread_started_rollout_does_not_count_as_descendant_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            parent = "019f1e7f-2c4c-7063-af43-6e97371de397"
            parent_rollout = sessions / f"rollout-2026-07-02T00-24-38-{parent}.jsonl"
            parent_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            stdout_path.write_text(
                json.dumps({"type": "thread.started", "thread_id": parent}) + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )

            first = monitor.poll(now=time.monotonic())
            with parent_rollout.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"agent_message","text":"parent still running"}\n')
            second = monitor.poll(now=time.monotonic())

        self.assertFalse(first.active)
        self.assertFalse(second.active)

    def test_discovered_child_without_rollout_stays_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            stdout_path.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "receiver_thread_ids": [child],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )

            signal = monitor.poll(now=time.monotonic())

        self.assertFalse(signal.active)
        self.assertEqual(signal.source, "")
        self.assertEqual(signal.detail, "")

    def test_monitor_filesystem_oserror_disables_future_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "stdout.jsonl"
            codex_home = root / "codex"
            sessions = codex_home / "sessions" / "2026" / "07" / "02"
            sessions.mkdir(parents=True)
            child = "019f1e7f-2c4c-7063-af43-6e97371de397"
            child_rollout = sessions / f"rollout-2026-07-02T00-24-38-{child}.jsonl"
            child_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            stdout_path.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "receiver_thread_ids": [child],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            monitor = CodexThreadActivityMonitor(
                stdout_path,
                codex_home=codex_home,
                poll_interval_seconds=0,
            )
            first = monitor.poll(now=time.monotonic())

            try:
                child_rollout.unlink()
                try:
                    child_rollout.symlink_to("missing-rollout-target.jsonl")
                except (NotImplementedError, OSError) as exc:
                    self.fail(f"symlink-based fallback test could not be set up: {exc}")
                second = monitor.poll(now=time.monotonic())
                third = monitor.poll(now=time.monotonic())
            finally:
                if child_rollout.exists() or child_rollout.is_symlink():
                    child_rollout.unlink()
                child_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")

        self.assertTrue(first.active)
        self.assertIn(child, first.detail)
        self.assertFalse(second.active)
        self.assertFalse(third.active)
        self.assertTrue(monitor._disabled)


if __name__ == "__main__":
    unittest.main()
