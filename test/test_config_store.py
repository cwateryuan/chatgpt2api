import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.config import DEFAULT_PROXY_RUNTIME, ConfigStore


class ConfigStoreTests(unittest.TestCase):
    def _make_store(self, initial: dict[str, object] | None = None) -> tuple[tempfile.TemporaryDirectory[str], ConfigStore]:
        tmp_dir = tempfile.TemporaryDirectory()
        path = Path(tmp_dir.name) / "config.json"
        data = {"auth-key": "test-auth"}
        if initial:
            data.update(initial)
        path.write_text(json.dumps(data), encoding="utf-8")
        return tmp_dir, ConfigStore(path)

    def test_stale_worker_update_preserves_newer_proxy_runtime(self) -> None:
        tmp_dir, worker_b = self._make_store()
        with tmp_dir:
            worker_a = ConfigStore(worker_b.path)
            worker_b_stale_runtime = copy.deepcopy(worker_b.get()["proxy_runtime"])

            warp_runtime = copy.deepcopy(DEFAULT_PROXY_RUNTIME)
            warp_runtime["enabled"] = True
            warp_runtime["egress_mode"] = "single_proxy"
            warp_runtime["proxy_url"] = "http://privoxy:8118"
            warp_runtime["clearance"]["enabled"] = True
            warp_runtime["clearance"]["mode"] = "flaresolverr"
            warp_runtime["clearance"]["flaresolverr_url"] = "http://flaresolverr:8191"
            worker_a.update({"proxy_runtime": warp_runtime})

            worker_b.update(
                {
                    "image_poll_timeout_secs": 180,
                    "proxy_runtime": worker_b_stale_runtime,
                }
            )

            saved = json.loads(worker_b.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["image_poll_timeout_secs"], 180)
            self.assertTrue(saved["proxy_runtime"]["enabled"])
            self.assertEqual(saved["proxy_runtime"]["proxy_url"], "http://privoxy:8118")
            self.assertTrue(saved["proxy_runtime"]["clearance"]["enabled"])
            self.assertEqual(saved["proxy_runtime"]["clearance"]["mode"], "flaresolverr")

    def test_stale_worker_update_preserves_newer_log_levels_and_timeout(self) -> None:
        tmp_dir, worker_b = self._make_store(
            {
                "log_levels": ["debug", "info", "warning", "error"],
                "image_poll_timeout_secs": 120,
            }
        )
        with tmp_dir:
            worker_a = ConfigStore(worker_b.path)
            stale_payload = {
                "log_levels": list(worker_b.log_levels),
                "image_poll_timeout_secs": worker_b.image_poll_timeout_secs,
                "base_url": "https://api.example",
            }

            worker_a.update({"log_levels": ["error", "warning"], "image_poll_timeout_secs": 240})
            worker_b.update(stale_payload)

            saved = json.loads(worker_b.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["base_url"], "https://api.example")
            self.assertEqual(saved["log_levels"], ["error", "warning"])
            self.assertEqual(saved["image_poll_timeout_secs"], 240)

    def test_reads_reload_when_config_file_changes(self) -> None:
        tmp_dir, store = self._make_store({"image_poll_timeout_secs": 120, "log_levels": ["info"]})
        with tmp_dir:
            data = json.loads(store.path.read_text(encoding="utf-8"))
            data["image_poll_timeout_secs"] = 333
            data["log_levels"] = []
            store.path.write_text(json.dumps(data), encoding="utf-8")

            self.assertEqual(store.image_poll_timeout_secs, 333)
            self.assertEqual(store.log_levels, [])

    def test_base_url_prefers_saved_config_over_environment(self) -> None:
        tmp_dir, store = self._make_store({"base_url": "https://img3.135335.xyz/"})
        with tmp_dir, mock.patch.dict(os.environ, {"CHATGPT2API_BASE_URL": "https://old-api.example.com"}):
            self.assertEqual(store.base_url, "https://img3.135335.xyz")

    def test_base_url_uses_environment_as_fallback(self) -> None:
        tmp_dir, store = self._make_store({"base_url": ""})
        with tmp_dir, mock.patch.dict(os.environ, {"CHATGPT2API_BASE_URL": "https://fallback.example.com/"}):
            self.assertEqual(store.base_url, "https://fallback.example.com")


if __name__ == "__main__":
    unittest.main()
