from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import api.support as support_module
from api.app import create_app


class WebFallbackTests(unittest.TestCase):
    def test_spa_routes_support_get_and_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            web_dist = Path(tmp)
            (web_dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")
            with mock.patch.object(support_module, "WEB_DIST_DIR", web_dist):
                client = TestClient(create_app())

                get_response = client.get("/image/")
                head_response = client.head("/image/")

        self.assertEqual(get_response.status_code, 200, get_response.text)
        self.assertIn("ok", get_response.text)
        self.assertEqual(head_response.status_code, 200, head_response.text)
        self.assertEqual(head_response.text, "")


if __name__ == "__main__":
    unittest.main()
