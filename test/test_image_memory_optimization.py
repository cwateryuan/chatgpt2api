from __future__ import annotations

import unittest
from unittest import mock

from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationTimeoutError,
    ImageOutput,
    _generate_single_image,
    format_downloaded_image_result,
    format_image_result,
)
from services.image_timeout import ImageRequestDeadline


class FakeBackend:
    def __init__(self, payloads: list[bytes]):
        self.payloads = payloads

    def iter_image_bytes(self, _urls: list[str]):
        yield from self.payloads


class FakeAccountService:
    def __init__(self) -> None:
        self.tokens = iter(["token-1", "token-2"])
        self.released: list[str] = []
        self.marked: list[tuple[str, bool]] = []

    def get_available_access_token(self, **_kwargs):
        return next(self.tokens)

    def get_account(self, token):
        return {"email": f"{token}@example.test"}

    def release_image_slot(self, token):
        self.released.append(token)

    def mark_image_result(self, token, success):
        self.marked.append((token, success))
        self.release_image_slot(token)


class ImageMemoryOptimizationTests(unittest.TestCase):
    def test_url_result_saves_downloaded_bytes_without_returning_base64(self):
        with mock.patch("services.protocol.conversation.save_image_bytes", return_value="http://app.test/images/one.png") as save:
            result = format_downloaded_image_result(
                FakeBackend([b"image-bytes"]),
                ["https://files.test/one.png"],
                "draw",
                "url",
                "http://app.test",
                123,
            )

        self.assertEqual(result["created"], 123)
        self.assertEqual(result["data"], [{"url": "http://app.test/images/one.png", "revised_prompt": "draw"}])
        save.assert_called_once_with(b"image-bytes", "http://app.test")

    def test_b64_result_preserves_existing_base64_and_saves_once(self):
        with mock.patch("services.protocol.conversation.save_image_bytes", return_value="http://app.test/images/one.png") as save:
            result = format_image_result(
                [{"b64_json": "aW1hZ2UtYnl0ZXM=", "revised_prompt": "revised"}],
                "draw",
                "b64_json",
                "http://app.test",
                123,
            )

        self.assertEqual(result["data"][0]["b64_json"], "aW1hZ2UtYnl0ZXM=")
        self.assertEqual(result["data"][0]["url"], "http://app.test/images/one.png")
        self.assertEqual(result["data"][0]["revised_prompt"], "revised")
        save.assert_called_once_with(b"image-bytes", "http://app.test")

    def test_retry_continue_releases_previous_image_slot(self):
        service = FakeAccountService()
        calls = {"count": 0}

        def fake_stream(_backend, _request, index, total):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("curl: (35) TLS connect error")
            yield ImageOutput(
                kind="result",
                model="gpt-image-2",
                index=index,
                total=total,
                data=[{"url": "http://app.test/image.png"}],
            )

        with (
            mock.patch("services.protocol.conversation.account_service", service),
            mock.patch("services.protocol.conversation.OpenAIBackendAPI") as backend_class,
            mock.patch("services.protocol.conversation.stream_image_outputs", fake_stream),
            mock.patch("services.protocol.conversation.is_tls_connection_error", return_value=True),
            mock.patch("services.protocol.conversation.time.sleep", lambda _secs: None),
            mock.patch("services.protocol.conversation.trim_memory", lambda *_args, **_kwargs: None),
        ):
            backend_class.return_value.close.return_value = None
            outputs = _generate_single_image(ConversationRequest(prompt="draw", model="gpt-image-2"), 1, 1)

        self.assertEqual(outputs[0].data[0]["url"], "http://app.test/image.png")
        self.assertIn("token-1", service.released)
        self.assertIn(("token-2", True), service.marked)

    def test_expired_deadline_raises_image_generation_timeout(self):
        deadline = ImageRequestDeadline(1, started_at=0)

        with self.assertRaises(ImageGenerationTimeoutError) as raised:
            _generate_single_image(
                ConversationRequest(prompt="draw", model="gpt-image-2", deadline=deadline),
                1,
                1,
            )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.code, "image_generation_timeout")


if __name__ == "__main__":
    unittest.main()
