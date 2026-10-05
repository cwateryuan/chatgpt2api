from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from services.image_failure import (
    FINAL_IMAGE_LIMIT_MESSAGE, ImageFailureError, ImageGenerationError,
    classify_final_image_reply, image_failure,
)
from services.image_task_service import ImageTaskService
from services.image_timeout import ImageDeadlineExpired
from services.log_service import LoggedCall
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI
from services.protocol import conversation, openai_v1_image_generations
from test import test_image_cooldown as pool_tests
from test import test_image_error_cooldown as flow_tests
from test.test_image_recovery import RecoveryBackend
from utils.helper import UpstreamHTTPError


PARAMETERS = '{"size":"1088x608","n":1,"prompt":null}'
TITLE = "Generate this image later"
DESCRIPTION = "Schedule this image for free, or upgrade to create it now."
UNAVAILABLE = "It looks like image creation is temporarily unavailable. Do you want to try something else?"


def reply(text=PARAMETERS):
    return conversation.ImageOutput(kind="message", model="gpt-image-2", index=1, total=1,
                                    text=text, conversation_id="conversation-1",
                                    failure=image_failure("upstream_text_reply", raw_detail=text))


class FinalImageReplyRecognitionTests(unittest.TestCase):
    def test_matches_parameter_shape_without_value_validation(self):
        for text in (PARAMETERS, ' \n' + PARAMETERS + '\t', '**' + PARAMETERS + '**',
                     '```json\n' + PARAMETERS + '\n```', '**```\n' + PARAMETERS + '\n```**',
                     '{"prompt" : "中文\\n带转义内容", "n" : null, "size" : false}',
                     '{"size":bad-json,"n":-1,"prompt":}',
                     '{"referenced_image_ids":[],"size":"1x1","n":1,"prompt":"x"}'):
            with self.subTest(text=text):
                failure = classify_final_image_reply(text)
                self.assertEqual(failure.code, "image_quota_exhausted")
                self.assertEqual(failure.public_detail, FINAL_IMAGE_LIMIT_MESSAGE)
                self.assertEqual(failure.raw_detail, text)
                self.assertEqual((failure.status_code, failure.error_type), (429, "insufficient_quota"))

    def test_text_matching_is_exact_after_whitespace_normalization(self):
        for text in (TITLE, DESCRIPTION, TITLE + '\n\n' + DESCRIPTION, UNAVAILABLE,
                     '  Generate  this\timage later\n'):
            with self.subTest(text=text):
                self.assertEqual(classify_final_image_reply(text).code, "image_quota_exhausted")

    def test_does_not_match_nearby_text_or_old_cooldown_errors(self):
        for text in ('{"size":"1x1","n":1}', PARAMETERS[:-1], '[' + PARAMETERS + ']',
                     'Example: ' + PARAMETERS, PARAMETERS + ' more text', '{"size":1,"prompt":2}',
                     TITLE.lower(), TITLE + '.', DESCRIPTION + ' Try again.',
                     UNAVAILABLE[:-1], None, {}, *conversation.IMAGE_ERROR_COOLDOWN_MESSAGES):
            with self.subTest(text=text):
                self.assertIsNone(classify_final_image_reply(text))


class FinalImageReplyFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow = flow_tests.ImageErrorCooldownFlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.stack.close)

    def test_final_text_and_exception_variants_limit_once_before_switching(self):
        f = self.flow
        for text in (PARAMETERS, TITLE, DESCRIPTION, TITLE + '\n' + DESCRIPTION, UNAVAILABLE):
            for mode in ("message", "generation_error", "failure_error", "http_error"):
                with self.subTest(text=text, mode=mode):
                    f.elapsed = 0
                    f.tokens = iter(["a", "b"])
                    f.service.reset_mock()
                    actions = {
                        "message": [reply(text)],
                        "generation_error": flow_tests.error(text),
                        "failure_error": flow_tests.error(text, ImageFailureError),
                        "http_error": UpstreamHTTPError("/backend-api/f/conversation", 400,
                                                        {"error": {"message": text}}),
                    }
                    f.actions = [actions[mode], [flow_tests.result()]]
                    self.assertEqual(f.generate()[0].kind, "result")
                    self.assertEqual([c.args for c in f.service.mark_image_result.call_args_list],
                                     [("a", False), ("b", True)])
                    self.assertEqual(f.service.mark_image_result.call_args_list[0].kwargs,
                                     {"quota_exhausted": True})
                    f.service.cooldown_image_account.assert_not_called()

    def test_disabled_cooldown_and_plain_reply_mode_still_limit(self):
        f = self.flow
        for minutes in (0, 10):
            with self.subTest(minutes=minutes), patch.dict(conversation.config.data,
                                                         {"image_account_cooldown_minutes": minutes}):
                f.tokens = iter(["a"])
                f.actions = [[reply()]]
                f.service.reset_mock()
                with self.assertRaises(ImageGenerationError) as caught:
                    f.generate(message_as_error=False)
                self.assertEqual(caught.exception.code, "image_quota_exhausted")
                self.assertEqual(caught.exception.public_error, FINAL_IMAGE_LIMIT_MESSAGE)
                self.assertEqual(caught.exception.raw_upstream_message, PARAMETERS)
                self.assertEqual(caught.exception.account_email, "a@example.test")
                self.assertEqual(caught.exception.conversation_id, "conversation-1")
                f.service.mark_image_result.assert_called_once_with("a", False, quota_exhausted=True)
                f.service.cooldown_image_account.assert_not_called()

    def test_repeated_limits_preserve_last_error_when_pool_empty(self):
        f = self.flow
        f.tokens = iter(["a", "b", "c"])
        f.actions = [[reply()], [reply(TITLE)], [reply(DESCRIPTION)]]
        with self.assertRaises(ImageGenerationError) as caught:
            f.generate()
        self.assertEqual(f.visited, ["a", "b", "c"])
        self.assertEqual(caught.exception.account_email, "c@example.test")
        self.assertEqual(caught.exception.public_error, DESCRIPTION)
        self.assertEqual(f.service.mark_image_result.call_count, 3)
        self.assertEqual(f.selections[-1][0], {"a", "b", "c"})

    def test_special_second_attempt_limits_without_third_attempt(self):
        f = self.flow
        f.actions = [flow_tests.error(), [reply()]]
        with self.assertRaises(ImageGenerationError) as caught:
            f.generate()
        self.assertEqual(f.visited, ["a", "b"])
        self.assertEqual(caught.exception.account_email, "b@example.test")
        self.assertEqual(caught.exception.status_code, 429)
        f.service.cooldown_image_account.assert_called_once()
        f.service.mark_image_result.assert_called_once_with("b", False, quota_exhausted=True)

    def test_window_or_total_deadline_expiry_still_records_limit(self):
        f = self.flow
        for elapsed in (19.95, 20, 21, 121):
            with self.subTest(elapsed=elapsed):
                f.elapsed = 0
                f.tokens = iter(["a", "b"])
                f.service.reset_mock()
                def delayed():
                    f.advance(elapsed)
                    return [reply()]
                f.actions = [delayed]
                before = len(f.visited)
                with self.assertRaises(ImageGenerationError) as caught:
                    f.generate()
                self.assertEqual(caught.exception.code, "image_quota_exhausted")
                self.assertEqual(f.visited[before:], ["a"])
                f.service.mark_image_result.assert_called_once_with("a", False, quota_exhausted=True)

    def test_selection_or_setup_crossing_window_returns_prior_error(self):
        f = self.flow
        select = f.service.get_available_access_token.side_effect
        create = f.backend.side_effect
        for stage in ("selection", "setup"):
            with self.subTest(stage=stage):
                f.elapsed = 0
                f.tokens = iter(["a", "b"])
                f.service.reset_mock()
                def delayed_select(**kwargs):
                    token = select(**kwargs)
                    if token == "b" and stage == "selection":
                        self.assertLess(kwargs["deadline"].remaining(), 20)
                        f.advance(20)
                    return token
                def delayed_create(**kwargs):
                    backend = create(**kwargs)
                    if backend.token == "b" and stage == "setup":
                        f.advance(20)
                    return backend
                f.service.get_available_access_token.side_effect = delayed_select
                f.backend.side_effect = delayed_create
                f.actions = [[reply()]]
                before = len(f.visited)
                with self.assertRaises(ImageGenerationError) as caught:
                    f.generate()
                self.assertEqual(caught.exception.account_email, "a@example.test")
                self.assertEqual(f.visited[before:], ["a"])
                f.service.release_image_slot.assert_any_call("b")

    def test_selection_timeout_preserves_prior_limit(self):
        f = self.flow
        f.service.get_available_access_token.side_effect = ["a", ImageDeadlineExpired()]
        f.actions = [[reply()]]
        with self.assertRaises(ImageGenerationError) as caught:
            f.generate()
        self.assertEqual(caught.exception.code, "image_quota_exhausted")
        self.assertEqual(caught.exception.account_email, "a@example.test")

    def test_prior_network_retry_does_not_reset_window(self):
        f = self.flow
        def network_error():
            f.advance(18)
            return ConnectionError("failed")
        def limited():
            f.advance(1.8)
            return [reply()]
        f.actions = [network_error, limited]
        with self.assertRaises(ImageGenerationError) as caught:
            f.generate()
        self.assertEqual(f.visited, ["a", "b"])
        self.assertEqual(caught.exception.code, "image_quota_exhausted")

    def test_replacement_may_finish_after_window(self):
        f = self.flow
        def slow_result():
            f.advance(40)
            return [flow_tests.result()]
        f.actions = [[reply()], slow_result]
        self.assertEqual(f.generate()[-1].kind, "result")
        self.assertGreater(f.elapsed, 20)

    def test_image_wins_before_or_after_matching_message_or_exception(self):
        f = self.flow
        for order in ("before", "after", "exception"):
            with self.subTest(order=order):
                f.tokens = iter(["a"])
                f.service.reset_mock()
                def stream(*_args):
                    if order == "before":
                        yield reply()
                    yield flow_tests.result()
                    if order == "after":
                        yield reply()
                    if order == "exception":
                        raise flow_tests.error(PARAMETERS)
                f.stream.side_effect = stream
                self.assertEqual([o.kind for o in f.generate()], ["result"])
                f.service.mark_image_result.assert_called_once_with("a", True)

    def test_progress_and_timeout_do_not_infer_limit(self):
        f = self.flow
        f.actions = [[conversation.ImageOutput(kind="progress", model="gpt-image-2", index=1,
                                               total=1, text=PARAMETERS), flow_tests.result()]]
        f.generate()
        f.service.mark_image_result.assert_called_once_with("a", True)
        for exc in (ImagePollTimeoutError("poll timed out"), ConnectionError("connection timed out")):
            with self.subTest(error=type(exc)):
                exc.raw_upstream_message = PARAMETERS
                f.service.reset_mock()
                f.tokens = iter(["a"])
                f.actions = [exc]
                with patch.object(conversation, "_retry_image_attempt", return_value=False):
                    with self.assertRaises(Exception):
                        f.generate()
                self.assertFalse(any(c.kwargs.get("quota_exhausted")
                                     for c in f.service.mark_image_result.call_args_list))

    async def test_http_stream_and_background_logs_keep_raw_reply_once(self):
        f = self.flow
        for streaming in (False, True):
            f.tokens = iter(["a"])
            f.actions = [[reply()]]
            with patch("services.log_service.log_service.add") as log:
                response = await LoggedCall({}, "/v1/images/generations", "gpt-image-2", "image").run(
                    openai_v1_image_generations.handle, {"prompt": "draw", "stream": streaming})
            self.assertEqual(response.status_code, 429)
            payload = json.loads(response.body)["error"]
            self.assertEqual((payload["code"], payload["type"]),
                             ("image_quota_exhausted", "insufficient_quota"))
            self.assertEqual(payload["message"], FINAL_IMAGE_LIMIT_MESSAGE)
            log.assert_called_once()
            detail = log.call_args.args[2]
            self.assertEqual(detail["error"], FINAL_IMAGE_LIMIT_MESSAGE)
            self.assertEqual(detail["raw_upstream_message"], PARAMETERS)
            self.assertEqual(detail["account_email"], "a@example.test")
            self.assertEqual(detail["conversation_id"], "conversation-1")
        f.tokens = iter(["a"])
        f.actions = [[reply()]]
        tasks = ImageTaskService.__new__(ImageTaskService)
        tasks.generation_handler = openai_v1_image_generations.handle
        tasks._update_task = Mock()
        with patch("services.image_task_service.log_service.add") as log:
            tasks._run_task("task", "generate", {"prompt": "draw"}, {}, "gpt-image-2")
        log.assert_called_once()
        detail = log.call_args.args[2]
        self.assertEqual(detail["raw_upstream_message"], PARAMETERS)
        self.assertEqual(detail["account_email"], "a@example.test")
        self.assertEqual(detail["conversation_id"], "conversation-1")

    def test_parallel_images_keep_the_final_error_account(self):
        f = self.flow
        def stream(backend, _request, index, _total):
            message = reply()
            message.conversation_id = f"image-{index}-{backend.token}"
            yield message
        f.stream.side_effect = stream
        with patch.object(conversation, "_retry_image_attempt", return_value=False), \
             patch.object(type(conversation.config), "image_parallel_generation", new_callable=PropertyMock,
                          return_value=True):
            with self.assertRaises(ImageGenerationError) as caught:
                list(conversation.stream_image_outputs_with_pool(conversation.ConversationRequest(
                    prompt="draw", model="gpt-image-2", n=2, deadline=f.deadline)))
        self.assertEqual(caught.exception.code, "image_quota_exhausted")
        self.assertTrue(caught.exception.conversation_id.startswith("image-2-"))
        token = caught.exception.conversation_id.rsplit("-", 1)[1]
        self.assertEqual(caught.exception.account_email, f"{token}@example.test")
        self.assertEqual(f.service.mark_image_result.call_count, 2)

    def test_stream_failure_after_output_logs_failed_account(self):
        f = self.flow
        f.tokens = iter(["b"])
        f.actions = [[reply()]]
        with self.assertRaises(ImageGenerationError) as caught:
            f.generate()
        def chunks():
            yield {"_account_email": "a@example.test", "_conversation_id": "first", "data": []}
            raise caught.exception
        call = LoggedCall({}, "/v1/images/generations", "gpt-image-2", "image")
        with patch("services.log_service.log_service.add") as log:
            with self.assertRaises(ImageGenerationError):
                list(call.stream(chunks()))
        detail = log.call_args.args[2]
        self.assertEqual(detail["account_email"], "b@example.test")
        self.assertEqual(detail["conversation_id"], "conversation-1")
        self.assertEqual(detail["raw_upstream_message"], PARAMETERS)


class FinalImageReplyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_stream = conversation.stream_image_outputs
        self.flow = flow_tests.ImageErrorCooldownFlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.stack.close)
        self.flow.deadline.budget.side_effect = lambda seconds: min(seconds, self.flow.deadline.remaining())
        self.flow.deadline.request_timeout.side_effect = lambda seconds: min(seconds, self.flow.deadline.remaining())
        self.flow.stream.side_effect = self.original_stream

    def test_sse_reference_parameters_are_not_generated_images(self):
        reference = "file_00000000" + "a" * 24
        generated = "file_00000000" + "b" * 24
        parameters = json.dumps({"size": "1088x608", "n": 1, "prompt": "draw",
                                 "referenced_image_ids": [reference]})
        def message(role, content, **extra):
            return {"message": {"author": {"role": role}, "content": content,
                                "status": "completed", "end_turn": True, **extra},
                    "conversation_id": "conv-1"}
        payloads = [
            message("user", {"content_type": "text", "parts": [parameters]}),
            {"type": "server_ste_metadata", "metadata": {"tool_invoked": True, "turn_use_case": "image gen"}},
            message("assistant", {"content_type": "code", "text": parameters}),
            message("tool", {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://" + generated}]}),
        ]
        events = list(conversation.iter_conversation_payloads(iter([*(json.dumps(p) for p in payloads), "[DONE]"])))
        self.assertTrue(all(reference not in event["file_ids"] for event in events))
        self.assertEqual(events[-1]["file_ids"], [generated])
        backend = SimpleNamespace(pool_proxy="", close=Mock(),
                                  resolve_conversation_image_urls=Mock(return_value=["https://files.test/image.png"]))
        self.flow.backend.side_effect = lambda **_kw: backend
        with patch.object(conversation, "conversation_events", return_value=iter(events)), \
             patch.object(conversation, "format_downloaded_image_result", return_value={"data": [{"b64_json": "image"}]}):
            outputs = self.flow.generate()
        self.assertEqual(outputs[-1].kind, "result")
        self.flow.service.mark_image_result.assert_called_once_with("a", True)

    def test_finished_sse_reply_limits_only_after_existing_poll(self):
        f = self.flow
        f.tokens = iter(["a"])
        reference = "file_00000000" + "a" * 24
        text = json.dumps({"size": "1088x608", "n": 1, "prompt": "draw",
                           "referenced_image_ids": [reference]})
        backend = SimpleNamespace(pool_proxy="", close=Mock(),
                                  resolve_conversation_image_urls=Mock(return_value=[]),
                                  _poll_image_results=Mock(return_value=([], [])),
                                  _query_backend_tasks=Mock(return_value=[]))
        f.backend.side_effect = lambda **_kw: backend
        events = [{"type": "conversation.done", "conversation_id": "conv-1", "text": text,
                   "turn_use_case": "image gen", "message_role": "assistant", "content_type": "text"}]
        with patch.object(conversation, "conversation_events", return_value=iter(events)):
            with self.assertRaises(ImageGenerationError) as caught:
                f.generate()
        backend.resolve_conversation_image_urls.assert_called_once()
        backend._poll_image_results.assert_called_once()
        self.assertEqual(caught.exception.code, "image_quota_exhausted")
        self.assertEqual(caught.exception.conversation_id, "conv-1")
        f.service.mark_image_result.assert_called_once_with("a", False, quota_exhausted=True)

    def test_incremental_tool_parameters_do_not_claim_reference_files_as_results(self):
        reference = "file_00000000" + "a" * 24
        payloads = [
            {"type": "server_ste_metadata", "metadata": {"tool_invoked": True}},
            {"message": {"author": {"role": "assistant"}, "status": "in_progress",
                         "content": {"content_type": "text", "parts": [""]}}},
            {"p": "/message/content/parts/0", "o": "append", "v":
                '{"size":"1088x608","n":1,"prompt":"draw","referenced_image_ids":["'},
            {"p": "/message/content/parts/0", "o": "append", "v": reference},
            {"p": "/message/content/parts/0", "o": "append", "v": '"]}'},
        ]
        events = list(conversation.iter_conversation_payloads(iter([*(json.dumps(p) for p in payloads), "[DONE]"])))
        self.assertEqual(events[-1]["file_ids"], [])
        self.assertIsNotNone(classify_final_image_reply(events[-1]["text"]))

    def test_task_recovery_preserves_original_reply_before_classification(self):
        f = self.flow
        f.tokens = iter(["a"])
        backend = RecoveryBackend(tasks=[{"image_gen_message": {
            "author": {"role": "assistant"}, "content": {"content_type": "text", "parts": [PARAMETERS]},
            "status": "completed", "end_turn": True}}])
        backend.pool_proxy = ""
        backend.close = Mock()
        f.backend.side_effect = lambda **_kw: backend
        def events(*_args, **_kwargs):
            yield {"type": "conversation.event", "conversation_id": "conv-1", "tool_invoked": True}
            raise TimeoutError("stream interrupted")
        with patch.object(conversation, "conversation_events", side_effect=events), \
             patch.dict(conversation.config.data, {"image_stream_recovery_enabled": True}):
            with self.assertRaises(ImageGenerationError) as caught:
                f.generate()
        self.assertEqual(caught.exception.code, "image_quota_exhausted")
        self.assertEqual(caught.exception.raw_upstream_message, PARAMETERS)
        self.assertEqual(caught.exception.conversation_id, "conv-1")

    def test_poll_terminal_error_retains_conversation_and_excludes_reference_ids(self):
        reference = "file_00000000" + "a" * 24
        text = json.dumps({"size": "1088x608", "n": 1, "prompt": "draw", "referenced_image_ids": [reference]})
        message = {"author": {"role": "assistant"}, "status": "completed", "end_turn": True,
                   "metadata": {"async_task_type": "image_gen", "referenced_image_ids": [reference]},
                   "content": {"content_type": "code", "text": text}}
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend._query_backend_tasks = Mock(return_value=[])
        backend._get_conversation = Mock(return_value={"mapping": {"current": {"message": message}}})
        with patch.dict(conversation.config.data, {"image_poll_initial_wait_secs": 0}):
            with self.assertRaises(ImageFailureError) as caught:
                backend._poll_image_results("conv-polled", 1)
        self.assertEqual(caught.exception.conversation_id, "conv-polled")
        self.assertEqual(str(caught.exception), text)
        self.flow.stream.side_effect = caught.exception
        self.flow.tokens = iter(["a"])
        with self.assertRaises(ImageGenerationError) as limited:
            self.flow.generate()
        self.assertEqual(limited.exception.code, "image_quota_exhausted")
        self.assertEqual(limited.exception.conversation_id, "conv-polled")


class FinalImageReplyStorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pool_tests.ImageCooldownTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def generate(self, service):
        with patch.object(conversation, "account_service", service), \
             patch.object(service, "fetch_remote_info", side_effect=lambda token, *_a, **_kw: service.get_account(token)), \
             patch.object(conversation, "OpenAIBackendAPI", return_value=SimpleNamespace(pool_proxy="", close=Mock())), \
             patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""), \
             patch.object(conversation, "stream_image_outputs", return_value=iter([reply()])), \
             patch.object(conversation, "_retry_image_attempt", return_value=False), \
             patch.object(conversation, "trim_memory"):
            with self.assertRaises(ImageGenerationError) as caught:
                conversation._generate_single_image(conversation.ConversationRequest(
                    prompt="draw", model="gpt-image-2", message_as_error=True), 1, 1)
            self.assertEqual(caught.exception.code, "image_quota_exhausted")

    def test_both_backends_limit_zero_success_and_unknown_quota_without_changing_cooldown(self):
        for database in (True, False):
            for minutes in (0, 30):
                for quota, unknown in ((25, False), (5, False), (0, True)):
                    with self.subTest(database=database, minutes=minutes, quota=quota, unknown=unknown), \
                         patch.dict(conversation.config.data, {"image_account_cooldown_minutes": minutes}):
                        account = pool_tests.account("a", quota, image_quota_unknown=unknown,
                                                     success=0, fail=2, rate_limit_429=3,
                                                     image_cooldown_started_at=10, image_cooldown_until=20)
                        name = f"limit-{database}-{minutes}-{quota}"
                        service = self.fixture.build([account], database, name)
                        method = "mutate_account" if database else "save_accounts"
                        with patch.object(service.storage, method, wraps=getattr(service.storage, method)) as save:
                            self.generate(service)
                        save.assert_called_once()
                        stored = service.storage.load_accounts()[0]
                        self.assertEqual((stored["status"], stored["quota"], stored["image_quota_unknown"]),
                                         ("限流", 0, False))
                        self.assertEqual((stored["success"], stored["fail"], stored["rate_limit_429"]), (0, 3, 3))
                        self.assertEqual((stored["image_cooldown_started_at"], stored["image_cooldown_until"]), (10, 20))
                        restarted = pool_tests.AccountService(service.storage)
                        self.assertEqual(restarted.get_image_pool_metrics()["current_available"], 0)
                        self.assertEqual(restarted.get_image_cooldown_metrics()["cooling_accounts"], 0)
                        restarted.clear_image_cooldowns()
                        self.assertEqual(restarted.get_account("a")["status"], "限流")
                        self.assertEqual(restarted.get_account("a")["quota"], 0)
                        with self.assertRaises(RuntimeError):
                            restarted.get_available_access_token()
                        restarted.update_account("a", {"restore_at": "2000-01-01T00:00:00+00:00"}, quiet=True)
                        self.assertIn("a", restarted.list_image_recovery_tokens())

    def test_auto_remove_setting_is_honored(self):
        for database in (True, False):
            with self.subTest(database=database), \
                 patch.dict(conversation.config.data, {"auto_remove_rate_limited_accounts": True}):
                service = self.fixture.build([pool_tests.account("a")], database, f"remove-{database}")
                self.generate(service)
                self.assertEqual(service.storage.load_accounts(), [])

    def test_account_lock_preserves_concurrent_unrelated_update(self):
        for database in (True, False):
            service = self.fixture.build([pool_tests.account("a", fail=3, success=2)], database, f"concurrent-{database}")
            other = pool_tests.AccountService(service.storage) if database else service
            barrier = Barrier(2)
            def limited():
                barrier.wait()
                service.mark_image_result("a", False, quota_exhausted=True)
            def update():
                barrier.wait()
                other.update_account("a", {"success": 12, "email": "updated@example.test"}, quiet=True)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(limited), executor.submit(update)]
                for future in futures:
                    future.result()
            account = service.storage.load_accounts()[0]
            self.assertEqual((account["status"], account["quota"], account["fail"], account["success"]),
                             ("限流", 0, 4, 12))
            self.assertEqual(account["email"], "updated@example.test")


if __name__ == "__main__":
    unittest.main()
