from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api
from services.account_service import AccountService
from services.register import apple_mailbox as apple, browser_register, mail_provider, openai_register
from services.register_service import RegisterService, _normalize
from services.storage.database_storage import DatabaseStorageBackend
from services.storage.json_storage import JSONStorageBackend


URL = "https://api.wdmail.top/m?f=json&key=test-secret&e=user@icloud.com:private-token"
IMPORT = f"user+one@icloud.com----{URL}\nuser+two@icloud.com----{URL}\nother@icloud.com----{URL}"


def response(payload=None, *, status=200, text="", headers=None):
    result = SimpleNamespace(status_code=status, text=text, headers=headers or {})
    result.json = mock.Mock(return_value=payload) if payload is not None else mock.Mock(side_effect=ValueError("invalid"))
    return result


class AppleMailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = (
            mock.patch.object(apple, "STATE_FILE", Path(self.tmp.name) / "state.json"),
            mock.patch.object(apple, "_state", {}),
            mock.patch.object(apple, "_signature", None),
            mock.patch.object(apple, "_owner", "test-run"),
            mock.patch.object(apple, "_cancelled", lambda: False),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.records = apple.parse_credentials(IMPORT)
        self.entry = {"id": "apple-main", "provider_ref": "apple-main", "type": "apple", "enable": True, "mailboxes": IMPORT}
        self.conf = {"request_timeout": 1, "wait_timeout": 0.2, "wait_interval": 0.01, "user_agent": "test", "proxy": ""}
        self.mail_config = {**self.conf, "providers": [self.entry]}

    def provider(self):
        provider = mail_provider.AppleMailProvider(self.entry, self.conf)
        self.addCleanup(provider.close)
        return provider

    def parse(self, result):
        return apple.parse_response(result, {"address": "user+one@icloud.com"}, mail_provider._parse_received_at)

    def message(self, code="123456", *, date=None, message_id="latest"):
        return {"message_id": message_id, "subject": "OpenAI verification code", "sender": "OpenAI", "text_content": f"Use code {code}", "received_at": date}

    def test_import_preserves_alias_urls_colon_and_deduplicates(self):
        text = f" user+One@icloud.com:ignored----https://example.com/share/key----{URL} \nUSER+ONE@icloud.com----{URL}&extra=1\n{URL}"
        records = apple.parse_credentials(text)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["email"], "USER+ONE@icloud.com")
        self.assertEqual(records[0]["mail_api_url"], URL + "&extra=1")
        self.assertEqual(records[1]["email"], "user@icloud.com")
        self.assertEqual(records[1]["mail_api_url"], URL)
        alias_url = URL.replace("user@", "user+tag@")
        self.assertEqual(apple.parse_credentials(alias_url)[0]["email"], "user+tag@icloud.com")
        self.assertEqual(apple.mother_address("user+tag@icloud.com"), "user@icloud.com")
        self.assertEqual(apple.mother_address("user+tag@example.com"), "user+tag@example.com")
        self.assertEqual(apple.parse_credentials(apple.merge_credentials(IMPORT, "")), self.records)
        for text in (f"user@icloud.com----{URL}&token=part----part", URL + "&token=part----part"):
            self.assertEqual(apple.parse_credentials(text)[0]["mail_api_url"], URL + "&token=part----part")

    def test_invalid_import_errors_do_not_include_keys(self):
        for text in (f"missing----{URL}", "user@icloud.com----", "user@icloud.com----https://example.com/share/key----", URL.replace("&e=", "&missing=")):
            with self.assertRaises(ValueError) as caught:
                apple.parse_credentials(text)
            self.assertNotIn("test-secret", str(caught.exception))
            self.assertNotIn("private-token", str(caught.exception))

    def test_generic_json_explicit_code_and_received_time(self):
        messages = self.parse(response({"success": True, "data": {"status_code": 200, "subject": "ChatGPT", "content": "Hello", "verification_code": "482913", "arrived_at": "2026-10-05T10:00:00Z"}}))
        self.assertEqual(apple.extract_code(messages[0]), "482913")
        self.assertEqual(messages[0]["received_at"].tzinfo, timezone.utc)
        self.assertIn("2026-10-05", messages[0]["message_id"])

    def test_list_json_html_cleanup_preference_and_empty_inbox(self):
        self.assertEqual(self.parse(response({"code": 0, "data": {"messages": []}})), [])
        messages = self.parse(response({"code": 0, "data": {"messages": [
            {"id": "advert", "subject": "Order 999999", "sender": "Shop", "received_at": 1791200000, "text_body": "999999"},
            {"id": "otp", "subject": "ChatGPT verification", "sender": "OpenAI", "received_at": 1791199990.5, "html_body": "<style>123456</style><script>222222</script><p>Enter&nbsp;code <b>548039</b></p>"},
        ]}}))
        self.assertEqual(messages[0]["message_id"], "otp")
        self.assertEqual(apple.extract_code(messages[0]), "548039")
        self.assertNotIn("123456", messages[0]["text_content"])
        self.assertEqual(messages[0]["received_at"].microsecond, 500000)

    def test_html_response_requires_html_evidence_and_code_boundaries(self):
        for headers, body in (({"Content-Type": "text/html"}, "<p>code: 4567</p>"), ({}, "<BODY>code 87654321</BODY>")):
            messages = self.parse(response(text=body, headers=headers))
            self.assertIsNotNone(apple.extract_code(messages[0]))
        with self.assertRaises(apple.AppleMailboxError):
            self.parse(response(text="oops secret"))
        self.assertIsNone(apple.extract_code(self.message("123456789")))

    def test_effective_http_and_api_status_are_preserved_and_redacted(self):
        cases = [(response(status=401, text=URL), 401), (response({"code": 401, "message": URL}), 401), (response({"code": 502}), 502), (response({"success": True, "data": {"status_code": 403}}), 403)]
        for result, expected in cases:
            with self.assertRaises(apple.AppleMailboxError) as caught:
                self.parse(result)
            self.assertEqual(caught.exception.status_code, expected)
            self.assertNotIn("test-secret", str(caught.exception))

    def test_requests_exact_imported_url_and_hides_network_errors(self):
        provider = self.provider()
        mailbox = provider.create_mailbox()
        with mock.patch.object(provider.session, "get", return_value=response({"code": 0, "data": {"messages": []}})) as get:
            self.assertEqual(provider.fetch_recent_messages(mailbox), [])
        self.assertEqual(get.call_args.args[0], URL)
        with mock.patch.object(provider.session, "get", side_effect=RuntimeError(URL)):
            with self.assertRaises(apple.AppleMailboxError) as caught:
                provider.fetch_recent_messages(mailbox)
        self.assertNotIn("test-secret", str(caught.exception))

    def test_old_undated_message_snapshot_and_changed_latest_id(self):
        provider = self.provider()
        mailbox = {"address": "user+one@icloud.com"}
        with mock.patch.object(provider, "fetch_recent_messages", return_value=[self.message("111111")]):
            provider.prepare_code_request(mailbox)
        baseline = mailbox["_code_requested_at"]
        with mock.patch.object(provider, "fetch_recent_messages", side_effect=[[self.message("111111")], [self.message("222222")]]):
            self.assertEqual(provider.wait_for_code(mailbox), "222222")
        self.assertEqual(mailbox["_code_requested_at"], baseline)
        with mock.patch.object(provider, "fetch_recent_messages", side_effect=[[self.message("222222")], [self.message("333333")]]):
            self.assertEqual(provider.wait_for_code(mailbox), "333333")
        self.assertEqual(len(mailbox["_seen_code_message_refs"]), 2)

    def test_freshness_uses_single_five_second_skew(self):
        provider = self.provider()
        now = datetime.now(timezone.utc)
        mailbox = {"address": "user@icloud.com", "_code_requested_at": now.isoformat()}
        messages = [self.message("111111", date=now - timedelta(seconds=6)), self.message("222222", date=now - timedelta(seconds=4))]
        with mock.patch.object(provider, "fetch_recent_messages", return_value=messages):
            self.assertEqual(provider.wait_for_code(mailbox), "222222")

    def test_three_consecutive_errors_and_success_resets_counter(self):
        provider = self.provider()
        mailbox = {"address": "user@icloud.com"}
        error = apple.AppleMailboxError("request rejected", 502)
        with mock.patch.object(provider, "fetch_recent_messages", side_effect=[error, error, [], error, error, [self.message()]]):
            self.assertEqual(provider.wait_for_code(mailbox), "123456")
        with mock.patch.object(provider, "fetch_recent_messages", side_effect=error) as fetch:
            with self.assertRaises(apple.AppleMailboxError):
                provider.wait_for_code(mailbox)
            self.assertEqual(fetch.call_count, 3)
        with mock.patch.object(provider, "fetch_recent_messages", side_effect=apple.AppleMailboxError("rejected", 401)) as fetch:
            with self.assertRaises(apple.AppleMailboxError):
                provider.wait_for_code(mailbox)
            self.assertEqual(fetch.call_count, 1)

    def test_poll_does_not_write_pool_and_request_is_deadline_bounded(self):
        provider = self.provider()
        mailbox = provider.create_mailbox()
        with mock.patch.object(apple, "_save") as save, mock.patch.object(provider.session, "get", return_value=response({"success": True, "data": {"content": "code 987654"}})) as get:
            self.assertEqual(provider.wait_for_code(mailbox), "987654")
            save.assert_not_called()
        self.assertLessEqual(get.call_args.kwargs["timeout"], 0.2)

    def test_mother_inbox_serialization_and_concurrent_exclusive_claims(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            def claim():
                try:
                    return apple.claim(self.records, "apple-main")
                except apple.ApplePoolBusyError:
                    return None
            results = [item for item in executor.map(lambda _: claim(), range(8)) if item]
        self.assertEqual(len(results), 2)
        self.assertEqual(len({apple.mother_address(item["email"]) for item in results}), 2)
        first = next(item for item in results if item["email"].startswith("user"))
        apple.finish({**first, "address": first["email"]}, "used")
        next_item = apple.claim(self.records, "apple-main")
        self.assertNotEqual(next_item["email"], first["email"])
        self.assertEqual(apple.mother_address(next_item["email"]), "user@icloud.com")

    def test_finish_reset_and_new_run_reclaim_only_in_use(self):
        provider = self.provider()
        first = provider.create_mailbox()
        mail_provider.mark_mailbox_result(first, success=True)
        second = provider.create_mailbox()
        second["_apple_credential_error"] = 401
        mail_provider.mark_mailbox_result(second, success=False, error=apple.AppleMailboxError("rejected", 401))
        third = provider.create_mailbox()
        apple._signature = None
        before = apple.pool_stats(self.records)
        self.assertEqual(before, {"unused": 0, "in_use": 1, "used": 1, "failed": 0, "token_invalid": 1})
        apple.begin_run("new-owner", lambda: False)
        self.assertEqual(apple.pool_stats(self.records)["unused"], 1)
        mail_provider.mark_mailbox_result(third, success=True)
        self.assertEqual(apple.pool_stats(self.records)["used"], 1)
        self.assertEqual(apple.reset(self.records, "failed"), 1)
        self.assertEqual(apple.pool_stats(self.records)["used"], 1)
        self.assertEqual(apple.reset(self.records, "all"), 1)
        self.assertEqual(apple.pool_stats(self.records)["unused"], 3)

    def test_timeout_cancel_and_failure_state_mapping(self):
        provider = self.provider()
        for error in (RuntimeError("browser_otp_timeout"), apple.AppleMailboxCancelledError("cancelled")):
            mailbox = provider.create_mailbox()
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            self.assertEqual(apple.pool_stats(self.records)["unused"], 3)
        mailbox = provider.create_mailbox()
        mail_provider.mark_mailbox_result(mailbox, success=False, error=RuntimeError(URL))
        self.assertEqual(apple.pool_stats(self.records)["failed"], 1)
        self.assertNotIn("test-secret", apple.STATE_FILE.read_text())
        with mock.patch.object(apple, "_cancelled", lambda: True):
            with self.assertRaises(apple.AppleMailboxCancelledError):
                apple.claim(self.records, "apple-main")

    def test_persist_failure_rolls_back_in_memory_claim(self):
        with mock.patch.object(apple, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                apple.claim(self.records, "apple-main")
        self.assertEqual(apple.pool_stats(self.records)["unused"], 3)

    def test_busy_pool_waits_for_release_or_stop_without_exhaustion(self):
        self.entry["mailboxes"] = f"user+one@icloud.com----{URL}\nuser+two@icloud.com----{URL}"
        first = mail_provider.create_mailbox(self.mail_config)
        results = []
        done = threading.Event()
        def create():
            try:
                results.append(mail_provider.create_mailbox(self.mail_config))
            except Exception as error:
                results.append(error)
            finally:
                done.set()
        thread = threading.Thread(target=create)
        thread.start()
        self.assertFalse(done.wait(0.05))
        mail_provider.mark_mailbox_result(first, success=True)
        self.assertTrue(done.wait(2))
        thread.join(2)
        self.assertIsInstance(results[0], dict)
        self.assertEqual(results[0]["address"], "user+two@icloud.com")
        with mock.patch.object(apple, "_cancelled", lambda: True):
            with self.assertRaises(apple.AppleMailboxCancelledError):
                mail_provider.create_mailbox(self.mail_config)

    def test_config_merges_by_id_on_reorder_and_redacts_all_outputs(self):
        with mock.patch("services.register_service._db_backend", return_value=None):
            service = RegisterService(Path(self.tmp.name) / "register.json")
            service.update({"mail": {"providers": [self.entry, {**self.entry, "id": "other-channel", "mailboxes": f"last@icloud.com----{URL}"}]}})
            snapshot = service.get()
            self.assertNotIn("test-secret", json.dumps(snapshot))
            self.assertNotIn("private-token", json.dumps(service.runtime_snapshot()))
            self.assertEqual(snapshot["mail"]["providers"][0]["mailboxes_count"], 3)
            providers = snapshot["mail"]["providers"][::-1]
            providers[1]["mailboxes"] = f"user+one@icloud.com----{URL}&new=1"
            service.update({"mail": {"providers": providers}})
            stored = service._config["mail"]["providers"]
            self.assertEqual(stored[0]["id"], "other-channel")
            self.assertEqual(apple.parse_credentials(stored[0]["mailboxes"])[0]["email"], "last@icloud.com")
            records = apple.parse_credentials(stored[1]["mailboxes"])
            self.assertEqual(len(records), 3)
            self.assertEqual(records[0]["mail_api_url"], URL + "&new=1")
            self.assertEqual(_normalize({"mail": {"providers": [{**self.entry, "type": "icloud"}]}})["mail"]["providers"][0]["type"], "apple")

    def test_reset_api_scope_auth_and_running_guard(self):
        with mock.patch("services.register_service._db_backend", return_value=None):
            service = RegisterService(Path(self.tmp.name) / "register.json")
            service.update({"mail": {"providers": [self.entry]}})
            mailbox = self.provider().create_mailbox()
            mail_provider.mark_mailbox_result(mailbox, success=False, error=RuntimeError("bad"))
            app = FastAPI()
            app.include_router(register_api.create_router())
            with TestClient(app) as client, mock.patch.object(register_api, "register_service", service), mock.patch.object(register_api, "require_admin") as admin:
                reply = client.post("/api/register/apple-pool/reset", json={"provider_id": "apple-main", "scope": "failed"})
                self.assertEqual(reply.status_code, 200)
                admin.assert_called()
                self.assertEqual(reply.json()["register"]["mail"]["providers"][0]["mailboxes_stats"]["unused"], 3)
                self.assertEqual(client.post("/api/register/apple-pool/reset", json={"provider_id": "apple-main", "scope": "invalid"}).status_code, 400)
                with mock.patch.object(service, "_runner_alive_locked", return_value=True):
                    self.assertEqual(client.post("/api/register/apple-pool/reset", json={"provider_id": "apple-main", "scope": "all"}).status_code, 409)

    def test_http_registration_preserves_rt_alias_and_web_source(self):
        with mock.patch.object(openai_register, "create_session"):
            registrar = openai_register.PlatformRegistrar()
        mailbox = {"provider": "apple", "address": "user+one@icloud.com"}
        with mock.patch.object(openai_register, "create_mailbox", return_value=mailbox), mock.patch.object(registrar, "_load_geo_environment"), mock.patch.object(registrar, "_platform_authorize", return_value="login") as authorize, mock.patch.object(registrar, "_passwordless_login", return_value={"access_token": "at", "refresh_token": "rt", "id_token": "id"}), mock.patch.object(mail_provider, "mark_mailbox_result"), mock.patch.object(openai_register, "_human_pause"), mock.patch.object(openai_register, "step"):
            result = registrar.register(1)
        self.assertEqual(result["email"], "user+one@icloud.com")
        self.assertEqual(result["refresh_token"], "rt")
        self.assertEqual(result["source_type"], "web")
        self.assertEqual(result["registration_engine"], "http")
        self.assertEqual(authorize.call_args.args[0], "user+one@icloud.com")

    def test_http_signup_prepares_inbox_before_otp_can_be_sent(self):
        with mock.patch.object(openai_register, "create_session"):
            registrar = openai_register.PlatformRegistrar()
        mailbox = {"provider": "apple", "address": "user+one@icloud.com"}
        order = []
        with mock.patch.object(openai_register, "create_mailbox", return_value=mailbox), mock.patch.object(registrar, "_load_geo_environment"), mock.patch.object(registrar, "_platform_authorize", return_value="signup"), mock.patch.object(mail_provider, "prepare_code_request", side_effect=lambda *_: order.append("baseline")), mock.patch.object(registrar, "_register_user", side_effect=lambda *_: order.append("register")), mock.patch.object(registrar, "_send_otp", side_effect=lambda *_: order.append("send")), mock.patch.object(openai_register, "wait_for_code", return_value="123456"), mock.patch.object(registrar, "_validate_otp"), mock.patch.object(registrar, "_open_about_you"), mock.patch.object(registrar, "_create_account"), mock.patch.object(registrar, "_exchange_registered_tokens", return_value={"access_token": "at", "refresh_token": "rt"}), mock.patch.object(mail_provider, "mark_mailbox_result"), mock.patch.object(openai_register, "_human_pause"), mock.patch.object(openai_register, "step"):
            result = registrar.register(1)
        self.assertEqual(order, ["baseline", "register", "send"])
        self.assertEqual(result["refresh_token"], "rt")

    def test_busy_apple_pool_can_use_another_provider_without_health_disable(self):
        self.entry["mailboxes"] = f"user+one@icloud.com----{URL}\nuser+two@icloud.com----{URL}"
        mail_provider.create_mailbox(self.mail_config)
        fallback = {"id": "fallback", "type": "tempmail_lol", "enable": True, "priority": 2}
        factory = mail_provider._provider_from_entry
        def provider(entry, conf):
            if entry["type"] == "apple":
                return factory(entry, conf)
            result = mock.Mock()
            result.create_mailbox.return_value = {"provider": "tempmail_lol", "address": "fallback@example.com"}
            return result
        with mock.patch.object(mail_provider, "_provider_from_entry", side_effect=provider), mock.patch.object(mail_provider, "_record_health_metadata") as health:
            mailbox = mail_provider.create_mailbox({**self.mail_config, "providers": [self.entry, fallback]})
        self.assertEqual(mailbox["address"], "fallback@example.com")
        health.assert_not_called()

    def test_browser_apple_source_rt_and_shared_baseline(self):
        registrar = browser_register.BrowserRegistrar()
        mailbox = {"provider": "apple", "address": "user+one@icloud.com", "_code_requested_at": "2026-10-05T10:00:00Z"}
        with mock.patch.object(browser_register, "browser_runtime_status", return_value={"browser_available": True}), mock.patch.object(mail_provider, "create_mailbox", return_value=mailbox), mock.patch.object(mail_provider, "mark_mailbox_result"), mock.patch.object(registrar, "_run_devtools_registration", return_value={"access_token": "at", "refresh_token": "rt", "registration_token_mode": "oauth"}), mock.patch.object(browser_register, "step"):
            result = registrar.register(1)
        self.assertEqual(result["source_type"], "web")
        self.assertEqual(result["registration_token_mode"], "oauth")
        self.assertEqual(result["refresh_token"], "rt")
        with mock.patch.object(mail_provider, "wait_for_code", return_value="123456") as wait, mock.patch.object(browser_register, "step"):
            self.assertEqual(registrar._wait_for_otp(mailbox, 1), "123456")
        self.assertEqual(mailbox["_code_requested_at"], "2026-10-05T10:00:00Z")
        self.assertLessEqual(wait.call_args.args[0]["wait_timeout"], 300)
        self.assertTrue(mail_provider.supports_passwordless(mailbox))
        self.assertEqual(_normalize({})["browser_token_mode"], "session")

    def test_account_metadata_and_rt_survive_json_and_database_storage(self):
        stores = [JSONStorageBackend(Path(self.tmp.name) / "accounts.json"), DatabaseStorageBackend(f"sqlite:///{Path(self.tmp.name) / 'accounts.db'}")]
        for storage in stores:
            service = AccountService(storage)
            service.add_account_items([{"access_token": "test-at", "refresh_token": "test-rt", "email": "user+one@icloud.com", "registration_engine": "http", "source_type": "web", "type": "Plus"}])
            service.update_account("test-at", {"success": 1, "quota": 24}, quiet=True)
            account = AccountService(storage).list_accounts()[0]
            self.assertEqual(account["registration_engine"], "http")
            self.assertEqual(account["refresh_token"], "test-rt")
            self.assertEqual(account["source_type"], "web")
            self.assertEqual(account["type"], "Plus")
            self.assertNotIn("mail_api_url", account)
            self.assertNotIn("test-secret", json.dumps(account))
            if hasattr(storage, "engine"):
                storage.engine.dispose()

    def test_mailbox_state_error_releases_runner_lock(self):
        with mock.patch("services.register_service._db_backend", return_value=None), mock.patch("services.register_service.runtime_state") as runtime:
            service = RegisterService(Path(self.tmp.name) / "register.json")
            service._config["mail"]["providers"] = [self.entry]
            service._lock_owner = "locked-owner"
            with mock.patch.object(apple, "begin_run", side_effect=apple.AppleMailboxError("disk failed")):
                with self.assertRaises(apple.AppleMailboxError):
                    service._start_runner_locked(reset_runtime=True, recovered=False)
            runtime.release_lock.assert_called_once()
            self.assertFalse(service._lock_owner)
            self.assertFalse(service._config["enabled"])


if __name__ == "__main__":
    unittest.main()
