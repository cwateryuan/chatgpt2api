import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import tempfile
import time
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

    def test_full_account_refresh_defaults_enabled_and_updates_partially(self) -> None:
        tmp_dir, store = self._make_store({"base_url": "https://api.example"})
        with tmp_dir:
            self.assertTrue(store.full_account_refresh_enabled)
            self.assertTrue(store.get()["full_account_refresh_enabled"])

            updated = store.update({"full_account_refresh_enabled": False})

            self.assertFalse(store.full_account_refresh_enabled)
            self.assertFalse(updated["full_account_refresh_enabled"])
            self.assertEqual(updated["base_url"], "https://api.example")

    def test_full_account_refresh_parses_string_booleans(self) -> None:
        tmp_dir, store = self._make_store({"full_account_refresh_enabled": "off"})
        with tmp_dir:
            self.assertFalse(store.full_account_refresh_enabled)

    def test_image_retention_converts_legacy_days_to_minutes(self) -> None:
        for days, expected_minutes in ((1, 1440), (15, 21600), (30, 43200)):
            with self.subTest(days=days):
                tmp_dir, store = self._make_store({"image_retention_days": days})
                with tmp_dir:
                    self.assertEqual(store.image_retention_minutes, expected_minutes)
                    self.assertEqual(store.get()["image_retention_minutes"], expected_minutes)

    def test_image_retention_update_uses_canonical_minutes(self) -> None:
        tmp_dir, store = self._make_store({"image_retention_days": 15})
        with tmp_dir:
            updated = store.update({"image_retention_minutes": 60, "image_retention_days": 15})
            saved = json.loads(store.path.read_text(encoding="utf-8"))

            self.assertEqual(updated["image_retention_minutes"], 60)
            self.assertEqual(updated["image_retention_days"], 60 / 1440)
            self.assertEqual(saved["image_retention_minutes"], 60)
            self.assertNotIn("image_retention_days", saved)

            self.assertEqual(store.update({"image_retention_minutes": 29})["image_retention_minutes"], 30)
            self.assertEqual(store.update({"image_retention_minutes": 60.5})["image_retention_minutes"], 30)

    def test_legacy_retention_update_is_still_accepted(self) -> None:
        tmp_dir, store = self._make_store()
        with tmp_dir:
            updated = store.update({"image_retention_days": 1})

            self.assertEqual(updated["image_retention_minutes"], 1440)

    def test_image_cleanup_is_throttled_serialized_and_batches_database_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"auth-key": "test-auth", "image_retention_minutes": 30}),
                encoding="utf-8",
            )
            with mock.patch("services.config.DATA_DIR", root / "data"):
                store = ConfigStore(config_path)
                backend = mock.Mock()
                backend.supports_database_features.return_value = True
                store._storage_backend = backend
                images_dir = store.images_dir
                old_mtime = time.time() - 31 * 60

                for index in range(1001):
                    path = images_dir / "2026" / "08" / "11" / f"{index}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"x")
                    os.utime(path, (old_mtime, old_mtime))

                self.assertEqual(store.cleanup_old_images(force=True), 1001)
                batches = [call.args[0] for call in backend.delete_image_records.call_args_list]
                self.assertEqual([len(batch) for batch in batches], [500, 500, 1])

                throttled_path = images_dir / "throttled.png"
                throttled_path.write_bytes(b"x")
                os.utime(throttled_path, (old_mtime, old_mtime))
                self.assertEqual(store.cleanup_old_images(), 0)
                self.assertTrue(throttled_path.exists())

                store._last_image_cleanup_at = 0.0
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _item: store.cleanup_old_images(), range(2)))
                self.assertEqual(sum(results), 1)
                self.assertFalse(throttled_path.exists())

    def test_image_cleanup_uses_minute_cutoffs(self) -> None:
        for retention_minutes in (30, 60, 1440):
            with self.subTest(retention_minutes=retention_minutes), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_path = root / "config.json"
                config_path.write_text(
                    json.dumps({"auth-key": "test-auth", "image_retention_minutes": retention_minutes}),
                    encoding="utf-8",
                )
                with mock.patch("services.config.DATA_DIR", root / "data"):
                    store = ConfigStore(config_path)
                    backend = mock.Mock()
                    backend.supports_database_features.return_value = False
                    store._storage_backend = backend
                    images_dir = store.images_dir
                    expired = images_dir / "expired.png"
                    retained = images_dir / "retained.png"
                    expired.write_bytes(b"old")
                    retained.write_bytes(b"new")
                    now = time.time()
                    expired_mtime = now - retention_minutes * 60 - 5
                    retained_mtime = now - retention_minutes * 60 + 5
                    os.utime(expired, (expired_mtime, expired_mtime))
                    os.utime(retained, (retained_mtime, retained_mtime))

                    self.assertEqual(store.cleanup_old_images(force=True), 1)
                    self.assertFalse(expired.exists())
                    self.assertTrue(retained.exists())


if __name__ == "__main__":
    unittest.main()
