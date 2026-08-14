from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.account_service import AccountService
from services.icloud_stats_service import ICloudStatsService
from services.storage.json_storage import JSONStorageBackend


class ICloudStatsServiceTests(unittest.TestCase):
    def test_backfill_archive_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "icloud_stats.json"
            service = ICloudStatsService(path)
            baseline = [
                {"email": "first@icloud.com"},
                {"email": "SECOND@ICLOUD.COM"},
                {"email": "other@example.com"},
            ]

            service.ensure_baseline(baseline)
            service.ensure_baseline([*baseline, {"email": "ignored@icloud.com"}])
            service.record_registered([{"email": "new@icloud.com"}, {"email": "other@example.com"}])
            service.record_deleted([
                {"email": "old@icloud.com", "success": 30, "rate_limit_429": 2},
                {"email": "other@example.com", "success": 99, "rate_limit_429": 99},
            ])

            snapshot = ICloudStatsService(path).snapshot([
                {"email": "live@icloud.com", "status": "正常", "success": 5, "rate_limit_429": 1},
                {"email": "limited@icloud.com", "status": "限流", "success": 2},
                {"email": "abnormal@icloud.com", "status": "异常", "success": 26},
            ])

            self.assertEqual(snapshot["registered_success_total"], 3)
            self.assertEqual(snapshot["current_accounts"], 2)
            self.assertEqual(snapshot["current_images"], 7)
            self.assertEqual(snapshot["deleted_accounts"], 1)
            self.assertEqual(snapshot["deleted_images"], 30)
            self.assertEqual(snapshot["over_25_accounts"], 2)
            self.assertEqual(snapshot["rate_limit_429_errors"], 3)

    def test_account_service_archives_deleted_icloud_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stats = ICloudStatsService(root / "icloud_stats.json")
            storage = JSONStorageBackend(root / "accounts.json")
            accounts = AccountService(storage, stats)
            accounts.initialize_icloud_stats()

            accounts.add_account_items([
                {
                    "access_token": "icloud-token",
                    "email": "generated@icloud.com",
                    "registration_engine": "http",
                    "status": "正常",
                    "success": 25,
                    "image_quota_unknown": True,
                }
            ])
            accounts.mark_image_result("icloud-token", True)
            accounts.mark_image_result("icloud-token", False, rate_limit_429=True)
            result = accounts.delete_accounts(["icloud-token"])
            snapshot = accounts.get_icloud_stats(result["items"])

            self.assertEqual(result["removed"], 1)
            self.assertEqual(snapshot["registered_success_total"], 1)
            self.assertEqual(snapshot["deleted_accounts"], 1)
            self.assertEqual(snapshot["deleted_images"], 26)
            self.assertEqual(snapshot["over_25_accounts"], 1)
            self.assertEqual(snapshot["rate_limit_429_errors"], 1)


if __name__ == "__main__":
    unittest.main()
