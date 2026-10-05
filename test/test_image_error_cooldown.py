from __future__ import annotations

import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import ANY, Mock, PropertyMock, patch

from services.image_cooldown import ImageSchedulingUnavailable
from services.image_failure import ImageFailureError, ImageGenerationError, image_failure
from services.image_task_service import ImageTaskService
from services.image_timeout import ImageDeadlineExpired, ImageRequestDeadline
from services.log_service import LoggedCall
from services.openai_backend_api import ImageContentPolicyError, ImagePollTimeoutError
from services.protocol import conversation, openai_v1_image_generations
from test import test_image_cooldown as cooldown_tests
from utils.helper import UpstreamHTTPError


MESSAGE = conversation.IMAGE_ERROR_COOLDOWN_MESSAGE
FILE_UPLOAD_MESSAGE = conversation.IMAGE_FILE_UPLOAD_COOLDOWN_MESSAGE
USAGE_MESSAGE = conversation.IMAGE_USAGE_COOLDOWN_MESSAGE


def error(message=MESSAGE, error_class=ImageGenerationError):
    exc = error_class(message, failure=image_failure("upstream_text_reply", raw_detail=message))
    exc.conversation_id = "conversation-1"
    return exc


def result():
    return conversation.ImageOutput(kind="result", model="gpt-image-2", index=1, total=1,
                                    data=[{"url": "https://example.test/image.png"}])


class ImageErrorCooldownFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(conversation.config.data,
                                           {"image_account_cooldown_minutes": 10, "log_levels": []}))
        self.elapsed = 0.0
        self.deadline = Mock(spec=ImageRequestDeadline)
        self.deadline.timeout_secs = 120
        self.deadline.remaining.side_effect = lambda: 120 - self.elapsed

        def require():
            if self.deadline.remaining() <= 0:
                raise ImageDeadlineExpired()
            return self.deadline.remaining()

        self.deadline.require.side_effect = require
        self.deadline.sleep.side_effect = self.advance
        self.stack.enter_context(patch.object(conversation.time, "monotonic", side_effect=lambda: self.elapsed))
        self.service = Mock()
        self.tokens = iter(["a", "b", "c", "d", "e"])
        self.selections = []

        def select(**kwargs):
            self.selections.append((set(kwargs["excluded_tokens"]), kwargs["deadline"]))
            try:
                return next(self.tokens)
            except StopIteration:
                raise ImageSchedulingUnavailable("empty") from None

        self.service.get_available_access_token.side_effect = select
        self.service.get_account.side_effect = lambda token: {"email": f"{token}@example.test"}
        self.service.cooldown_image_account.side_effect = lambda token, **_: self.service.release_image_slot(token)
        self.service.mark_image_result.side_effect = lambda token, *_a, **_kw: self.service.release_image_slot(token)
        self.stack.enter_context(patch.object(conversation, "account_service", self.service))
        self.stack.enter_context(patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""))
        self.stack.enter_context(patch.object(conversation, "trim_memory"))
        self.backends = []

        def backend(access_token, **_kwargs):
            value = SimpleNamespace(token=access_token, pool_proxy="", close=Mock())
            self.backends.append(value)
            return value

        self.backend = self.stack.enter_context(patch.object(conversation, "OpenAIBackendAPI", side_effect=backend))
        self.actions = []
        self.visited = []

        def stream(backend, _request, _index, _total):
            self.visited.append(backend.token)
            action = self.actions.pop(0)
            if callable(action):
                action = action()
            if isinstance(action, BaseException):
                raise action
            yield from action

        self.stream = self.stack.enter_context(patch.object(conversation, "stream_image_outputs", side_effect=stream))

    def advance(self, seconds):
        self.elapsed += seconds

    def generate(self, *, message_as_error=True):
        return conversation._generate_single_image(conversation.ConversationRequest(
            model="gpt-image-2", prompt="draw", message_as_error=message_as_error, deadline=self.deadline), 1, 1)

    def test_matching_error_cools_a_and_uses_b_once(self):
        self.actions = [error(), [result()]]
        self.assertEqual(self.generate()[0].kind, "result")
        self.assertEqual(self.visited, ["a", "b"])
        self.assertEqual(self.selections[1][0], {"a"})
        self.assertIs(self.selections[1][1].parent, self.deadline)
        self.service.cooldown_image_account.assert_called_once()
        self.assertEqual(self.service.cooldown_image_account.call_args.args, ("a",))
        self.service.mark_image_result.assert_called_once_with("b", True)
        self.assertEqual([call.args[0] for call in self.service.release_image_slot.call_args_list], ["a", "b"])
        for backend in self.backends:
            backend.close.assert_called_once()

    def test_exact_match_and_error_representations(self):
        for message in (MESSAGE, FILE_UPLOAD_MESSAGE, USAGE_MESSAGE):
            variants = [
                error(" \n" + message + "\t"), error(message, error_class=ImageFailureError),
                UpstreamHTTPError("/backend-api/f/conversation", 400, {"error": {"message": message}}),
                ImageContentPolicyError(message),
                [conversation.ImageOutput(kind="message", model="gpt-image-2", index=1, total=1, text=message)],
            ]
            for variant in variants:
                with self.subTest(message=message, variant=type(variant)):
                    self.actions = [variant, [result()]]
                    self.tokens = iter(["a", "b"])
                    self.service.cooldown_image_account.reset_mock()
                    self.assertEqual(self.generate()[0].kind, "result")
                    self.service.cooldown_image_account.assert_called_once()

    def test_other_messages_do_not_cool_or_retry(self):
        for message in [MESSAGE + " Try later.", MESSAGE.lower(), MESSAGE[:-1], MESSAGE[:-1] + "。",
                        FILE_UPLOAD_MESSAGE + " Try later.", FILE_UPLOAD_MESSAGE.lower(), FILE_UPLOAD_MESSAGE[:-1],
                        USAGE_MESSAGE + " Try later.", USAGE_MESSAGE.lower(), USAGE_MESSAGE[:-1],
                        "All available accounts have reached the file upload limit. Please try again later.",
                        "We're so sorry, but the image we created may violate our content policies."]:
            with self.subTest(message=message):
                self.tokens = iter(["a", "b"])
                self.actions = [error(message)]
                with self.assertRaises(ImageGenerationError):
                    self.generate()
        self.service.cooldown_image_account.assert_not_called()
        self.service.mark_image_result.assert_not_called()
        self.assertEqual(len(self.visited), 12)

    def test_disabled_setting_keeps_original_400_behavior(self):
        for message in (MESSAGE, FILE_UPLOAD_MESSAGE, USAGE_MESSAGE):
            with self.subTest(message=message):
                self.actions = [error(message)]
                self.tokens = iter(["a"])
                with patch.dict(conversation.config.data, {"image_account_cooldown_minutes": 0}):
                    with self.assertRaises(ImageGenerationError):
                        self.generate()
        self.assertEqual(self.visited, ["a", "a", "a"])
        self.service.cooldown_image_account.assert_not_called()

    def test_b_matching_failure_cools_b_without_trying_c(self):
        self.actions = [error(), error()]
        with self.assertRaises(ImageGenerationError) as caught:
            self.generate()
        self.assertEqual(caught.exception.account_email, "b@example.test")
        self.assertEqual(self.visited, ["a", "b"])
        self.assertEqual([c.args[0] for c in self.service.cooldown_image_account.call_args_list], ["a", "b"])
        self.service.mark_image_result.assert_not_called()

    def test_b_additional_matching_failure_cools_b_without_trying_c(self):
        for message in (FILE_UPLOAD_MESSAGE, USAGE_MESSAGE):
            with self.subTest(message=message):
                self.tokens = iter(["a", "b", "c"])
                self.actions = [error(), error(message)]
                self.service.cooldown_image_account.reset_mock()
                with self.assertRaises(ImageGenerationError) as caught:
                    self.generate()
                self.assertEqual(caught.exception.account_email, "b@example.test")
                self.assertEqual(self.visited[-2:], ["a", "b"])
                self.assertEqual([c.args[0] for c in self.service.cooldown_image_account.call_args_list], ["a", "b"])
                self.service.mark_image_result.assert_not_called()

    def test_file_upload_summary_cools_only_final_account_without_failure_counters(self):
        def exhausted_file_upload():
            self.advance(conversation.IMAGE_RETRY_WINDOW_SECONDS)
            return UpstreamHTTPError("/backend-api/files", 429, {"code": "throttled"})

        self.tokens = iter(["a", "b"])
        self.actions = [exhausted_file_upload]
        with self.assertRaises(ImageGenerationError) as caught:
            self.generate()
        self.assertEqual(caught.exception.public_error, FILE_UPLOAD_MESSAGE)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.code, "throttled")
        self.assertEqual(caught.exception.account_email, "a@example.test")
        self.assertEqual(self.visited, ["a"])
        self.service.cooldown_image_account.assert_called_once_with("a", started_at=ANY)
        self.service.mark_image_result.assert_not_called()

    def test_b_other_errors_do_not_enter_legacy_retries(self):
        for exc in [error("other request error"), ConnectionError("connection failed"),
                    ImagePollTimeoutError("poll timeout"), ImageDeadlineExpired(),
                    UpstreamHTTPError("/backend-api/f/conversation", 503, {"error": {"message": "unavailable"}})]:
            with self.subTest(error=type(exc)):
                self.tokens = iter(["a", "b", "c"])
                self.actions = [error(), exc]
                before = len(self.visited)
                with self.assertRaises(ImageGenerationError) as caught:
                    self.generate()
                self.assertEqual(caught.exception.account_email, "b@example.test")
                self.assertEqual(self.visited[before:], ["a", "b"])

    def test_no_replacement_preserves_a_error(self):
        first = error()
        self.tokens = iter(["a"])
        self.actions = [first]
        with self.assertRaises(ImageGenerationError) as caught:
            self.generate()
        self.assertIs(caught.exception, first)
        self.assertEqual(caught.exception.account_email, "a@example.test")
        self.assertEqual(self.visited, ["a"])

    def test_window_expiry_and_backoff_boundary_still_cool(self):
        for elapsed in (20, 21, 19.95, 121):
            with self.subTest(elapsed=elapsed):
                self.elapsed = 0
                self.tokens = iter(["a", "b"])
                self.service.cooldown_image_account.reset_mock()
                def delayed_error():
                    self.advance(elapsed)
                    return error()
                self.actions = [delayed_error]
                before = len(self.visited)
                with self.assertRaises(ImageGenerationError) as caught:
                    self.generate()
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(self.visited[before:], ["a"])
                self.service.cooldown_image_account.assert_called_once()

    def test_selection_crossing_window_releases_b_and_returns_a(self):
        select = self.service.get_available_access_token.side_effect
        def delayed_select(**kwargs):
            token = select(**kwargs)
            if token == "b":
                self.assertLess(kwargs["deadline"].remaining(), 20)
                self.advance(20)
            return token
        self.service.get_available_access_token.side_effect = delayed_select
        self.actions = [error()]
        with self.assertRaises(ImageGenerationError) as caught:
            self.generate()
        self.assertEqual(caught.exception.account_email, "a@example.test")
        self.assertEqual(self.visited, ["a"])
        self.service.release_image_slot.assert_any_call("b")

    def test_prior_non_400_retry_does_not_reset_special_retry_window(self):
        def network_error():
            self.advance(18)
            return ConnectionError("upstream connection failed")
        def target_error():
            self.advance(1.8)
            return error()
        self.actions = [network_error, target_error]
        with self.assertRaises(ImageGenerationError) as caught:
            self.generate()
        self.assertEqual(caught.exception.account_email, "b@example.test")
        self.assertEqual(self.visited, ["a", "b"])
        self.service.cooldown_image_account.assert_called_once()
        self.assertEqual(self.service.cooldown_image_account.call_args.args, ("b",))

    def test_setup_crossing_window_does_not_submit_b(self):
        create = self.backend.side_effect
        def delayed_backend(**kwargs):
            backend = create(**kwargs)
            if backend.token == "b":
                self.advance(20)
            return backend
        self.backend.side_effect = delayed_backend
        self.actions = [error()]
        with self.assertRaises(ImageGenerationError) as caught:
            self.generate()
        self.assertEqual(caught.exception.account_email, "a@example.test")
        self.assertEqual(self.visited, ["a"])
        self.backends[-1].close.assert_called_once()

    def test_b_can_complete_after_retry_window(self):
        def slow_result():
            self.advance(40)
            return [result()]
        self.actions = [error(), slow_result]
        self.assertEqual(self.generate()[0].kind, "result")
        self.service.mark_image_result.assert_called_once_with("b", True)
        self.assertGreater(self.elapsed, 20)

    def test_plain_message_mode_preserves_reply_after_second_failure(self):
        message = lambda: [conversation.ImageOutput(kind="message", model="gpt-image-2", index=1, total=1,
                                                    text=MESSAGE, conversation_id="plain")]
        self.actions = [message, message]
        outputs = self.generate(message_as_error=False)
        self.assertEqual(outputs[-1].text, MESSAGE)
        self.assertEqual(outputs[-1].account_email, "b@example.test")
        self.assertEqual(self.service.cooldown_image_account.call_count, 2)

    def test_existing_result_is_kept_without_duplicate_generation(self):
        def stream(*_args):
            yield result()
            raise error()
        self.stream.side_effect = stream
        outputs = self.generate()
        self.assertEqual(outputs[0].kind, "result")
        self.service.mark_image_result.assert_called_once_with("a", True)
        self.service.cooldown_image_account.assert_not_called()
        self.backend.assert_called_once()

    def test_bookkeeping_failure_does_not_release_retained_a_slot(self):
        self.service.cooldown_image_account.side_effect = None
        self.service.cooldown_image_account.return_value = None
        self.actions = [error(), [result()]]
        self.generate()
        self.service.release_image_slot.assert_called_once_with("b")

    async def test_sync_stream_and_background_logs_use_final_email(self):
        for streaming in (False, True):
            self.tokens = iter(["a", "b"])
            self.actions = [error(), error()]
            with patch("services.log_service.log_service.add") as log:
                response = await LoggedCall({}, "/v1/images/generations", "gpt-image-2", "image").run(
                    openai_v1_image_generations.handle, {"prompt": "draw", "stream": streaming})
            self.assertEqual(response.status_code, 400)
            log.assert_called_once()
            self.assertEqual(log.call_args.args[2]["account_email"], "b@example.test")
        self.tokens = iter(["a", "b"])
        self.actions = [error(), error()]
        tasks = ImageTaskService.__new__(ImageTaskService)
        tasks.generation_handler = openai_v1_image_generations.handle
        tasks._update_task = Mock()
        with patch("services.image_task_service.log_service.add") as log:
            tasks._run_task("task", "generate", {"prompt": "draw"}, {}, "gpt-image-2")
        log.assert_called_once()
        self.assertEqual(log.call_args.args[2]["account_email"], "b@example.test")

    def test_parallel_images_each_switch_once_and_preserve_final_error(self):
        counts = Counter()
        final_errors = {}
        lock = Lock()
        def stream(backend, _request, index, _total):
            with lock:
                counts[index] += 1
                exc = error()
                exc.account_email = f"{backend.token}@example.test"
                exc.conversation_id = f"image-{index}-attempt-{counts[index]}"
                final_errors[index] = exc
            raise exc
        self.stream.side_effect = stream
        with patch.object(type(conversation.config), "image_parallel_generation", new_callable=PropertyMock,
                          return_value=True):
            with self.assertRaises(ImageGenerationError) as caught:
                list(conversation.stream_image_outputs_with_pool(conversation.ConversationRequest(
                    model="gpt-image-2", prompt="draw", n=2, deadline=self.deadline)))
        self.assertEqual(counts, {1: 2, 2: 2})
        self.assertIs(caught.exception, final_errors[2])
        self.assertEqual(self.service.cooldown_image_account.call_count, 4)


class ImageErrorCooldownStorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cooldown_tests.ImageCooldownTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def build(self, database, name="error"):
        service = self.fixture.build([cooldown_tests.account("a", 25, success=3, fail=2, rate_limit_429=1)],
                                     database, name + str(database))
        service.storage.save_accounts(list(service._accounts.values()))
        return service

    def test_persists_only_cooldown_before_releasing_without_cache_reset(self):
        for database in (True, False):
            with self.subTest(database=database):
                service = self.build(database)
                with patch("time.time", return_value=1000):
                    token = service.get_available_access_token()
                    before = service.storage.load_accounts()[0]
                    batch = service._cooldown_batches.copy()
                    release = service.release_image_slot
                    def checked_release(token):
                        self.assertEqual(service.get_account(token)["image_cooldown_until"], 4600)
                        release(token)
                    write_method = "mutate_account" if database else "save_accounts"
                    with patch.object(service, "release_image_slot", side_effect=checked_release), \
                         patch.object(service.storage, write_method, wraps=getattr(service.storage, write_method)) as write:
                        updated = service.cooldown_image_account(token, started_at=1000)
                    write.assert_called_once()
                    self.assertEqual(service._cooldown_batches, batch)
                    persisted = service.storage.load_accounts()[0]
                    self.assertEqual({k: v for k, v in persisted.items() if not k.startswith("image_cooldown_")},
                                     {k: v for k, v in before.items() if not k.startswith("image_cooldown_")})
                    self.assertEqual(updated["image_cooldown_started_at"], 1000)
                    self.assertEqual(service.get_image_cooldown_metrics()["cooling_accounts"], 1)
                    reloaded = cooldown_tests.AccountService(service.storage)
                    with self.assertRaises(ImageSchedulingUnavailable):
                        reloaded.get_available_access_token()
                with patch("time.time", return_value=4600):
                    self.assertEqual(service.get_available_access_token(), "a")
                    service.release_image_slot("a")

    def test_recalculate_clear_and_disabled_behavior(self):
        for database in (True, False):
            with self.subTest(database=database):
                service = self.build(database, "settings")
                with patch.dict(conversation.config.data, {"image_account_cooldown_minutes": 10}), \
                     patch("time.time", return_value=1000):
                    service.cooldown_image_account("a", started_at=1000)
                    service.on_image_cooldown_config_changed(10, 20, 1001)
                    self.assertEqual(service.get_account("a")["image_cooldown_until"], 2200)
                    self.assertEqual(service.clear_image_cooldowns()["cleared"], 1)
                    service.on_image_cooldown_config_changed(20, 30, 1002)
                    self.assertEqual(service.get_account("a")["image_cooldown_until"], 0)
                with patch.dict(conversation.config.data, {"image_account_cooldown_minutes": 0}), \
                     patch.object(service.storage, "mutate_account") as mutate, \
                     patch.object(service.storage, "save_accounts") as save:
                    service.cooldown_image_account("a", started_at=1003)
                    mutate.assert_not_called()
                    save.assert_not_called()

    def test_existing_later_cooldown_keeps_its_start(self):
        for database in (True, False):
            service = self.build(database, "later")
            service.cooldown_image_account("a", started_at=2000)
            service.cooldown_image_account("a", started_at=1000)
            self.assertEqual(service.get_account("a")["image_cooldown_started_at"], 2000)
            self.assertEqual(service.get_account("a")["image_cooldown_until"], 5600)

    def test_storage_failure_retains_lease(self):
        for database in (True, False):
            with self.subTest(database=database):
                service = self.build(database, "failed-write")
                token = service.get_available_access_token()
                method = "mutate_account" if database else "save_accounts"
                with patch.object(service.storage, method, side_effect=RuntimeError("disk unavailable")):
                    self.assertIsNone(service.cooldown_image_account(token, started_at=1000))
                self.assertEqual(self.fixture.runtime.image_inflight_snapshot([token])[token], 1)
                service.release_image_slot(token)

    def test_concurrent_quota_update_is_not_overwritten(self):
        service = self.build(True, "concurrent")
        other_worker = cooldown_tests.AccountService(service.storage)
        barrier = Barrier(2)
        def cooldown():
            barrier.wait()
            service.cooldown_image_account("a", started_at=1000)
        def change_quota():
            barrier.wait()
            other_worker.update_account("a", {"quota": 17, "success": 11, "fail": 7}, quiet=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(cooldown), executor.submit(change_quota)]
            for future in futures:
                future.result()
        updated = service.get_account("a")
        self.assertEqual((updated["quota"], updated["success"], updated["fail"]), (17, 11, 7))
        self.assertEqual(updated["image_cooldown_until"], 4600)

    def test_real_pool_cools_two_failed_accounts_and_leaves_third_for_next_request(self):
        for database in (True, False):
            with self.subTest(database=database):
                service = self.fixture.build(
                    [cooldown_tests.account(t, 25, email=f"{t}@example.test") for t in ("a", "b", "c")],
                    database, f"real-pool-{database}")
                selected = []
                def backend(access_token, **_kwargs):
                    selected.append(access_token)
                    return SimpleNamespace(pool_proxy="", close=lambda: None)
                def fail_stream(*_args):
                    raise error()
                with patch.object(conversation, "account_service", service), \
                     patch.object(conversation, "OpenAIBackendAPI", side_effect=backend), \
                     patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""), \
                     patch.object(conversation, "stream_image_outputs", side_effect=fail_stream), \
                     patch.object(conversation, "trim_memory"):
                    with self.assertRaises(ImageGenerationError) as caught:
                        conversation._generate_single_image(conversation.ConversationRequest(
                            prompt="draw", model="gpt-image-2", message_as_error=True), 1, 1)
                self.assertEqual(len(selected), 2)
                self.assertEqual(len(set(selected)), 2)
                self.assertEqual(caught.exception.account_email, f"{selected[-1]}@example.test")
                self.assertEqual(service.get_image_cooldown_metrics()["cooling_accounts"], 2)
                remaining = service.get_available_access_token()
                self.assertNotIn(remaining, selected)
                service.release_image_slot(remaining)
                for token in selected:
                    account = service.get_account(token)
                    self.assertEqual((account["quota"], account["success"], account["fail"]), (25, 0, 0))


if __name__ == "__main__":
    unittest.main()
