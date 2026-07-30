from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api
from services.account_service import AccountService
from services.openai_backend_api import OpenAIBackendAPI
from services.proxy_service import ClearanceBundle
from services.register import browser_register, openai_register
from services.register_service import RegisterService, _normalize
from services.storage.json_storage import JSONStorageBackend


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers: dict[str, str] = {}

    def close(self):
        return None


class RegistrationHardeningTests(unittest.TestCase):
    def test_browser_clearance_uses_matching_proxy_scope_and_safe_cookie_import(self):
        registrar = browser_register.BrowserRegistrar()
        bundle = ClearanceBundle(
            target_host="auth.openai.com",
            proxy_url="http://proxy.example:8080",
            cookies={"cf_clearance": "secret-clearance"},
            user_agent="Clearance UA",
        )
        profile = SimpleNamespace(
            clearance_enabled=True,
            clearance_mode="flaresolverr",
            proxy_source="runtime",
        )
        with (
            mock.patch.object(browser_register.proxy_settings, "get_profile", return_value=profile),
            mock.patch.object(browser_register.proxy_settings, "refresh_clearance", return_value=bundle) as refresh,
            mock.patch.object(browser_register, "step") as log_step,
        ):
            loaded = registrar._load_clearance(1)

        self.assertIs(loaded, bundle)
        self.assertEqual(refresh.call_args.kwargs["target_url"], browser_register.auth_base)
        self.assertTrue(refresh.call_args.kwargs["upstream"])
        self.assertTrue(refresh.call_args.kwargs["force"])
        self.assertIn(registrar.fingerprint["device_id"], refresh.call_args.kwargs["clearance_scope"])
        self.assertNotIn("secret-clearance", " ".join(str(call) for call in log_step.call_args_list))

        cookies = browser_register._browser_auth_cookies(bundle, registrar.fingerprint["device_id"])
        self.assertEqual({item["name"] for item in cookies}, {"cf_clearance", "oai-did"})
        self.assertTrue(all(item["url"].startswith("https://auth.openai.com") for item in cookies))
        linux_fp = browser_register._align_fingerprint_platform({"user_agent": "Chrome on Linux"})
        self.assertEqual(linux_fp["sec_ch_ua_platform"], '"Linux"')

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

    def test_browser_runtime_probe_runs_off_asyncio_thread(self):
        event_loop_thread = threading.get_ident()
        probe_threads: list[int] = []

        def fake_probe():
            probe_threads.append(threading.get_ident())
            return {"browser_available": True, "browser_version": "test", "browser_error": ""}

        async def check_status():
            return browser_register.browser_runtime_status(refresh=True)

        with mock.patch.object(browser_register, "_probe_browser_runtime", side_effect=fake_probe):
            status = asyncio.run(check_status())

        self.assertTrue(status["browser_available"])
        self.assertEqual(len(probe_threads), 1)
        self.assertNotEqual(probe_threads[0], event_loop_thread)

    def test_browser_authorization_state_machine_reaches_callback(self):
        status = browser_register.browser_runtime_status()
        if not status["browser_available"]:
            self.skipTest(status["browser_error"])
        from playwright.sync_api import sync_playwright

        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 30
        html = """
        <input name="email" type="email"><button onclick="showPassword()">Continue</button>
        <script>
        let passwordAttempts = 0;
        function showPassword() { document.body.innerHTML = '<input name="newPassword" type="password" autocomplete="new-password"><button onclick="submitPassword()">Continue</button>'; }
        function submitPassword() { passwordAttempts += 1; if (passwordAttempts === 1) { document.body.innerHTML = '<main><h1>Something went wrong</h1><p>private@example.com could not continue</p><button onclick="showPassword()">Try again</button>'; } else { setTimeout(showOtp, 1200); } }
        function showOtp() { document.body.innerHTML = '<input inputmode="numeric" maxlength="1"><input inputmode="numeric" maxlength="1"><input inputmode="numeric" maxlength="1"><input inputmode="numeric" maxlength="1"><input inputmode="numeric" maxlength="1"><input inputmode="numeric" maxlength="1"><button onclick="showProfile()">Verify</button>'; }
        function showProfile() { document.body.innerHTML = '<input name="name"><input name="birthdate" type="date"><button onclick="showConsent()">Continue</button>'; }
        function showConsent() { document.body.innerHTML = '<button onclick="finish()">Allow</button>'; }
        function finish() { location.hash = "done"; }
        </script>
        """

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                page.locator('input[name="email"]').fill("private@example.com")
                self.assertNotIn("private@example.com", registrar._control_summary(page))
                page.set_content('<input readonly type="text" placeholder="Email address" value="private@example.com">')
                self.assertIsNone(registrar._editable(page, ('input[placeholder*="Email" i]',)))
                page.set_content('<main><h1>Something went wrong</h1><p>private@example.com could not continue</p><button>Try again</button></main>')
                self.assertNotIn("private@example.com", registrar._error_summary(page))
                page.set_content(html)

                def capture_callback(url: str) -> None:
                    if str(url).endswith("#done"):
                        registrar.callback_code = "callback-code"

                registrar._capture_callback = capture_callback
                with mock.patch.object(browser_register.mail_provider, "wait_for_code", return_value="123456"):
                    password = registrar._run_authorization_flow(
                        page,
                        {"provider": "test"},
                        "user@example.com",
                        "Password123!",
                        "Jane Doe",
                        "2000-01-02",
                        1,
                    )

                otp_registrar = browser_register.BrowserRegistrar()
                otp_registrar._deadline = time.monotonic() + 30
                page.set_content("""
                    <input name="new-password" type="password" autocomplete="new-password">
                    <button onclick="document.body.dataset.passwordSubmitted='yes'">Continue</button>
                    <button id="signup-otp">Sign up with a one-time code</button>
                    <script>
                    setTimeout(() => document.querySelector('#signup-otp').addEventListener('click', showSignupOtp), 1200);
                    function showSignupOtp() { document.body.dataset.otpClicks = String(Number(document.body.dataset.otpClicks || '0') + 1); setTimeout(renderSignupOtp, 2500); }
                    function renderSignupOtp() { document.body.innerHTML = '<input name="otpCode" autocomplete="one-time-code"><button onclick="showSignupProfile()">Continue</button>'; }
                    function showSignupProfile() { document.body.innerHTML = '<input name="name"><input name="birthdate" type="date"><button onclick="showSignupConsent()">Continue</button>'; }
                    function showSignupConsent() { document.body.innerHTML = '<button onclick="finishSignup()">Allow</button>'; }
                    function finishSignup() { location.hash = 'otp-signup-done'; }
                    </script>
                """)

                def capture_otp_signup(url: str) -> None:
                    if str(url).endswith("#otp-signup-done"):
                        otp_registrar.callback_code = "otp-signup-code"

                otp_registrar._capture_callback = capture_otp_signup
                with mock.patch.object(browser_register.mail_provider, "wait_for_code", return_value="246810"):
                    otp_password = otp_registrar._run_authorization_flow(
                        page,
                        {"provider": "outlook_token"},
                        "otp-user@example.com",
                        "UnusedPassword123!",
                        "Jane Doe",
                        "2000-01-02",
                        1,
                    )
                self.assertEqual(otp_password, "")
                self.assertEqual(otp_registrar.callback_code, "otp-signup-code")
                self.assertNotEqual(page.locator("body").get_attribute("data-password-submitted"), "yes")
                self.assertEqual(page.locator("body").get_attribute("data-otp-clicks"), "1")

                direct_otp_registrar = browser_register.BrowserRegistrar()
                direct_otp_registrar._deadline = time.monotonic() + 30
                page.set_content("""
                    <input name="code" autocomplete="one-time-code"><button onclick="showDirectProfile()">Continue</button>
                    <script>
                    function showDirectProfile() { document.body.innerHTML = '<input name="name"><input name="birthdate" type="date"><button onclick="finishDirectSignup()">Continue</button>'; }
                    function finishDirectSignup() { location.hash = 'direct-otp-signup-done'; }
                    </script>
                """)

                def capture_direct_otp_signup(url: str) -> None:
                    if str(url).endswith("#direct-otp-signup-done"):
                        direct_otp_registrar.callback_code = "direct-otp-signup-code"

                direct_otp_registrar._capture_callback = capture_direct_otp_signup
                with mock.patch.object(browser_register.mail_provider, "wait_for_code", return_value="135790"):
                    direct_otp_password = direct_otp_registrar._run_authorization_flow(
                        page,
                        {"provider": "outlook_token"},
                        "direct-otp@example.com",
                        "UnusedPassword123!",
                        "Jane Doe",
                        "2000-01-02",
                        1,
                    )
                self.assertEqual(direct_otp_password, "")

                page.set_content("""
                    <input name="current-password" type="password" autocomplete="current-password webauthn">
                    <button onclick="showLoginOtp()">Log in with a one-time code</button>
                    <script>
                    function showLoginOtp() { document.body.innerHTML = '<input name="otpCode" autocomplete="one-time-code"><button onclick="finishLogin()">Continue</button>'; }
                    function finishLogin() { document.body.dataset.done = 'yes'; }
                    </script>
                """)
                with mock.patch.object(browser_register.mail_provider, "wait_for_code", return_value="654321"):
                    registrar._handle_existing_outlook(page, {"provider": "outlook_token"}, 1)
                self.assertEqual(page.locator("body").get_attribute("data-done"), "yes")
            finally:
                browser.close()

        self.assertEqual(password, "Password123!")
        self.assertEqual(registrar.callback_code, "callback-code")

    def test_browser_authorization_restarts_transition_with_same_email(self):
        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 30
        page = mock.Mock()
        page.url = "https://auth.openai.com/create-account/password"
        stalled = browser_register.BrowserRegistrationError(
            "browser_one_time_code_transition_timeout:path=/create-account/password"
        )

        with (
            mock.patch.object(registrar, "_run_authorization_flow", side_effect=[stalled, ""]) as run_flow,
            mock.patch.object(registrar, "_registration_url", return_value="https://chatgpt.com/auth/login") as registration_url,
            mock.patch.object(browser_register, "step") as log_step,
        ):
            password = registrar._run_authorization_with_restarts(
                page,
                {"provider": "outlook_token"},
                "same@example.com",
                "UnusedPassword123!",
                "Jane Doe",
                "2000-01-02",
                1,
            )

        self.assertEqual(password, "")
        self.assertEqual(run_flow.call_count, 2)
        self.assertEqual([call.args[2] for call in run_flow.call_args_list], ["same@example.com"] * 2)
        registration_url.assert_called_once_with()
        page.goto.assert_called_once_with(
            "https://chatgpt.com/auth/login",
            wait_until="domcontentloaded",
            timeout=mock.ANY,
        )
        self.assertIn("重新提交同一邮箱", " ".join(str(call) for call in log_step.call_args_list))

    def test_browser_chatgpt_session_token_is_read_after_registration(self):
        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 30
        page = mock.Mock()
        page.evaluate.side_effect = [
            {"status": 200, "data": {"user": {"email": "same@example.com"}}},
            {"status": 200, "data": {"accessToken": "session-access-token"}},
        ]

        with mock.patch.object(browser_register, "step"):
            tokens = registrar._chatgpt_session_tokens(page, 1)

        self.assertEqual(tokens["access_token"], "session-access-token")
        self.assertEqual(tokens["refresh_token"], "")
        self.assertEqual(page.evaluate.call_count, 2)
        page.wait_for_timeout.assert_called_once_with(1_000)

    def test_browser_registration_uses_chatgpt_login_entry(self):
        registrar = browser_register.BrowserRegistrar()
        self.assertEqual(registrar._registration_url(), "https://chatgpt.com/auth/login")
        page = mock.Mock()
        page.url = "https://chatgpt.com/"
        self.assertTrue(registrar._registration_complete(page))
        page.url = "https://chatgpt.com/auth/login"
        self.assertFalse(registrar._registration_complete(page))
        page.url = "https://chatgpt.com/api/auth/callback/openai"
        self.assertFalse(registrar._registration_complete(page))

    def test_browser_devtools_registration_follows_reference_retry_flow(self):
        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 120
        process = mock.Mock()
        code_state = {
            "url": "https://auth.openai.com/email-verification",
            "inputs": [{"visible": True, "name": "code", "autocomplete": "one-time-code", "maxLength": 6}],
            "buttons": [{"type": "submit", "text": "Continue"}],
        }
        profile_state = {
            "url": "https://auth.openai.com/about-you",
            "body": "Full name Birthday",
            "inputs": [{"visible": True, "name": "name"}, {"visible": True, "name": "birthdate"}],
            "buttons": [{"type": "submit", "text": "Continue"}],
        }
        logged_in_state = {"url": "https://chatgpt.com/", "inputs": [], "buttons": []}

        with (
            mock.patch.object(registrar, "_launch_devtools_browser", return_value=(process, 12345)),
            mock.patch.object(registrar, "_wait_for_otp", return_value="246810") as wait_for_otp,
            mock.patch.object(browser_register.browser_devtools, "wait_for", return_value={"url": "https://chatgpt.com/auth/login"}),
            mock.patch.object(
                browser_register.browser_devtools,
                "submit_until",
                side_effect=[code_state, profile_state, logged_in_state],
            ) as submit_until,
            mock.patch.object(
                browser_register.browser_devtools,
                "evaluate_json",
                return_value={"userAgent": "Mozilla/5.0 Chrome/136.0.0.0"},
            ),
            mock.patch.object(
                browser_register.browser_devtools,
                "response_body_for_request",
                return_value='{"accessToken":"session-token"}',
            ),
            mock.patch.object(browser_register.browser_devtools, "close_browser") as close_browser,
            mock.patch.object(browser_register, "step"),
        ):
            tokens = registrar._run_devtools_registration(
                {"provider": "outlook_token"},
                "same@example.com",
                "Jane Doe",
                "2000-01-02",
                1,
            )

        self.assertEqual(tokens["access_token"], "session-token")
        self.assertEqual(submit_until.call_count, 3)
        wait_for_otp.assert_called_once()
        close_browser.assert_called_once_with(12345, process)
        self.assertIn("Chrome/136", registrar.fingerprint["user_agent"])

    def test_browser_devtools_actions_use_native_input_events(self):
        email_script = browser_register._devtools_submit_email_js("same@example.com")
        code_script = browser_register._devtools_submit_code_js("246810")
        self.assertIn("InputEvent", email_script)
        self.assertIn("descriptor.set.call", email_script)
        self.assertIn("InputEvent", code_script)
        self.assertIn("code_submit_missing", code_script)

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
