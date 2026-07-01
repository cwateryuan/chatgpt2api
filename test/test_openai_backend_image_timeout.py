from __future__ import annotations

import unittest
from unittest import mock

from curl_cffi import CurlOpt

from services.image_timeout import ImageDeadlineExpired, ImageRequestDeadline
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI, _curl_deadline_options, _post_stream_with_curl_options


class FakeResponse:
    status_code = 200


class FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.headers = {}
        self.curl_options = {CurlOpt.LOW_SPEED_TIME: 10}
        self.curl_options_seen = None

    def post(self, *_args, **_kwargs):
        self.curl_options_seen = dict(self.curl_options)
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

    def test_post_stream_with_curl_options_restores_session_options(self):
        session = FakeSession()

        response = _post_stream_with_curl_options(
            session,
            "https://example.test/stream",
            curl_options={CurlOpt.TIMEOUT_MS: 1234},
            stream=True,
        )

        self.assertIsInstance(response, FakeResponse)
        self.assertEqual(session.curl_options_seen[CurlOpt.TIMEOUT_MS], 1234)
        self.assertEqual(session.curl_options, {CurlOpt.LOW_SPEED_TIME: 10})


if __name__ == "__main__":
    unittest.main()
