from __future__ import annotations

import unittest
from unittest import mock

from curl_cffi import CurlOpt

from services.image_timeout import ImageDeadlineExpired, ImageRequestDeadline
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI, _curl_deadline_options


class FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"conduit_token": "conduit-token"}


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}
        self.curl_options = {CurlOpt.LOW_SPEED_TIME: 10}
        self.curl_options_seen = None
        self.post_calls = []

    def post(self, *args, **kwargs):
        self.curl_options_seen = dict(self.curl_options)
        self.post_calls.append((args, kwargs))
        return FakeResponse()

    def close(self):
        pass


class OpenAIBackendImageTimeoutTests(unittest.TestCase):
    def test_curl_deadline_options_sets_absolute_timeout_ms(self):
        deadline = ImageRequestDeadline(10, started_at=100)

        with mock.patch("services.image_timeout.time.time", return_value=102.25):
            options = _curl_deadline_options(deadline)

        self.assertEqual(options, {CurlOpt.TIMEOUT_MS: 7750})

    def test_curl_deadline_options_rejects_expired_deadline(self):
        deadline = ImageRequestDeadline(1, started_at=100)

        with mock.patch("services.image_timeout.time.time", return_value=102):
            with self.assertRaises(ImageDeadlineExpired):
                _curl_deadline_options(deadline)

    def test_backend_session_uses_upstream_proxy_scope(self):
        with (
            mock.patch("services.openai_backend_api.account_service.get_account", return_value={}),
            mock.patch("services.openai_backend_api.proxy_settings.build_session_kwargs", return_value={}) as build_kwargs,
            mock.patch("services.openai_backend_api.requests.Session", FakeSession),
        ):
            OpenAIBackendAPI("token")

        self.assertTrue(build_kwargs.call_args.kwargs["upstream"])

    def test_image_sse_temporarily_applies_deadline_curl_option(self):
        with (
            mock.patch("services.openai_backend_api.account_service.get_account", return_value={}),
            mock.patch("services.openai_backend_api.proxy_settings.build_session_kwargs", return_value={}),
            mock.patch("services.openai_backend_api.requests.Session", FakeSession),
        ):
            backend = OpenAIBackendAPI("token")
            deadline = ImageRequestDeadline(5)
            response = backend._start_image_generation(
                "draw",
                ChatRequirements(token="requirements-token"),
                "conduit-token",
                "gpt-image-2",
                deadline=deadline,
            )

        self.assertIsInstance(response, FakeResponse)
        self.assertIn(CurlOpt.TIMEOUT_MS, backend.session.curl_options_seen)
        self.assertEqual(backend.session.curl_options, {CurlOpt.LOW_SPEED_TIME: 10})

    def test_image_requests_reuse_registered_browser_context(self):
        fp = {
            "user_agent": "Registered Chrome UA",
            "impersonate": "chrome136",
            "accept_language": "en-US,en;q=0.9",
            "device_id": "browser-device-id",
            "session_id": "browser-session-id",
            "sec_ch_ua": '"Chromium";v="136"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Linux"',
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
            mock.patch("services.openai_backend_api.proxy_settings.build_session_kwargs", return_value={}) as build_kwargs,
            mock.patch("services.openai_backend_api.requests.Session", FakeSession),
        ):
            backend = OpenAIBackendAPI("token")
            requirements = ChatRequirements(token="requirements-token")
            backend._prepare_image_conversation("draw", requirements, "gpt-image-2")
            backend._start_image_generation("draw", requirements, "conduit-token", "gpt-image-2")

        self.assertEqual(build_kwargs.call_args.kwargs["account"]["proxy"], "http://privoxy:8118")
        prepare_payload = backend.session.post_calls[0][1]["json"]
        image_payload = backend.session.post_calls[1][1]["json"]
        for payload in (prepare_payload, image_payload):
            self.assertEqual(payload["timezone"], "UTC")
            self.assertEqual(payload["timezone_offset_min"], 0)
            self.assertEqual(payload["client_contextual_info"]["screen_width"], 1365)
            self.assertEqual(payload["client_contextual_info"]["page_height"], 681)
        self.assertEqual(backend.session.headers["OAI-Device-Id"], "browser-device-id")
        self.assertEqual(backend.session.headers["User-Agent"], "Registered Chrome UA")


if __name__ == "__main__":
    unittest.main()
