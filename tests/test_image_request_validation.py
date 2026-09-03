from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from unittest.mock import PropertyMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from api.ai import ImageGenerationRequest
from api.image_inputs import MAX_IMAGE_COUNT, _parse_count
from services.protocol import conversation
from services.image_failure import ImageFailureError, image_failure


class ImageRequestValidationTests(unittest.TestCase):
    def test_generation_request_accepts_up_to_maximum(self) -> None:
        self.assertEqual(MAX_IMAGE_COUNT, 10)
        self.assertEqual(ImageGenerationRequest(prompt="test", n=1).n, 1)
        self.assertEqual(ImageGenerationRequest(prompt="test", n=4).n, 4)
        self.assertEqual(ImageGenerationRequest(prompt="test", n=10).n, 10)

    def test_generation_request_rejects_out_of_range_counts(self) -> None:
        with self.assertRaises(ValidationError):
            ImageGenerationRequest(prompt="test", n=0)
        with self.assertRaises(ValidationError):
            ImageGenerationRequest(prompt="test", n=11)

    def test_edit_request_count_accepts_ten_and_rejects_eleven(self) -> None:
        self.assertEqual(_parse_count("10"), 10)
        with self.assertRaises(HTTPException) as context:
            _parse_count("11")
        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail["error"], "n must be between 1 and 10")


class ImageRequestConcurrencyTests(unittest.TestCase):
    def test_ten_images_submit_ten_independent_parallel_tasks(self) -> None:
        submitted_workers: list[int] = []
        calls: list[tuple[int, int]] = []

        def generate_single(request: conversation.ConversationRequest, index: int, total: int):
            calls.append((index, total))
            return [
                conversation.ImageOutput(
                    kind="result",
                    model=request.model,
                    index=index,
                    total=total,
                )
            ]

        def executor_factory(*, max_workers: int):
            submitted_workers.append(max_workers)
            return RealThreadPoolExecutor(max_workers=max_workers)

        request = conversation.ConversationRequest(model="gpt-image-2", prompt="test", n=10)
        with (
            patch.object(conversation, "is_supported_image_model", return_value=True),
            patch.object(conversation, "_generate_single_image", side_effect=generate_single),
            patch.object(
                type(conversation.config),
                "image_parallel_generation",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch.object(conversation, "ThreadPoolExecutor", side_effect=executor_factory),
        ):
            outputs = list(conversation.stream_image_outputs_with_pool(request))

        self.assertEqual(submitted_workers, [10])
        self.assertEqual(sorted(index for index, _total in calls), list(range(1, 11)))
        self.assertEqual({total for _index, total in calls}, {10})
        self.assertEqual(sorted(output.index for output in outputs), list(range(1, 11)))

    def test_quota_failure_from_poll_switches_account_before_returning_429(self) -> None:
        selected_tokens: list[str] = []
        stream_calls = 0

        def select_account(*args, **kwargs):
            token = ("token-1", "token-2")[len(selected_tokens)]
            selected_tokens.append(token)
            return token

        def stream_images(*args, **kwargs):
            nonlocal stream_calls
            stream_calls += 1
            if stream_calls == 1:
                yield conversation.ImageOutput(
                    kind="progress",
                    model="gpt-image-2",
                    index=1,
                    total=1,
                )
                raise ImageFailureError(
                    "free plan exhausted",
                    failure=image_failure("image_quota_exhausted", raw_detail="free plan exhausted"),
                )
            yield conversation.ImageOutput(
                kind="result",
                model="gpt-image-2",
                index=1,
                total=1,
                data=[{"b64_json": "image"}],
            )

        request = conversation.ConversationRequest(model="gpt-image-2", prompt="test", n=1)
        with (
            patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""),
            patch.object(conversation.account_service, "get_available_access_token", side_effect=select_account),
            patch.object(conversation.account_service, "get_account", return_value={"email": "test@example.com"}),
            patch.object(conversation.account_service, "mark_image_result"),
            patch.object(conversation.account_service, "release_image_slot"),
            patch.object(conversation, "OpenAIBackendAPI"),
            patch.object(conversation, "stream_image_outputs", side_effect=stream_images),
            patch.object(conversation, "is_supported_image_model", return_value=True),
        ):
            outputs = conversation._generate_single_image(request, 1, 1)

        self.assertEqual(selected_tokens, ["token-1", "token-2"])
        self.assertEqual(stream_calls, 2)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, "result")


if __name__ == "__main__":
    unittest.main()
