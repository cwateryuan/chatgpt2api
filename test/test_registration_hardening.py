from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api
from services.account_service import AccountService
from services.openai_backend_api import OpenAIBackendAPI
from services.register import openai_register
from services.register_service import RegisterService, _normalize
from services.storage.json_storage import JSONStorageBackend


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers: dict[str, str] = {}

    def close(self):
        return None


class RegistrationHardeningTests(unittest.TestCase):
    def test_existing_account_fingerprint_is_generated_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            service = AccountService(JSONStorageBackend(path))
            service.add_account_items([{
                "access_token": "token",
                "fp": {"user_agent": "Custom UA", "custom_marker": "keep"},
            }])

            first = service.get_or_create_fingerprint("token")
            second = service.get_or_create_fingerprint("token")
            reloaded = AccountService(JSONStorageBackend(path)).get_account("token")

        self.assertEqual(first, second)
        self.assertEqual(first["user_agent"], "Custom UA")
        self.assertEqual(first["custom_marker"], "keep")
        self.assertEqual(reloaded["fp"]["device_id"], first["device_id"])
        self.assertEqual(reloaded["fp"]["session_id"], first["session_id"])

    def test_backend_uses_persisted_fingerprint_for_tls_and_headers(self):
        fp = {
            "user_agent": "Stable UA",
            "impersonate": "chrome136",
            "accept_language": "en-US,en;q=0.9",
            "device_id": "device-1",
            "session_id": "session-1",
            "sec_ch_ua": '"Chromium";v="136"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        }
        with (
            mock.patch("services.openai_backend_api.account_service.get_account", return_value={"fp": fp}),
            mock.patch("services.openai_backend_api.account_service.get_or_create_fingerprint", return_value=fp),
            mock.patch("services.openai_backend_api.proxy_settings.build_session_kwargs", return_value={}) as build,
            mock.patch("services.openai_backend_api.requests.Session", FakeSession),
        ):
            backend = OpenAIBackendAPI("token")

        self.assertEqual(build.call_args.kwargs["impersonate"], "chrome136")
        self.assertEqual(backend.session.headers["User-Agent"], "Stable UA")
        self.assertEqual(backend.session.headers["OAI-Device-Id"], "device-1")
        self.assertNotIn("Sec-Ch-Ua-Full-Version-List", backend.session.headers)

    def test_oauth_refresh_persists_and_reuses_account_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AccountService(JSONStorageBackend(Path(tmp) / "accounts.json"))
            service.add_account_items([{"access_token": "token", "refresh_token": "refresh"}])
            with mock.patch.object(
                service,
                "_request_access_token_refresh",
                return_value={"access_token": "token", "refresh_token": "refresh", "id_token": ""},
            ) as request_refresh:
                service.refresh_access_token("token", force=True)

            fp = service.get_account("token")["fp"]
            request_fp = request_refresh.call_args.args[1]["fp"]

        self.assertEqual(request_fp, fp)
        self.assertTrue(fp["device_id"])
        self.assertTrue(fp["session_id"])

    def test_browser_engine_normalizes_to_single_effective_thread(self):
        config = _normalize({"engine": "browser", "threads": 9, "stats": {"threads": 9}})
        self.assertEqual(config["threads"], 9)
        self.assertEqual(config["stats"]["threads"], 1)

    def test_browser_start_fails_when_runtime_is_unavailable(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("services.register_service._db_backend", return_value=None),
            mock.patch(
                "services.register_service.browser_register.browser_runtime_status",
                return_value={"browser_available": False, "browser_version": "", "browser_error": "missing"},
            ),
        ):
            service = RegisterService(Path(tmp) / "register.json")
            service.update({"engine": "browser"})
            with self.assertRaisesRegex(RuntimeError, "browser_runtime_unavailable"):
                service.start()

    def test_browser_unavailable_start_returns_http_409(self):
        app = FastAPI()
        app.include_router(register_api.create_router())
        with (
            mock.patch.object(register_api, "require_admin"),
            mock.patch.object(
                register_api.register_service,
                "start",
                side_effect=RuntimeError("browser_runtime_unavailable"),
            ),
        ):
            response = TestClient(app).post("/api/register/start")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "browser_runtime_unavailable")

    def test_http_worker_saves_unknown_quota_without_full_refresh(self):
        result = {
            "email": "user@example.com",
            "access_token": "token",
            "refresh_token": "refresh",
            "fp": {"device_id": "device", "session_id": "session"},
        }
        registrar = mock.Mock()
        registrar.register.return_value = dict(result)
        old_mode = openai_register.config.get("mode")
        openai_register.config["mode"] = "total"
        try:
            with (
                mock.patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
                mock.patch.object(openai_register.account_service, "add_account_items") as add,
                mock.patch.object(openai_register.account_service, "refresh_accounts") as refresh,
            ):
                outcome = openai_register.worker(1)
        finally:
            openai_register.config["mode"] = old_mode

        self.assertTrue(outcome["ok"])
        saved = add.call_args.args[0][0]
        self.assertTrue(saved["image_quota_unknown"])
        self.assertEqual(saved["status"], "正常")
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
