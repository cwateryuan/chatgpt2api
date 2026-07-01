import copy
from contextlib import contextmanager
import json
import tempfile
import unittest
from pathlib import Path

from services.config import DEFAULT_PROXY_RUNTIME, ConfigStore


class FakeAppConfigStorage:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}
        self.lock_entries = 0

    def supports_database_features(self) -> bool:
        return True

    def load_app_config(self, key: str) -> dict[str, object] | None:
        item = self.items.get(key)
        return dict(item) if isinstance(item, dict) else None

    def save_app_config(self, key: str, data: dict[str, object]) -> None:
        self.items[key] = dict(data)

    @contextmanager
    def app_config_write_lock(self, key: str):
        self.lock_entries += 1
        yield


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

    def test_db_settings_are_seeded_from_config_file(self) -> None:
        tmp_dir, store = self._make_store({"image_poll_timeout_secs": 120})
        with tmp_dir:
            fake_db = FakeAppConfigStorage()
            store._storage_backend = fake_db
            store.reload_if_changed(force=True)

            self.assertEqual(fake_db.items["settings"]["image_poll_timeout_secs"], 120)

    def test_db_settings_override_recreated_config_file(self) -> None:
        tmp_dir, store = self._make_store({"image_poll_timeout_secs": 120, "log_levels": ["info"]})
        with tmp_dir:
            fake_db = FakeAppConfigStorage()
            fake_db.save_app_config("settings", {"image_poll_timeout_secs": 240, "log_levels": ["error"]})
            store._storage_backend = fake_db
            store.reload_if_changed(force=True)

            self.assertEqual(store.image_poll_timeout_secs, 240)
            self.assertEqual(store.log_levels, ["error"])

            store.path.write_text(json.dumps({"auth-key": "test-auth", "image_poll_timeout_secs": 60}), encoding="utf-8")
            store.reload_if_changed(force=True)

            self.assertEqual(store.image_poll_timeout_secs, 240)
            self.assertEqual(store.log_levels, ["error"])

    def test_multi_worker_db_update_merges_latest_settings(self) -> None:
        tmp_dir, worker_a = self._make_store({"image_poll_timeout_secs": 120, "log_levels": ["info"]})
        with tmp_dir:
            fake_db = FakeAppConfigStorage()
            worker_a._storage_backend = fake_db
            worker_a.reload_if_changed(force=True)
            worker_b = ConfigStore(worker_a.path)
            worker_b._storage_backend = fake_db
            worker_b.reload_if_changed(force=True)

            worker_a.update({"proxy_runtime": {"enabled": True, "egress_mode": "single_proxy", "proxy_url": "http://proxy"}})
            worker_b.update({"image_poll_timeout_secs": 300})

            saved = fake_db.load_app_config("settings") or {}
            self.assertEqual(saved["image_poll_timeout_secs"], 300)
            self.assertTrue(saved["proxy_runtime"]["enabled"])
            self.assertEqual(saved["proxy_runtime"]["proxy_url"], "http://proxy")
            self.assertGreaterEqual(fake_db.lock_entries, 2)


if __name__ == "__main__":
    unittest.main()
