from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api
from services.account_service import AccountService
from services.openai_backend_api import OpenAIBackendAPI
from services.proxy_service import ClearanceBundle, normalize_proxy_url
from services.register import browser_register, openai_register
from services.register_service import RegisterService, _normalize
from services.storage.json_storage import JSONStorageBackend


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers: dict[str, str] = {}
        self.cookies = mock.Mock()

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
                "fp": {
                    "user_agent": "Custom UA",
                    "custom_marker": "keep",
                    "timezone": "Europe/Berlin",
                    "latitude": "50.11",
                    "longitude": "8.68",
                },
            }])

            first = service.get_or_create_fingerprint("token")
            second = service.get_or_create_fingerprint("token")
            reloaded = AccountService(JSONStorageBackend(path)).get_account("token")

        self.assertEqual(first, second)
        self.assertEqual(first["user_agent"], "Custom UA")
        self.assertEqual(first["custom_marker"], "keep")
        self.assertEqual(reloaded["fp"]["timezone"], "Europe/Berlin")
        self.assertEqual(reloaded["fp"]["latitude"], "50.11")
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
            "language": "en-US",
            "timezone": "UTC",
            "timezone_offset_min": "0",
            "screen_width": "1365",
            "screen_height": "768",
            "page_width": "1280",
            "page_height": "681",
            "pixel_ratio": "1",
        }
        account = {"fp": fp, "proxy": "http://privoxy:8118"}
        with (
            mock.patch("services.openai_backend_api.account_service.get_account", return_value=account),
            mock.patch("services.openai_backend_api.account_service.get_or_create_fingerprint", return_value=fp),
            mock.patch("services.openai_backend_api.proxy_settings.build_session_kwargs", return_value={}) as build,
            mock.patch("services.openai_backend_api.requests.Session", FakeSession),
        ):
            backend = OpenAIBackendAPI("token")

        self.assertEqual(build.call_args.kwargs["impersonate"], "chrome136")
        self.assertEqual(backend.session.headers["User-Agent"], "Stable UA")
        self.assertEqual(backend.session.headers["OAI-Device-Id"], "device-1")
        self.assertEqual(backend.session.headers["OAI-Language"], "en-US")
        self.assertNotIn("Sec-Ch-Ua-Full-Version-List", backend.session.headers)
        self.assertEqual(build.call_args.kwargs["account"]["proxy"], "http://privoxy:8118")
        backend.session.cookies.set.assert_called_once_with("oai-did", "device-1", domain=".chatgpt.com", path="/")
        payload = backend._conversation_payload([], "auto", "Asia/Shanghai")
        self.assertEqual(payload["timezone"], "UTC")
        self.assertEqual(payload["timezone_offset_min"], 0)
        self.assertEqual(payload["client_contextual_info"]["screen_width"], 1365)
        self.assertEqual(payload["client_contextual_info"]["page_height"], 681)

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

    def test_browser_engine_preserves_configured_threads(self):
        config = _normalize({"engine": "browser", "threads": 9, "stats": {"threads": 9}})
        self.assertEqual(config["threads"], 9)
        self.assertEqual(config["stats"]["threads"], 9)

    def test_registration_proxy_pool_is_normalized_and_round_robined(self):
        raw = "  http://proxy-a:8080\r\n\nhttp://proxy-b:8080\nhttp://proxy-a:8080  "
        config = _normalize({"proxy": raw})
        self.assertEqual(config["proxy"], "http://proxy-a:8080\nhttp://proxy-b:8080")
        self.assertEqual(openai_register.registration_proxy_pool(config["proxy"]), [
            "http://proxy-a:8080",
            "http://proxy-b:8080",
        ])
        self.assertEqual(openai_register.select_registration_proxy(config["proxy"], 1), "http://proxy-a:8080")
        self.assertEqual(openai_register.select_registration_proxy(config["proxy"], 2), "http://proxy-b:8080")
        self.assertEqual(openai_register.select_registration_proxy(config["proxy"], 3), "http://proxy-a:8080")

    def test_browser_token_mode_defaults_to_session_and_accepts_oauth(self):
        self.assertEqual(_normalize({"engine": "browser"})["browser_token_mode"], "session")
        self.assertEqual(_normalize({"engine": "browser", "browser_token_mode": "oauth"})["browser_token_mode"], "oauth")
        self.assertEqual(_normalize({"engine": "browser", "browser_token_mode": "invalid"})["browser_token_mode"], "session")

    def test_browser_runner_uses_configured_thread_pool_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = RegisterService(Path(tmp) / "register.json")
            service._config = _normalize({"engine": "browser", "threads": 4, "enabled": False})
            with (
                mock.patch("services.register_service.ThreadPoolExecutor") as executor,
                mock.patch.object(service, "_refresh_runtime_cfg_for_runner", return_value=service._config),
                mock.patch.object(service, "_bump"),
                mock.patch.object(service, "_save"),
                mock.patch.object(service, "_save_runtime"),
                mock.patch.object(service, "_stop_requested", return_value=False),
                mock.patch.object(service, "_clear_stop_requested"),
                mock.patch.object(service, "_append_log"),
                mock.patch("services.register_service.trim_memory"),
            ):
                service._run()

        executor.assert_called_once_with(max_workers=4)

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
        registrar.registration_proxy_username = "clip-user"
        registrar.registration_proxy_password = "clip-password"
        registrar.geo_environment = {
            "timezone": "Europe/Berlin",
            "language": "de-DE",
            "accept_language": "de-DE,de;q=0.9,en;q=0.8",
            "latitude": 50.11,
            "longitude": 8.68,
        }
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
            mock.patch.object(browser_register.browser_devtools, "ProxyAuthHandler") as proxy_auth,
            mock.patch.object(
                browser_register.browser_devtools,
                "apply_environment_overrides",
                return_value={"timezone": True, "locale": True, "geolocation": True},
            ) as apply_environment,
            mock.patch.object(browser_register.browser_devtools, "navigate_to") as navigate_to,
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
                return_value={
                    "user_agent": "Mozilla/5.0 Chrome/136.0.0.0",
                    "platform": "Linux",
                    "mobile": False,
                    "brands": [{"brand": "Chromium", "version": "136"}],
                    "languages": ["en-US", "en"],
                    "language": "en-US",
                    "timezone": "UTC",
                    "timezone_offset_min": 0,
                    "screen_width": 1365,
                    "screen_height": 768,
                    "page_width": 1365,
                    "page_height": 681,
                    "pixel_ratio": 1,
                },
            ),
            mock.patch.object(
                browser_register.browser_devtools,
                "get_all_cookies",
                return_value=[{"name": "oai-did", "value": "browser-device-id"}],
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
        self.assertEqual(tokens["registration_token_mode"], "session")
        proxy_auth.assert_called_once_with(12345, "clip-user", "clip-password")
        proxy_auth.return_value.start.assert_called_once()
        proxy_auth.return_value.close.assert_called_once()
        apply_environment.assert_called_once_with(
            12345,
            timezone_id="Europe/Berlin",
            locale="de-DE",
            accept_language="de-DE,de;q=0.9,en;q=0.8",
            latitude=50.11,
            longitude=8.68,
            timeout=mock.ANY,
        )
        navigate_to.assert_called_once_with(12345, browser_register.CHATGPT_LOGIN_URL, mock.ANY)
        self.assertEqual(submit_until.call_count, 3)
        wait_for_otp.assert_called_once()
        close_browser.assert_called_once_with(12345, process)
        self.assertIn("Chrome/136", registrar.fingerprint["user_agent"])
        self.assertEqual(registrar.fingerprint["device_id"], "browser-device-id")
        self.assertEqual(registrar.fingerprint["timezone"], "UTC")
        self.assertEqual(registrar.fingerprint["screen_width"], "1365")

    def test_browser_oauth_authorize_reuses_login_state_and_exchanges_callback(self):
        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 30
        callback = "https://platform.openai.com/auth/callback?code=callback-code&state=expected-state"
        tokens = {"access_token": "oauth-access", "refresh_token": "oauth-refresh", "id_token": "oauth-id"}

        with (
            mock.patch.object(browser_register, "generate_pkce", return_value=("verifier", "challenge")),
            mock.patch.object(browser_register.secrets, "token_urlsafe", side_effect=["expected-state", "nonce"]),
            mock.patch.object(browser_register.browser_devtools, "navigate_to") as navigate,
            mock.patch.object(registrar, "_devtools_state", return_value={"url": callback, "inputs": [], "buttons": []}),
            mock.patch.object(registrar, "_exchange_browser_oauth_code", return_value=tokens) as exchange,
            mock.patch.object(browser_register, "step"),
        ):
            result = registrar._devtools_platform_oauth_tokens(
                12345,
                {"provider": "outlook_token"},
                "same@example.com",
                1,
            )

        authorize_url = navigate.call_args.args[1]
        params = parse_qs(urlparse(authorize_url).query)
        self.assertNotIn("max_age", params)
        self.assertIn("offline_access", params["scope"][0])
        self.assertEqual(params["state"], ["expected-state"])
        exchange.assert_called_once_with("callback-code", "verifier")
        self.assertEqual(result, tokens)

    def test_browser_oauth_callback_state_mismatch_is_rejected(self):
        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 30
        callback = "https://platform.openai.com/auth/callback?code=callback-code&state=wrong-state"
        with (
            mock.patch.object(browser_register, "generate_pkce", return_value=("verifier", "challenge")),
            mock.patch.object(browser_register.secrets, "token_urlsafe", side_effect=["expected-state", "nonce"]),
            mock.patch.object(browser_register.browser_devtools, "navigate_to"),
            mock.patch.object(registrar, "_devtools_state", return_value={"url": callback, "inputs": [], "buttons": []}),
            mock.patch.object(browser_register, "step"),
        ):
            with self.assertRaisesRegex(browser_register.BrowserRegistrationError, "browser_oauth_state_mismatch"):
                registrar._devtools_platform_oauth_tokens(12345, {}, "same@example.com", 1)

    def test_browser_oauth_exchange_uses_registration_proxy(self):
        registrar = browser_register.BrowserRegistrar()
        registrar.registration_proxy_url = "http://user:password@proxy.example:8080"
        registrar.fingerprint["impersonate"] = "chrome136"
        session = mock.Mock()
        tokens = {"access_token": "oauth-access", "refresh_token": "oauth-refresh", "id_token": "oauth-id"}
        with (
            mock.patch.object(browser_register.proxy_settings, "build_session_kwargs", return_value={"proxy": registrar.registration_proxy_url}) as build,
            mock.patch.object(browser_register.requests, "Session", return_value=session),
            mock.patch.object(browser_register, "request_platform_oauth_token", return_value=tokens),
        ):
            result = registrar._exchange_browser_oauth_code("callback-code", "verifier")

        self.assertEqual(result, tokens)
        self.assertEqual(build.call_args.kwargs["proxy"], registrar.registration_proxy_url)
        self.assertFalse(build.call_args.kwargs["upstream"])
        self.assertEqual(build.call_args.kwargs["impersonate"], "chrome136")
        session.close.assert_called_once()

    def test_browser_oauth_mode_marks_success_and_session_fallback(self):
        registrar = browser_register.BrowserRegistrar()
        oauth_tokens = {"access_token": "oauth-access", "refresh_token": "oauth-refresh", "id_token": "oauth-id"}
        with (
            mock.patch.dict(browser_register.config, {"browser_token_mode": "oauth"}),
            mock.patch.object(registrar, "_devtools_access_token", return_value="session-access"),
            mock.patch.object(registrar, "_devtools_platform_oauth_tokens", return_value=oauth_tokens),
            mock.patch.object(browser_register, "step"),
        ):
            result = registrar._devtools_registration_tokens(12345, {}, "same@example.com", 1)
        self.assertEqual(result["registration_token_mode"], "oauth")
        self.assertEqual(result["refresh_token"], "oauth-refresh")

        with (
            mock.patch.dict(browser_register.config, {"browser_token_mode": "oauth"}),
            mock.patch.object(registrar, "_devtools_access_token", return_value="session-access"),
            mock.patch.object(
                registrar,
                "_devtools_platform_oauth_tokens",
                side_effect=browser_register.BrowserRegistrationError("oauth blocked"),
            ),
            mock.patch.object(browser_register, "step") as log_step,
        ):
            result = registrar._devtools_registration_tokens(12345, {}, "same@example.com", 1)
        self.assertEqual(result["registration_token_mode"], "session_fallback")
        self.assertEqual(result["access_token"], "session-access")
        self.assertIn("使用 session token", " ".join(str(call) for call in log_step.call_args_list))

    def test_browser_oauth_and_session_failure_rejects_account(self):
        registrar = browser_register.BrowserRegistrar()
        with (
            mock.patch.dict(browser_register.config, {"browser_token_mode": "oauth"}),
            mock.patch.object(registrar, "_devtools_access_token", side_effect=RuntimeError("session unavailable")),
            mock.patch.object(registrar, "_devtools_platform_oauth_tokens", side_effect=RuntimeError("oauth unavailable")),
            mock.patch.object(browser_register, "step"),
        ):
            with self.assertRaisesRegex(browser_register.BrowserRegistrationError, "browser_oauth_and_session_failed"):
                registrar._devtools_registration_tokens(12345, {}, "same@example.com", 1)

    def test_browser_oauth_failure_log_redacts_proxy_credentials(self):
        message = browser_register._sanitized_error(
            RuntimeError("connect failed via http://proxy-user:proxy-password@proxy.example:8080")
        )
        self.assertNotIn("proxy-user", message)
        self.assertNotIn("proxy-password", message)
        self.assertIn("http://***:***@proxy.example:8080", message)

    def test_browser_devtools_actions_use_native_input_events(self):
        email_script = browser_register._devtools_submit_email_js("same@example.com")
        code_script = browser_register._devtools_submit_code_js("246810")
        profile_script = browser_register._devtools_submit_profile_js("Jane Doe", "2000-01-02")
        self.assertIn("InputEvent", email_script)
        self.assertIn("descriptor.set.call", email_script)
        self.assertIn("InputEvent", code_script)
        self.assertIn("code_submit_missing", code_script)
        self.assertIn('element.name === "age"', profile_script)
        self.assertIn("setValue(ageInput, age)", profile_script)
        self.assertIn("finish creating account", profile_script)

    def test_proxy_geography_normalizes_locale_and_uses_cache(self):
        geo = browser_register._normalize_geo_payload({
            "ip": "198.51.100.10",
            "country_code": "DE",
            "city": "Frankfurt",
            "timezone": "Europe/Berlin",
            "latitude": 50.11,
            "longitude": 8.68,
            "languages": "de-DE,de,en",
        })
        self.assertEqual(geo["language"], "de-DE")
        self.assertEqual(geo["languages"], ["de-DE", "de", "en"])
        self.assertEqual(geo["accept_language"], "de-DE,de;q=0.9,en;q=0.8")

        browser_register._geo_cache.clear()
        browser_register._geo_inflight.clear()
        with mock.patch.object(browser_register, "_query_proxy_geography", return_value=geo) as query:
            first = browser_register._proxy_geography("http://proxy.example:8080", {"impersonate": "chrome"})
            second = browser_register._proxy_geography("http://proxy.example:8080", {"impersonate": "chrome"})
        self.assertEqual(first, geo)
        self.assertEqual(second, geo)
        query.assert_called_once()

    def test_proxy_geography_failure_does_not_abort_registration(self):
        registrar = browser_register.BrowserRegistrar()
        with (
            mock.patch.object(
                browser_register,
                "_proxy_geography",
                side_effect=RuntimeError("failed via http://proxy-user:proxy-password@proxy.example:8080"),
            ),
            mock.patch.object(browser_register, "step") as log_step,
        ):
            registrar._load_geo_environment("http://proxy.example:8080", 1)
        self.assertEqual(registrar.geo_environment, {})
        message = " ".join(str(call) for call in log_step.call_args_list)
        self.assertNotIn("proxy-user", message)
        self.assertNotIn("proxy-password", message)

    def test_devtools_environment_overrides_apply_all_supported_values(self):
        devtools = mock.Mock()
        context = mock.MagicMock()
        context.__enter__.return_value = devtools
        with (
            mock.patch.object(browser_register.browser_devtools, "page_websocket", return_value="ws://browser"),
            mock.patch.object(browser_register.browser_devtools, "DevToolsSocket", return_value=context),
        ):
            applied = browser_register.browser_devtools.apply_environment_overrides(
                12345,
                timezone_id="Europe/Berlin",
                locale="de-DE",
                accept_language="de-DE,de;q=0.9,en;q=0.8",
                latitude=50.11,
                longitude=8.68,
                timeout=5,
            )

        self.assertEqual(applied, {"timezone": True, "locale": True, "geolocation": True})
        methods = [call.args[0] for call in devtools.call.call_args_list]
        self.assertEqual(methods, [
            "Emulation.setTimezoneOverride",
            "Emulation.setLocaleOverride",
            "Network.enable",
            "Network.setExtraHTTPHeaders",
            "Emulation.setGeolocationOverride",
        ])

    def test_browser_devtools_launch_requires_and_applies_registration_proxy(self):
        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 30
        process = mock.Mock()
        proxy_profile = SimpleNamespace(
            proxy_url="http://clip-user:clip-password@us.cliproxy.io:3010",
            proxy_source="explicit",
        )
        geo = {
            "ip": "198.51.100.10",
            "country": "DE",
            "city": "Frankfurt",
            "timezone": "Europe/Berlin",
            "latitude": 50.11,
            "longitude": 8.68,
            "language": "de-DE",
            "languages": ["de-DE", "de", "en"],
            "accept_language": "de-DE,de;q=0.9,en;q=0.8",
        }

        with (
            mock.patch.object(browser_register.browser_devtools, "find_browser_executable", return_value="chromium"),
            mock.patch.object(browser_register.browser_devtools, "free_local_port", return_value=12345),
            mock.patch.object(browser_register.browser_devtools, "wait_for_devtools"),
            mock.patch.object(browser_register.proxy_settings, "get_profile", return_value=proxy_profile) as get_profile,
            mock.patch.object(browser_register, "_proxy_geography", return_value=geo),
            mock.patch.object(browser_register.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(browser_register, "step"),
        ):
            launched_process, port = registrar._launch_devtools_browser(Path("profile"), 1)

        self.assertIs(launched_process, process)
        self.assertEqual(port, 12345)
        command = popen.call_args.args[0]
        self.assertIn("--proxy-server=http://us.cliproxy.io:3010", command)
        self.assertIn("--lang=de-DE", command)
        self.assertNotIn("clip-password", " ".join(command))
        self.assertEqual(command[-1], "about:blank")
        self.assertEqual(registrar.registration_proxy_username, "clip-user")
        self.assertEqual(registrar.registration_proxy_password, "clip-password")
        self.assertEqual(registrar.fingerprint["timezone"], "Europe/Berlin")
        self.assertEqual(registrar.fingerprint["latitude"], "50.11")
        get_profile.assert_called_once_with(proxy=mock.ANY, upstream=False)

        missing_profile = SimpleNamespace(proxy_url="", proxy_source="none")
        with (
            mock.patch.object(browser_register.browser_devtools, "find_browser_executable", return_value="chromium"),
            mock.patch.object(browser_register.browser_devtools, "free_local_port", return_value=12345),
            mock.patch.object(browser_register.proxy_settings, "get_profile", return_value=missing_profile),
        ):
            with self.assertRaisesRegex(browser_register.BrowserRegistrationError, "browser_registration_proxy_missing"):
                registrar._launch_devtools_browser(Path("profile"), 1)

    def test_browser_proxy_formats_and_proxy_auth_challenge(self):
        url = normalize_proxy_url("us.cliproxy.io:3010:clip-user:clip-password")
        self.assertEqual(
            browser_register.browser_devtools.browser_proxy_config(url),
            ("http://us.cliproxy.io:3010", "clip-user", "clip-password"),
        )
        self.assertEqual(
            browser_register.browser_devtools.browser_proxy_config(
                "http://clip-user:clip-password@us.cliproxy.io:3010"
            ),
            ("http://us.cliproxy.io:3010", "clip-user", "clip-password"),
        )
        with self.assertRaisesRegex(RuntimeError, "browser_authenticated_socks5_unsupported"):
            browser_register.browser_devtools.browser_proxy_config(
                "socks5h://clip-user:clip-password@us.cliproxy.io:3010"
            )
        with self.assertRaisesRegex(RuntimeError, "browser_proxy_url_invalid"):
            browser_register.browser_devtools.browser_proxy_config("http://proxy.example:not-a-port")

        handler = browser_register.browser_devtools.ProxyAuthHandler(12345, "clip-user", "clip-password")
        devtools = mock.Mock()
        messages = iter([
            {
                "method": "Fetch.authRequired",
                "params": {
                    "requestId": "proxy-request",
                    "authChallenge": {"source": "Proxy"},
                },
            },
            {
                "method": "Fetch.authRequired",
                "params": {
                    "requestId": "server-request",
                    "authChallenge": {"source": "Server"},
                },
            },
        ])

        def receive_event():
            try:
                return json.dumps(next(messages))
            except StopIteration:
                handler._stop.set()
                raise socket.timeout

        devtools._recv_frame.side_effect = receive_event
        context = mock.MagicMock()
        context.__enter__.return_value = devtools
        with (
            mock.patch.object(browser_register.browser_devtools, "page_websocket", return_value="ws://browser"),
            mock.patch.object(browser_register.browser_devtools, "DevToolsSocket", return_value=context),
        ):
            handler._run()

        proxy_response = devtools.send.call_args_list[0].args
        server_response = devtools.send.call_args_list[1].args
        self.assertEqual(proxy_response[0], "Fetch.continueWithAuth")
        self.assertEqual(proxy_response[1]["authChallengeResponse"]["username"], "clip-user")
        self.assertEqual(proxy_response[1]["authChallengeResponse"]["password"], "clip-password")
        self.assertEqual(server_response[1]["authChallengeResponse"], {"response": "Default"})

    def test_browser_registration_result_does_not_bind_registration_proxy(self):
        registrar = browser_register.BrowserRegistrar()
        registrar.registration_proxy_url = "http://clip-user:clip-password@us.cliproxy.io:3010"
        with (
            mock.patch.object(
                browser_register,
                "browser_runtime_status",
                return_value={"browser_available": True},
            ),
            mock.patch.object(
                browser_register.mail_provider,
                "create_mailbox",
                return_value={"provider": "outlook_token", "address": "new@example.com"},
            ),
            mock.patch.object(browser_register.mail_provider, "mark_mailbox_result"),
            mock.patch.object(
                registrar,
                "_run_devtools_registration",
                return_value={"access_token": "token", "refresh_token": "", "id_token": ""},
            ),
            mock.patch.object(browser_register, "step"),
        ):
            result = registrar.register(1)

        self.assertNotIn("proxy", result)
        self.assertEqual(result["registration_engine"], "browser")

    def test_browser_launch_closes_process_when_devtools_start_fails(self):
        registrar = browser_register.BrowserRegistrar()
        registrar._deadline = time.monotonic() + 30
        process = mock.Mock()
        process.poll.return_value = None
        profile = SimpleNamespace(proxy_url="http://proxy.example:8080", proxy_source="explicit")
        with (
            mock.patch.object(browser_register.browser_devtools, "find_browser_executable", return_value="chromium"),
            mock.patch.object(browser_register.browser_devtools, "free_local_port", return_value=12345),
            mock.patch.object(browser_register.proxy_settings, "get_profile", return_value=profile),
            mock.patch.object(browser_register, "_proxy_geography", return_value={}),
            mock.patch.object(browser_register.subprocess, "Popen", return_value=process),
            mock.patch.object(
                browser_register.browser_devtools,
                "wait_for_devtools",
                side_effect=RuntimeError("browser_devtools_timeout"),
            ),
            mock.patch.object(browser_register.browser_devtools, "close_browser") as close_browser,
            mock.patch.object(browser_register, "step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "browser_devtools_timeout"):
                registrar._launch_devtools_browser(Path("profile"), 1)

        close_browser.assert_called_once_with(12345, process)

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
        old_proxy = openai_register.config.get("proxy")
        openai_register.config["mode"] = "total"
        openai_register.config["proxy"] = "http://proxy-a:8080\nhttp://proxy-b:8080"
        try:
            with (
                mock.patch.object(openai_register, "PlatformRegistrar", return_value=registrar) as registrar_factory,
                mock.patch.object(openai_register.account_service, "add_account_items") as add,
                mock.patch.object(openai_register.account_service, "refresh_accounts") as refresh,
            ):
                outcome = openai_register.worker(2)
        finally:
            openai_register.config["mode"] = old_mode
            openai_register.config["proxy"] = old_proxy

        self.assertTrue(outcome["ok"])
        registrar_factory.assert_called_once_with("http://proxy-b:8080")
        saved = add.call_args.args[0][0]
        self.assertTrue(saved["image_quota_unknown"])
        self.assertEqual(saved["status"], "正常")
        refresh.assert_not_called()

    def test_browser_worker_uses_round_robin_registration_proxy(self):
        registrar = mock.Mock()
        registrar.register.return_value = {
            "email": "user@example.com",
            "access_token": "token",
            "refresh_token": "",
            "fp": {"device_id": "device", "session_id": "session"},
        }
        old_mode = browser_register.config.get("mode")
        old_proxy = browser_register.config.get("proxy")
        browser_register.config["mode"] = "total"
        browser_register.config["proxy"] = "http://proxy-a:8080\nhttp://proxy-b:8080"
        try:
            with (
                mock.patch.object(browser_register, "BrowserRegistrar", return_value=registrar) as registrar_factory,
                mock.patch("services.account_service.account_service.add_account_items") as add,
                mock.patch.object(browser_register, "step"),
            ):
                outcome = browser_register.worker(3)
        finally:
            browser_register.config["mode"] = old_mode
            browser_register.config["proxy"] = old_proxy

        self.assertTrue(outcome["ok"])
        registrar_factory.assert_called_once_with("http://proxy-a:8080")
        add.assert_called_once()


if __name__ == "__main__":
    unittest.main()
