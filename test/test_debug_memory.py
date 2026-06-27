from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import services.debug_memory as debug_memory


class _FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeImageTaskService:
    def __init__(self) -> None:
        self._lock = _FakeLock()
        self._tasks = {
            "owner:task-1": {
                "id": "task-1",
                "owner_id": "owner",
                "status": "success",
                "mode": "generate",
                "updated_at": "2026-06-27 12:00:00",
                "progress": "done",
                "data": [
                    {
                        "b64_json": "A" * 4096,
                        "url": "https://example.test/private-token?secret=abc",
                        "revised_prompt": "secret prompt text",
                    }
                ],
                "error": "secret error",
            }
        }


class _FakeRuntimeState:
    def image_inflight_total(self) -> int:
        return 3


class DebugMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.output_path = self.data_dir / "memory_diagnostics.jsonl"
        (self.data_dir / "image_tasks.json").write_text('{"tasks":[]}\n', encoding="utf-8")
        self.env_patch = mock.patch.dict(
            "os.environ",
            {
                "APP_MEMORY_DIAG_OUTPUT": str(self.output_path),
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.data_patch = mock.patch.object(debug_memory, "DATA_DIR", self.data_dir)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def test_snapshot_summarizes_without_full_sensitive_payloads(self) -> None:
        fake_task_service = _FakeImageTaskService()
        fake_runtime = _FakeRuntimeState()
        with (
            mock.patch.dict("sys.modules", {
                "services.image_task_service": mock.Mock(image_task_service=fake_task_service),
                "services.runtime_state": mock.Mock(runtime_state=fake_runtime),
            }),
        ):
            snapshot = debug_memory.build_memory_snapshot(reason="unit-test", collect=False)

        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual(snapshot["runtime"]["image_inflight_total"], 3)
        self.assertEqual(snapshot["image_tasks"]["loaded_tasks"], 1)
        self.assertEqual(snapshot["image_tasks"]["b64_json_tasks"], 1)
        self.assertIn("data_chars", snapshot["image_tasks"]["recent"][0])
        self.assertNotIn("A" * 128, encoded)
        self.assertNotIn("secret prompt text", encoded)
        self.assertNotIn("private-token", encoded)
        self.assertNotIn("secret error", encoded)

    def test_scheduler_is_disabled_by_default(self) -> None:
        stop_event = threading.Event()
        with mock.patch.dict("os.environ", {"APP_MEMORY_DIAG_ENABLED": ""}, clear=False):
            thread = debug_memory.start_memory_diagnostic_scheduler(stop_event)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.output_path.exists())

    def test_scheduler_writes_jsonl_when_enabled(self) -> None:
        stop_event = threading.Event()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "APP_MEMORY_DIAG_ENABLED": "true",
                    "APP_MEMORY_DIAG_INTERVAL_SECS": "60",
                    "APP_MEMORY_DIAG_OUTPUT": str(self.output_path),
                },
                clear=False,
            ),
            mock.patch.object(debug_memory, "_install_signal_handler", lambda: None),
        ):
            thread = debug_memory.start_memory_diagnostic_scheduler(stop_event)
            deadline = time.time() + 2
            while time.time() < deadline and not self.output_path.exists():
                time.sleep(0.02)
            stop_event.set()
            thread.join(timeout=1)

        self.assertTrue(self.output_path.exists())
        lines = self.output_path.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["reason"], "startup")
        self.assertIn("pid", payload)
        self.assertIn("proc", payload)
        self.assertIn("image_tasks", payload)


if __name__ == "__main__":
    unittest.main()
