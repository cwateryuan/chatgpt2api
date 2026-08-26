from __future__ import annotations

import unittest
from unittest import mock

from services.image_failure import ImageGenerationError
from services.image_timeout import ImageRequestDeadline
from services.protocol import conversation


class RecoveryBackend:
    def __init__(self, *, conversation_payload=None, tasks=None, urls=None):
        self.conversation_payload = conversation_payload or {"mapping": {}}
        self.tasks = tasks or []
        self.urls = urls or []
        self.found_started_at = None

    def find_conversation_by_prompt(self, prompt, started_at, timeout_secs=5.0):
        self.found_started_at = started_at
        return "conv-recovered"

    def _query_backend_tasks(self, conversation_id, timeout_secs=5.0):
        return self.tasks

    def _get_conversation(self, conversation_id, timeout_secs=5.0):
        return self.conversation_payload

    def _extract_image_tool_records(self, payload):
        return [{"file_ids": ["file-generated"], "sediment_ids": []}] if payload.get("has_image") else []

    def resolve_conversation_image_urls(self, *args, **kwargs):
        return self.urls


class ImageRecoveryTests(unittest.TestCase):
    def request(self) -> conversation.ConversationRequest:
        return conversation.ConversationRequest(
            prompt="make an image",
            model="gpt-image-2",
            timeout_secs=30,
            deadline=ImageRequestDeadline(30),
            response_format="b64_json",
        )

    def test_stream_failure_recovers_existing_task_without_resubmitting(self) -> None:
        backend = RecoveryBackend(conversation_payload={"has_image": True}, urls=["https://files.test/image.png"])
        with mock.patch.object(
            conversation,
            "format_downloaded_image_result",
            return_value={"data": [{"b64_json": "encoded"}]},
        ):
            output = conversation._recover_after_image_stream_failure(
                backend,
                self.request(),
                {"conversation_id": "conv-1", "tool_invoked": True},
                TimeoutError("stream timed out"),
                1,
                1,
                123.0,
                failure_code="image_stream_timeout",
            )

        self.assertEqual(output.kind, "result")
        self.assertEqual(output.data[0]["b64_json"], "encoded")
        self.assertEqual(output.conversation_id, "conv-1")

    def test_stream_failure_uses_real_start_time_when_recovering_conversation(self) -> None:
        backend = RecoveryBackend()
        request = self.request()
        request.deadline = ImageRequestDeadline(30)
        with mock.patch.object(conversation, "format_downloaded_image_result"):
            with self.assertRaises(ImageGenerationError):
                conversation._recover_after_image_stream_failure(
                    backend,
                    request,
                    {"tool_invoked": True},
                    TimeoutError("stream timed out"),
                    1,
                    1,
                    456.0,
                    failure_code="image_stream_timeout",
                )
        self.assertEqual(backend.found_started_at, 456.0)

    def test_reference_task_failure_returns_request_message(self) -> None:
        backend = RecoveryBackend(tasks=[{
            "image_gen_message": {
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": ["reference image required"]},
                "metadata": {"is_error": True},
            }
        }])
        output = conversation._recover_after_image_stream_failure(
            backend,
            self.request(),
            {"conversation_id": "conv-1"},
            TimeoutError("stream timed out"),
            1,
            1,
            123.0,
            failure_code="image_stream_timeout",
        )
        self.assertEqual(output.kind, "message")
        self.assertEqual(output.failure.response_code, "reference_image_required")
        self.assertEqual(output.failure.param, "image")

    def test_sse_interrupt_recovers_without_resubmitting_generation(self) -> None:
        backend = RecoveryBackend(
            conversation_payload={"has_image": True},
            urls=["https://files.test/image.png"],
        )

        def interrupted_events(*_args, **_kwargs):
            yield {
                "conversation_id": "conv-1",
                "tool_invoked": True,
                "type": "conversation.event",
            }
            raise TimeoutError("SSE interrupted")

        request = self.request()
        with (
            mock.patch.object(conversation, "conversation_events", interrupted_events),
            mock.patch.object(conversation, "format_downloaded_image_result", return_value={"data": [{"b64_json": "encoded"}]}),
        ):
            outputs = list(conversation.stream_image_outputs(backend, request))

        self.assertEqual([item.kind for item in outputs], ["progress", "result"])
        self.assertEqual(outputs[-1].conversation_id, "conv-1")


if __name__ == "__main__":
    unittest.main()
