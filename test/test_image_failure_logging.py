from __future__ import annotations

import unittest
from unittest.mock import Mock, PropertyMock, patch

from services.image_failure import ImageFailureError, ImageGenerationError, image_failure
from services.image_task_service import ImageTaskService
from services.log_service import LoggedCall
from services.protocol import conversation, openai_v1_image_generations


class ImageFailureLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        log_levels = patch.dict(conversation.config.data, {"log_levels": []})
        log_levels.start()
        self.addCleanup(log_levels.stop)

    async def test_request_400_logs_selected_email_before_returning(self):
        message = "We're so sorry, but the image we created may violate our content policies."
        for error_class in (ImageFailureError, ImageGenerationError):
            for streaming in (False, True):
                for existing_email in ("", "upstream@example.test"):
                    with self.subTest(error_class=error_class, streaming=streaming, existing_email=existing_email):
                        error = error_class(message, failure=image_failure("upstream_text_reply", raw_detail=message))
                        error.account_email = existing_email
                        error.conversation_id = "conversation-1"
                        service = Mock()
                        service.get_available_access_token.return_value = "token-1"
                        service.get_account.return_value = {"email": "selected@example.test"}
                        call = LoggedCall({}, "/v1/images/generations", "gpt-image-2", "文生图")
                        with (
                            patch.object(conversation, "account_service", service),
                            patch.object(conversation, "OpenAIBackendAPI"),
                            patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""),
                            patch.object(conversation, "stream_image_outputs", side_effect=error) as stream,
                            patch.object(conversation, "_retry_image_attempt") as retry,
                            patch.object(conversation, "trim_memory"),
                            patch("services.log_service.log_service.add") as log,
                        ):
                            response = await call.run(openai_v1_image_generations.handle,
                                                      {"prompt": "draw", "stream": streaming})

                        self.assertEqual(response.status_code, 400)
                        log.assert_called_once()
                        detail = log.call_args.args[2]
                        self.assertEqual(detail["account_email"], existing_email or "selected@example.test")
                        self.assertEqual(detail["conversation_id"], "conversation-1")
                        self.assertEqual(detail["failure_code"], "upstream_text_reply")
                        self.assertEqual(detail["error"], message)
                        self.assertFalse(detail["failure_account_failure"])
                        self.assertFalse(detail["failure_retryable"])
                        stream.assert_called_once()
                        retry.assert_not_called()
                        service.mark_image_result.assert_not_called()
                        service.release_image_slot.assert_called_once_with("token-1")

    async def test_background_task_400_logs_selected_email(self):
        error = ImageFailureError("upstream rejected request", failure=image_failure("upstream_text_reply"))
        accounts = Mock()
        accounts.get_available_access_token.return_value = "token-1"
        accounts.get_account.return_value = {"email": "selected@example.test"}
        task_service = ImageTaskService.__new__(ImageTaskService)
        task_service.generation_handler = openai_v1_image_generations.handle
        task_service._update_task = Mock()
        with (
            patch.object(conversation, "account_service", accounts),
            patch.object(conversation, "OpenAIBackendAPI"),
            patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""),
            patch.object(conversation, "stream_image_outputs", side_effect=error),
            patch.object(conversation, "trim_memory"),
            patch("services.image_task_service.log_service.add") as log,
        ):
            task_service._run_task("task-1", "generate", {"prompt": "draw"}, {}, "gpt-image-2")
        log.assert_called_once()
        detail = log.call_args.args[2]
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["account_email"], "selected@example.test")
        self.assertEqual(detail["failure_code"], "upstream_text_reply")
        self.assertEqual(detail["status_code"], 400)
        accounts.mark_image_result.assert_not_called()

    async def test_parallel_failures_keep_matching_email_and_error(self):
        errors = {
            index: ImageGenerationError(
                f"request {index} rejected", failure=image_failure("upstream_text_reply"),
                account_email=f"account-{index}@example.test", conversation_id=f"conversation-{index}",
            ) for index in (1, 2)
        }

        def generate(_request, index, _total):
            raise errors[index]

        call = LoggedCall({}, "/v1/images/generations", "gpt-image-2", "文生图")
        with (
            patch.object(conversation, "_generate_single_image", side_effect=generate),
            patch.object(type(conversation.config), "image_parallel_generation", new_callable=PropertyMock,
                         return_value=True),
            patch("services.log_service.log_service.add") as log,
        ):
            response = await call.run(openai_v1_image_generations.handle, {"prompt": "draw", "n": 2})
        self.assertEqual(response.status_code, 400)
        log.assert_called_once()
        detail = log.call_args.args[2]
        self.assertEqual(detail["account_email"], errors[2].account_email)
        self.assertEqual(detail["conversation_id"], errors[2].conversation_id)
        self.assertEqual(detail["error"], str(errors[2]))
        self.assertEqual(detail["failure_code"], "upstream_text_reply")

    async def test_failure_before_account_selection_does_not_invent_email(self):
        accounts = Mock()
        accounts.get_available_access_token.side_effect = RuntimeError("no available image quota")
        call = LoggedCall({}, "/v1/images/generations", "gpt-image-2", "文生图")
        with (
            patch.object(conversation, "account_service", accounts),
            patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""),
            patch.object(conversation, "OpenAIBackendAPI") as backend,
            patch("services.log_service.log_service.add") as log,
        ):
            await call.run(openai_v1_image_generations.handle, {"prompt": "draw"})
        log.assert_called_once()
        self.assertNotIn("account_email", log.call_args.args[2])
        backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
