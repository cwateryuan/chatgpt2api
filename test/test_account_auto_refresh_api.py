from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import accounts


class AccountAutoRefreshApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts.create_router())
        self.client = TestClient(app)

    def test_get_returns_current_setting_and_interval(self) -> None:
        fake_config = mock.Mock(full_account_refresh_enabled=True)
        with (
            mock.patch.object(accounts, "require_admin"),
            mock.patch.object(accounts, "config", fake_config),
        ):
            response = self.client.get("/api/accounts/auto-refresh")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": True, "interval_seconds": 60})

    def test_post_updates_only_auto_refresh_setting(self) -> None:
        fake_config = mock.Mock()
        fake_config.full_account_refresh_enabled = False
        with (
            mock.patch.object(accounts, "require_admin"),
            mock.patch.object(accounts, "config", fake_config),
        ):
            response = self.client.post("/api/accounts/auto-refresh", json={"enabled": False})

        self.assertEqual(response.status_code, 200)
        fake_config.update.assert_called_once_with({"full_account_refresh_enabled": False})
        self.assertEqual(response.json(), {"enabled": False, "interval_seconds": 60})

    def test_endpoints_require_admin(self) -> None:
        def reject(_authorization):
            raise HTTPException(status_code=403, detail="admin required")

        with mock.patch.object(accounts, "require_admin", side_effect=reject):
            get_response = self.client.get("/api/accounts/auto-refresh")
            post_response = self.client.post("/api/accounts/auto-refresh", json={"enabled": False})

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
