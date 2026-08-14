from __future__ import annotations

import unittest

from services.storage.database_storage import DatabaseStorageBackend


class ImagePoolMetricsTests(unittest.TestCase):
    def test_database_aggregate_excludes_unavailable_accounts(self) -> None:
        storage = DatabaseStorageBackend("sqlite://")
        try:
            accounts = [
                {"access_token": "normal-five", "status": "正常", "quota": 5},
                {"access_token": "normal-seven", "quota": 7},
                {"access_token": "normal-empty", "status": "正常", "quota": 0},
                {
                    "access_token": "normal-unknown",
                    "status": "正常",
                    "quota": 100,
                    "image_quota_unknown": True,
                },
                {"access_token": "limited", "status": "限流", "quota": 99},
                {
                    "access_token": "abnormal-unknown",
                    "status": "异常",
                    "image_quota_unknown": True,
                },
                {"access_token": "disabled", "status": "禁用", "quota": 88},
            ]
            for account in accounts:
                storage.upsert_account(account)

            self.assertEqual(
                storage.get_image_pool_metrics(),
                {"current_available": 3, "current_quota": 12},
            )
        finally:
            storage.engine.dispose()


if __name__ == "__main__":
    unittest.main()
