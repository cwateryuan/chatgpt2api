from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from curl_cffi import requests

from utils.helper import UpstreamHTTPError, ensure_ok


class _JsonErrorHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = b'{"detail":"Model not found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class EnsureOkStreamBodyTests(unittest.TestCase):
    def test_streamed_404_preserves_upstream_json_body(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _JsonErrorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        response = requests.post(
            f"http://127.0.0.1:{port}/backend-api/f/conversation",
            json={"model": "gpt-5-3-mini"},
            stream=True,
            timeout=5,
        )

        with self.assertRaises(UpstreamHTTPError) as caught:
            ensure_ok(response, "/backend-api/f/conversation")

        error = caught.exception
        self.assertEqual(error.status_code, 404)
        self.assertEqual(error.body, {"detail": "Model not found"})
        self.assertIn('"detail": "Model not found"', str(error))


if __name__ == "__main__":
    unittest.main()
