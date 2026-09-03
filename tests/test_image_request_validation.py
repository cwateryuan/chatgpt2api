from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from unittest.mock import PropertyMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from api.ai import ImageGenerationRequest
from api.image_inputs import MAX_IMAGE_COUNT, _parse_count
from services.protocol import conversation


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


if __name__ == "__main__":
    unittest.main()
