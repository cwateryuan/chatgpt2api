from __future__ import annotations

import unittest
from unittest import mock

from services.protocol.conversation import format_downloaded_image_result, format_image_result


class FakeBackend:
    def __init__(self, payloads: list[bytes]):
        self.payloads = payloads

    def iter_image_bytes(self, _urls: list[str]):
        yield from self.payloads


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


if __name__ == "__main__":
    unittest.main()
