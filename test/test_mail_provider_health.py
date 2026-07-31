from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api
from services.register import mail_provider
from services.register import openai_register
from services.register.mail_health import MailHealthStore
from services.register_service import RegisterService, _normalize


class MailProviderHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MailHealthStore(Path(self.temporary.name) / "health.json")
        self.db_patch = mock.patch.object(self.store, "_database_backend", return_value=None)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.health_patch = mock.patch.object(mail_provider, "mail_health_store", self.store)
        self.health_patch.start()
        self.addCleanup(self.health_patch.stop)
        mail_provider.mailpit_domain_indexes.clear()

    @staticmethod
    def mail(*providers: dict, threshold: int = 3) -> dict:
        return {
            "request_timeout": 5,
            "wait_timeout": 5,
            "wait_interval": 1,
            "auto_disable": True,
            "failure_threshold": threshold,
            "providers": list(providers),
        }

    def test_mailpit_domain_parsing_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(
            mail_provider.normalize_mailpit_domains(" @A.example.com, b.example.com\nA.EXAMPLE.COM "),
            ["a.example.com", "b.example.com"],
        )

    def test_mailpit_round_robin_and_sequential_selection(self) -> None:
        entry = {"id": "mailpit-main", "domain": ["a.test", "b.test"], "domain_mode": "round_robin"}
        selected = [mail_provider._select_mailpit_domain(entry, auto_disable=True) for _ in range(3)]
        self.assertEqual(selected, ["a.test", "b.test", "a.test"])
        entry["domain_mode"] = "sequential"
        self.assertEqual(mail_provider._select_mailpit_domain(entry, auto_disable=True), "a.test")
        self.store.record_result("mailpit-main", success=False, threshold=1, domain="a.test", domains=["a.test", "b.test"])
        self.assertEqual(mail_provider._select_mailpit_domain(entry, auto_disable=True), "b.test")

    def test_provider_priority_is_strict_and_config_order_breaks_ties(self) -> None:
        config = self.mail(
            {"id": "later", "type": "tempmail_lol", "enable": True, "priority": 20},
            {"id": "first", "type": "duckmail", "enable": True, "priority": 1},
            {"id": "same", "type": "gptmail", "enable": True, "priority": 1},
        )
        self.assertEqual([item["id"] for item in mail_provider._available_entries(config)], ["first", "same", "later"])
        self.assertEqual(mail_provider._next_entry(config)["id"], "first")

    def test_ordinary_failure_does_not_immediately_fall_back_but_next_task_does_after_disable(self) -> None:
        config = self.mail(
            {"id": "primary", "type": "tempmail_lol", "enable": True, "priority": 1},
            {"id": "fallback", "type": "duckmail", "enable": True, "priority": 2},
            threshold=1,
        )
        created: list[str] = []

        class FakeProvider:
            def __init__(self, entry: dict):
                self.entry = entry

            def create_mailbox(self, username=None):
                created.append(self.entry["id"])
                if self.entry["id"] == "primary":
                    raise RuntimeError("primary rejected")
                return {"provider": self.entry["type"], "provider_ref": self.entry["id"], "address": "ok@example.com"}

            def close(self):
                return None

        with mock.patch.object(mail_provider, "_provider_from_entry", side_effect=lambda entry, conf: FakeProvider(entry)):
            with self.assertRaisesRegex(RuntimeError, "primary rejected"):
                mail_provider.create_mailbox(config)
            mailbox = mail_provider.create_mailbox(config)
        self.assertEqual(created, ["primary", "fallback"])
        self.assertEqual(mailbox["_mail_provider_id"], "fallback")

    def test_mailpit_failures_are_per_domain_and_disable_provider_only_when_all_disabled(self) -> None:
        domains = ["a.test", "b.test"]
        for _ in range(2):
            self.store.record_result("mailpit", success=False, threshold=2, error="rejected", domain="a.test", domains=domains)
        state = self.store.provider_state("mailpit")
        self.assertTrue(state["domains"]["a.test"]["disabled"])
        self.assertFalse(state["disabled"])
        self.store.record_result("mailpit", success=False, threshold=2, domain="b.test", domains=domains)
        self.store.record_result("mailpit", success=True, threshold=2, domain="b.test", domains=domains)
        self.assertEqual(self.store.provider_state("mailpit")["domains"]["b.test"]["consecutive_failures"], 0)
        for _ in range(2):
            self.store.record_result("mailpit", success=False, threshold=2, domain="b.test", domains=domains)
        self.assertTrue(self.store.provider_state("mailpit")["disabled"])
        self.store.record_result("mailpit", success=True, threshold=2, domain="a.test", domains=domains)
        self.assertTrue(self.store.provider_state("mailpit")["domains"]["a.test"]["disabled"])

    def test_ordinary_provider_threshold_success_reset_and_disabled_latch(self) -> None:
        self.store.record_result("ordinary", success=False, threshold=2, error="one")
        self.store.record_result("ordinary", success=True, threshold=2)
        self.assertEqual(self.store.provider_state("ordinary")["consecutive_failures"], 0)
        self.store.record_result("ordinary", success=False, threshold=2, error="one")
        self.store.record_result("ordinary", success=False, threshold=2, error="two")
        self.assertTrue(self.store.provider_state("ordinary")["disabled"])
        self.store.record_result("ordinary", success=True, threshold=2)
        self.assertTrue(self.store.provider_state("ordinary")["disabled"])

    def test_outlook_failures_never_enter_provider_health_and_exhaustion_falls_back(self) -> None:
        mailbox = {"provider": "outlook_token", "address": "bad@example.com", "_mail_provider_id": "outlook"}
        with mock.patch.object(mail_provider, "_set_outlook_token_state") as set_state:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=RuntimeError("invalid mailbox"))
        set_state.assert_called_once()
        self.assertEqual(self.store.provider_state("outlook"), {})

        config = self.mail(
            {"id": "outlook", "type": "outlook_token", "enable": True, "priority": 1, "mailboxes": "bad@example.com----p----client----token"},
            {"id": "mailpit", "type": "mailpit", "enable": True, "priority": 2, "domain": ["a.test"], "api_url": "http://mailpit"},
        )
        with mock.patch.object(mail_provider, "_outlook_entry_has_mailbox", return_value=False):
            self.assertEqual(mail_provider._next_entry(config)["id"], "mailpit")

    def test_outlook_many_invalid_mailboxes_continue_until_pool_is_exhausted(self) -> None:
        credentials = [
            {"email": f"bad{index}@example.com", "password": "p", "client_id": "client", "refresh_token": "token"}
            for index in range(100)
        ]
        credentials.append({"email": "good@example.com", "password": "p", "client_id": "client", "refresh_token": "token"})
        state_file = Path(self.temporary.name) / "outlook.json"
        invalid_state = {
            item["email"]: {"state": "failed", "reason": "invalid", "updated_at": ""}
            for item in credentials[:-1]
        }
        with (
            mock.patch.object(mail_provider, "OUTLOOK_TOKEN_USED_FILE", state_file),
            mock.patch.object(mail_provider, "_load_outlook_token_state", return_value=invalid_state),
            mock.patch.object(mail_provider, "_save_outlook_token_state") as save_state,
        ):
            provider = mail_provider.OutlookTokenProvider(
                {"provider_ref": "outlook", "label": "Outlook", "mailboxes": credentials},
                {"request_timeout": 5, "wait_timeout": 5, "wait_interval": 1, "user_agent": "test", "proxy": ""},
            )
            try:
                mailbox = provider.create_mailbox()
            finally:
                provider.close()
        self.assertEqual(mailbox["address"], "good@example.com")
        self.assertEqual(save_state.call_args.args[0]["good@example.com"]["state"], "in_use")

        exhausted_state = {**invalid_state, "good@example.com": {"state": "failed", "reason": "invalid", "updated_at": ""}}
        with mock.patch.object(mail_provider, "_load_outlook_token_state", return_value=exhausted_state):
            self.assertFalse(mail_provider._outlook_entry_has_mailbox({"mailboxes": credentials}))
            self.assertTrue(mail_provider._outlook_entry_has_mailbox({"mailboxes": credentials + [{"email": "new@example.com", "client_id": "client", "refresh_token": "token"}]}))
        with mock.patch.object(mail_provider, "_load_outlook_token_state", return_value={}):
            self.assertTrue(mail_provider._outlook_entry_has_mailbox({"mailboxes": credentials}))

    def test_concurrent_updates_are_not_lost_and_persist(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.store.record_result("parallel", success=False, threshold=100), range(25)))
        self.assertEqual(self.store.provider_state("parallel")["consecutive_failures"], 25)
        reloaded = MailHealthStore(Path(self.temporary.name) / "health.json")
        with mock.patch.object(reloaded, "_database_backend", return_value=None):
            self.assertEqual(reloaded.provider_state("parallel")["consecutive_failures"], 25)

    def test_database_named_config_persistence(self) -> None:
        class FakeDatabase:
            def __init__(self):
                self.values: dict[str, dict] = {}

            def load_named_config(self, key: str):
                return self.values.get(key)

            def save_named_config(self, key: str, value: dict):
                self.values[key] = value

        database = FakeDatabase()
        store = MailHealthStore(Path(self.temporary.name) / "unused.json")
        with mock.patch.object(store, "_database_backend", return_value=database):
            store.record_result("database-provider", success=False, threshold=2)
        reloaded = MailHealthStore(Path(self.temporary.name) / "also-unused.json")
        with mock.patch.object(reloaded, "_database_backend", return_value=database):
            self.assertEqual(reloaded.provider_state("database-provider")["consecutive_failures"], 1)

    def test_manual_reset_scopes(self) -> None:
        self.store.record_result("mailpit", success=False, threshold=1, domain="a.test", domains=["a.test", "b.test"])
        self.assertEqual(self.store.reset("mailpit", "a.test"), 1)
        self.assertNotIn("a.test", self.store.provider_state("mailpit").get("domains", {}))
        self.store.record_result("ordinary", success=False, threshold=1)
        self.assertEqual(self.store.reset("ordinary"), 1)
        self.assertEqual(self.store.provider_state("ordinary"), {})
        self.assertGreaterEqual(self.store.reset(), 1)

    def test_legacy_config_gets_ids_defaults_and_mailpit_domains(self) -> None:
        config = _normalize({"mail": {"providers": [{"type": "mailpit", "enable": True, "domain": "a.test,b.test\na.test"}]}})
        mail = config["mail"]
        self.assertTrue(mail["auto_disable"])
        self.assertEqual(mail["failure_threshold"], 10)
        self.assertEqual(mail["providers"][0]["id"], "mailpit#1")
        self.assertEqual(mail["providers"][0]["priority"], 1)
        self.assertEqual(mail["providers"][0]["domain"], ["a.test", "b.test"])

    def test_all_unavailable_raises_terminal_error(self) -> None:
        config = self.mail({"id": "off", "type": "mailpit", "enable": False, "priority": 1, "domain": ["a.test"]})
        with self.assertRaises(mail_provider.AllMailProvidersUnavailableError):
            mail_provider.create_mailbox(config)

    def test_register_service_stops_on_terminal_mail_result(self) -> None:
        service = RegisterService(Path(self.temporary.name) / "register.json")
        service._config = _normalize({"enabled": True, "engine": "http", "mode": "total", "total": 5, "threads": 1})
        with (
            mock.patch.object(mail_provider, "mail_health_store", self.store),
            mock.patch("services.register_service.openai_register.worker", return_value={"ok": False, "stop_reason": "all_mail_providers_unavailable"}),
            mock.patch.object(service, "_save"),
            mock.patch.object(service, "_save_runtime"),
            mock.patch("services.register_service.trim_memory"),
        ):
            service._run()
        self.assertFalse(service._config["enabled"])
        self.assertEqual(service._config["stats"]["stop_reason"], "all_mail_providers_unavailable")

    def test_http_registration_marks_empty_access_token_as_failure(self) -> None:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.fingerprint = {}
        registrar._platform_authorize = mock.Mock(return_value="signup")
        registrar._register_user = mock.Mock()
        registrar._send_otp = mock.Mock()
        registrar._validate_otp = mock.Mock()
        registrar._open_about_you = mock.Mock()
        registrar._create_account = mock.Mock()
        registrar._exchange_registered_tokens = mock.Mock(return_value={"access_token": ""})
        mailbox = {"provider": "mailpit", "address": "test@example.com"}
        with (
            mock.patch.object(openai_register, "create_mailbox", return_value=mailbox),
            mock.patch.object(openai_register, "wait_for_code", return_value="123456"),
            mock.patch.object(mail_provider, "mark_mailbox_result") as mark_result,
            self.assertRaisesRegex(RuntimeError, "access_token 为空"),
        ):
            registrar.register(1)
        self.assertEqual(mark_result.call_count, 1)
        self.assertFalse(mark_result.call_args.kwargs["success"])

    def test_manual_reset_api_passes_provider_and_domain_scope(self) -> None:
        app = FastAPI()
        app.include_router(register_api.create_router())
        response_config = {"enabled": False}
        with (
            mock.patch.object(register_api, "require_admin"),
            mock.patch.object(register_api.register_service, "reset_mail_health", return_value=response_config) as reset,
        ):
            response = TestClient(app).post(
                "/api/register/mail-health/reset",
                json={"provider_id": "mailpit-main", "domain": "a.test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"register": response_config})
        reset.assert_called_once_with("mailpit-main", "a.test")


if __name__ == "__main__":
    unittest.main()
