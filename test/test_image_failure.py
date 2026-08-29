from __future__ import annotations

import unittest
from unittest import mock

from services.image_failure import (
    ImageGenerationError,
    classify_conversation_failure,
    classify_message_facts,
    classify_task_failure,
    classify_image_exception,
)
from utils.helper import UpstreamHTTPError


class ImageFailureTests(unittest.TestCase):
    def test_reference_image_required_preserves_public_code_and_param(self) -> None:
        exc = UpstreamHTTPError(
            "/backend-api/f/conversation",
            400,
            {
                "error": {
                    "code": "reference_image_required",
                    "message": "reference image required upstream message",
                    "param": "image",
                    "type": "invalid_request_error",
                }
            },
        )
        failure = classify_image_exception(exc)
        payload = ImageGenerationError(failure=failure).to_openai_error()

        self.assertEqual(failure.code, "invalid_image_input")
        self.assertFalse(failure.switch_account)
        self.assertFalse(failure.account_failure)
        self.assertEqual(payload["error"]["code"], "reference_image_required")
        self.assertEqual(payload["error"]["param"], "image")
        self.assertEqual(payload["error"]["message"], "reference image required upstream message")

    def test_unknown_http_400_does_not_expose_upstream_code(self) -> None:
        exc = UpstreamHTTPError("/backend-api/f/conversation", 400, {"error": {"code": "private_internal_code"}})
        failure = classify_image_exception(exc)
        payload = ImageGenerationError(failure=failure).to_openai_error()
        self.assertEqual(payload["error"]["code"], "invalid_image_input")
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertFalse(failure.switch_account)

    def test_http_status_controls_account_policy(self) -> None:
        rate_failure = classify_image_exception(UpstreamHTTPError("/backend-api/f/conversation", 429, {}))
        server_failure = classify_image_exception(UpstreamHTTPError("/backend-api/f/conversation", 503, {}))
        self.assertTrue(rate_failure.switch_account)
        self.assertTrue(rate_failure.account_failure)
        self.assertTrue(server_failure.switch_account)

    def test_expired_upload_token_is_auth_invalid(self) -> None:
        exc = UpstreamHTTPError(
            "/backend-api/files",
            401,
            {
                "error": {
                    "message": "Provided authentication token is expired. Please try signing in again.",
                    "code": "token_expired",
                }
            },
        )
        failure = classify_image_exception(exc)
        self.assertEqual(failure.code, "auth_invalid")
        self.assertTrue(failure.switch_account)
        self.assertTrue(failure.account_failure)

    def test_free_plan_limit_text_is_quota_exhausted(self) -> None:
        message = (
            "You've hit the Free plan limit for image generations requests. "
            "You can create more images when the limit resets in 5 hours and 5 minutes."
        )
        failure = classify_image_exception(RuntimeError(message))
        self.assertEqual(failure.code, "image_quota_exhausted")
        self.assertTrue(failure.switch_account)
        self.assertTrue(failure.account_failure)
        self.assertEqual(failure.status_code, 429)

    def test_assistant_terminal_text_and_tool_parameter_are_distinct(self) -> None:
        assistant = classify_message_facts(
            role="assistant",
            content_type="text",
            status="finished_successfully",
            end_turn=True,
            has_text=True,
            raw_detail="The model returned text instead of an image.",
        )
        tool_parameters = classify_task_failure({
            "image_gen_message": {
                "author": {"role": "tool"},
                "content": {"content_type": "multimodal_text", "parts": ['{"size":"1024x1024","n":1}']},
                "metadata": {"is_error": True},
            }
        })
        self.assertIsNotNone(assistant)
        self.assertEqual(assistant.code, "upstream_text_reply")
        self.assertIsNone(tool_parameters)

    def test_reference_text_is_classified_without_structured_code(self) -> None:
        failure = classify_message_facts(
            role="assistant",
            content_type="text",
            status="completed",
            end_turn=True,
            has_text=True,
            raw_detail="The request needs a reference image before editing.",
        )
        self.assertIsNotNone(failure)
        self.assertEqual(failure.response_code, "reference_image_required")
        self.assertEqual(failure.param, "image")

    def test_conversation_failure_ignores_prior_turn_and_image_output(self) -> None:
        data = {
            "mapping": {
                "old": {"message": {"author": {"role": "assistant"}, "create_time": 1, "content": {"content_type": "text", "parts": ["old failure"]}, "status": "failed"}},
                "user": {"message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["new"]}, "create_time": 2}},
                "new": {"message": {"author": {"role": "assistant"}, "create_time": 3, "end_turn": True, "status": "completed", "content": {"content_type": "text", "parts": ["needs reference image"]}}},
            }
        }
        failure = classify_conversation_failure(data)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.response_code, "reference_image_required")

        data["mapping"]["image"] = {"message": {"author": {"role": "tool"}, "create_time": 3, "content": {"content_type": "multimodal_text", "parts": [{"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-1"}]}}}
        self.assertIsNone(classify_conversation_failure(data))


if __name__ == "__main__":
    unittest.main()
