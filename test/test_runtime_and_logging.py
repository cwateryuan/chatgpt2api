from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import anyio
from starlette.datastructures import Headers

from api.support import resolve_client_ip
from services.protocol.conversation import build_image_prompt
from services.runtime_state import RuntimeState
from services.runtime_config import configure_thread_stack_size, configure_threadpool_tokens
from services.config import config
from utils.log import logger


class _Client:
    host = "127.0.0.1"


class _Request:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = Headers(headers or {})
        self.client = _Client()


class RuntimeAndLoggingTests(unittest.TestCase):
    def test_image_slot_probe_is_bounded_and_idle_accounts_are_preferred(self):
        state = RuntimeState()

        class FakeRedis:
            def __init__(self):
                self.args = ()

            def eval(self, *args):
                self.args = args
                return ["", ""]

        fake_redis = FakeRedis()
        state._redis = fake_redis
        self.assertEqual(state.acquire_image_slot([f"token-{index}" for index in range(300)], 2, 180, probe_limit=64), "")
        self.assertEqual(len(fake_redis.args) - 5, 64)

        state._redis = None
        first = state.acquire_image_slot(["a", "b"], 2, 180)
        second = state.acquire_image_slot(["a", "b"], 2, 180)
        third = state.acquire_image_slot(["a", "b"], 2, 180)
        self.assertNotEqual(first, second)
        self.assertIn(third, {"a", "b"})
        self.assertLessEqual(state.get_image_inflight("a"), 2)
        self.assertLessEqual(state.get_image_inflight("b"), 2)
    def test_build_image_prompt_adds_single_image_instruction(self):
        prompt = build_image_prompt("画一只杯子", "1024x1024", "auto")
        self.assertIn("本次只生成一张图片。", prompt)
        self.assertIn("输出图片尺寸为 1024x1024。", prompt)
        self.assertIn("输出图片质量为 auto。", prompt)

    def test_build_image_prompt_does_not_duplicate_instruction(self):
        prompt = build_image_prompt("画一只杯子。本次只生成一张图片。", None, "")
        self.assertEqual(prompt.count("本次只生成一张图片"), 1)

    def test_resolve_client_ip_prefers_cloudflare_header(self):
        request = _Request({
            "CF-Connecting-IP": "203.0.113.10",
            "X-Forwarded-For": "198.51.100.1, 198.51.100.2",
        })
        self.assertEqual(resolve_client_ip(request), "203.0.113.10")

    def test_resolve_client_ip_uses_first_forwarded_for(self):
        request = _Request({"X-Forwarded-For": "198.51.100.1, 198.51.100.2"})
        self.assertEqual(resolve_client_ip(request), "198.51.100.1")

    def test_configure_threadpool_tokens_uses_environment(self):
        async def run_check():
            return configure_threadpool_tokens(default=3)

        with patch.dict(os.environ, {"APP_THREADPOOL_TOKENS": "7"}):
            self.assertEqual(anyio.run(run_check), 7)

    def test_configure_thread_stack_size_uses_environment(self):
        with (
            patch.dict(os.environ, {"APP_THREAD_STACK_SIZE_KB": "1024"}),
            patch("services.runtime_config.threading.stack_size", return_value=0) as stack_size,
        ):
            self.assertEqual(configure_thread_stack_size(), 1024 * 1024)
            stack_size.assert_called_once_with(1024 * 1024)

    def test_empty_log_levels_disables_default_console_levels(self):
        with patch.dict(config.data, {"log_levels": []}, clear=False):
            self.assertFalse(logger._enabled("info"))
            self.assertFalse(logger._enabled("debug"))
            self.assertFalse(logger._enabled("warning"))
            self.assertFalse(logger._enabled("error"))

    def test_missing_log_levels_uses_default_console_levels(self):
        with patch.dict(config.data, {}, clear=True):
            self.assertTrue(logger._enabled("info"))
            self.assertFalse(logger._enabled("debug"))
            self.assertTrue(logger._enabled("warning"))
            self.assertTrue(logger._enabled("error"))


if __name__ == "__main__":
    unittest.main()
