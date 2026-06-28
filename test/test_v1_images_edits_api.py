from __future__ import annotations

import base64
import asyncio
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.ai as ai_module
import api.image_inputs as image_inputs_module


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}
PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
DATA_IMAGE_URL = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode('ascii')}"


class ImagesEditsApiTests(unittest.TestCase):
    def setUp(self):
        self.handle_calls = []

        def fake_handle(payload):
            self.handle_calls.append(payload)
            return {"created": 1, "data": [{"b64_json": base64.b64encode(b"out").decode("ascii")}]}

        self.handler_patcher = mock.patch.object(ai_module.openai_v1_image_edit, "handle", fake_handle)
        self.handler_patcher.start()
        self.addCleanup(self.handler_patcher.stop)
        app = FastAPI()
        app.include_router(ai_module.create_router())
        self.client = TestClient(app)

    def test_edit_accepts_json_image_url(self):
        """测试图片编辑接口支持官方 JSON image_url 引用。"""
        with mock.patch("services.log_service.log_service") as log_service:
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                json={
                    "model": "gpt-image-2",
                    "prompt": "edit",
                    "images": [{"image_url": DATA_IMAGE_URL}],
                    "n": 1,
                    "response_format": "b64_json",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.handle_calls), 1)
        payload = self.handle_calls[0]
        self.assertEqual(payload["prompt"], "edit")
        self.assertEqual(payload["n"], 1)
        self.assertEqual(payload["images"], [(PNG_BYTES, "image_url.png", "image/png")])
        detail = log_service.add.call_args.args[2]
        self.assertEqual(detail["image_inputs"][0]["source_type"], "data_url")
        self.assertEqual(detail["image_inputs"][0]["detected_format"], "PNG")
        self.assertEqual(detail["image_inputs"][0]["width"], 1)
        self.assertEqual(detail["image_inputs"][0]["height"], 1)
        self.assertNotIn(base64.b64encode(PNG_BYTES).decode("ascii"), str(detail))

    def test_edit_logs_invalid_base64_metadata_without_full_payload(self):
        bad_base64 = "not-valid-base64" * 20
        with mock.patch("services.log_service.log_service") as log_service:
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                json={"model": "gpt-image-2", "prompt": "edit", "image": bad_base64},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_image_input")
        detail = log_service.add.call_args.args[2]
        self.assertEqual(detail["image_inputs"][0]["source_type"], "base64")
        self.assertIn("invalid base64", detail["image_inputs"][0]["parse_error"])
        self.assertNotIn(bad_base64, str(detail))

    def test_url_diagnostics_do_not_include_query_string(self):
        class Response:
            status_code = 200
            content = PNG_BYTES
            headers = {"content-type": "image/png", "content-length": str(len(PNG_BYTES))}

        source = image_inputs_module.ImageSourceRef(
            source_type="url",
            value="https://cdn.example.test/path/one.png?token=secret",
        )
        with mock.patch.object(image_inputs_module.requests, "get", return_value=Response()):
            images, diagnostics = asyncio.run(image_inputs_module.read_image_sources_with_diagnostics([source]))

        self.assertEqual(images, [(PNG_BYTES, "one.png", "image/png")])
        self.assertEqual(diagnostics[0]["source_type"], "url")
        self.assertEqual(diagnostics[0]["url_host"], "cdn.example.test")
        self.assertEqual(diagnostics[0]["url_path"], "/path/one.png")
        self.assertNotIn("secret", str(diagnostics))


    def test_edit_rejects_file_id_reference(self):
        """测试图片编辑接口对暂不支持的 file_id 返回明确错误。"""
        response = self.client.post(
            "/v1/images/edits",
            headers=AUTH_HEADERS,
            json={
                "model": "gpt-image-2",
                "prompt": "edit",
                "images": [{"file_id": "file-abc123"}],
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("file_id image references are not supported", response.text)
        self.assertEqual(self.handle_calls, [])


if __name__ == "__main__":
    unittest.main()
