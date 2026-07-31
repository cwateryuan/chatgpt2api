import unittest
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=types.SimpleNamespace()))
config_stub = types.ModuleType("services.config")
config_stub.DATA_DIR = Path(__file__).resolve().parent
sys.modules.setdefault("services.config", config_stub)

from services.register import mail_provider


class FakeOutlookProvider(mail_provider.OutlookTokenProvider):
    def __init__(self, messages):
        self.messages = messages
        self.conf = {"wait_timeout": 0.3, "wait_interval": 0.05}
        self.provider_ref = "test"

    def fetch_recent_messages(self, mailbox):
        return list(self.messages)


class OutlookCodeTests(unittest.TestCase):
    def test_outlook_alias_supported_domains(self):
        for domain in ("outlook.com", "hotmail.com", "live.com", "msn.com", "hotmail.co.uk", "outlook.de"):
            with self.subTest(domain=domain):
                self.assertTrue(mail_provider.outlook_alias_supported(f"user@{domain}"))
        for domain in ("gmail.com", "example.com", "live.co.uk", "notoutlook.com"):
            with self.subTest(domain=domain):
                self.assertFalse(mail_provider.outlook_alias_supported(f"user@{domain}"))

    def test_alias_expansion_is_optional_sanitized_deduplicated_and_capped(self):
        credential = {"email": "user+old@outlook.com", "password": "p", "client_id": "client", "refresh_token": "token"}
        self.assertEqual(mail_provider.expand_outlook_aliases([credential], {}), [credential])

        expanded = mail_provider.expand_outlook_aliases(
            [credential],
            {"alias_enabled": True, "alias_per_email": 2, "alias_prefix": " c2 api! ", "alias_include_original": True},
        )
        self.assertEqual([item["email"] for item in expanded], ["user+old@outlook.com", "user+c2api1@outlook.com", "user+c2api2@outlook.com"])
        self.assertEqual(expanded[1]["login_email"], "user+old@outlook.com")
        self.assertEqual(expanded[1]["refresh_token"], "token")

        aliases_only = mail_provider.expand_outlook_aliases(
            [{**credential, "email": "user@outlook.com"}, {**credential, "email": "user@outlook.com"}],
            {"alias_enabled": True, "alias_per_email": 999, "alias_prefix": "***", "alias_include_original": False},
        )
        self.assertEqual(len(aliases_only), 200)
        self.assertEqual(aliases_only[0]["email"], "user+c2api1@outlook.com")
        self.assertEqual(aliases_only[-1]["email"], "user+c2api200@outlook.com")

    def test_alias_state_is_independent_but_parent_token_invalid_is_inherited(self):
        original = {"email": "user@outlook.com", "client_id": "client", "refresh_token": "token"}
        alias_one, alias_two = mail_provider.expand_outlook_aliases(
            [original],
            {"alias_enabled": True, "alias_per_email": 2, "alias_include_original": False},
        )
        store = {alias_one["email"]: {"state": "used"}}
        self.assertFalse(mail_provider._outlook_credential_available(store, alias_one))
        self.assertTrue(mail_provider._outlook_credential_available(store, alias_two))

        store[original["email"]] = {"state": "token_invalid"}
        self.assertEqual(mail_provider._outlook_credential_state(store, alias_two), "token_invalid")
        self.assertFalse(mail_provider._outlook_credential_available(store, alias_two))

        store[original["email"]] = {"state": "used"}
        self.assertTrue(mail_provider._outlook_credential_available(store, alias_two))

    def test_prune_keeps_base_credential_when_an_alias_has_state(self):
        credential = {"email": "user@hotmail.com", "password": "p", "client_id": "client", "refresh_token": "token"}
        entry = {"alias_enabled": True, "alias_per_email": 2, "alias_prefix": "reg", "alias_include_original": False}
        store = {"user+reg2@hotmail.com": {"state": "failed"}}
        with mock.patch.object(mail_provider, "_load_outlook_token_state", return_value=store):
            kept, removed = mail_provider.prune_outlook_unused_credentials([credential], entry)
        self.assertEqual(kept, [credential])
        self.assertEqual(removed, 0)

    def test_graph_normalization_includes_recipients(self):
        provider = object.__new__(mail_provider.OutlookTokenProvider)
        item = {
            "id": "message-1",
            "subject": "code",
            "toRecipients": [{"emailAddress": {"address": "user+c2api1@outlook.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "copy@example.com"}}],
            "body": {"contentType": "text", "content": "Verification code: 123456"},
        }
        normalized = provider._normalize_graph_item({"address": "user+c2api1@outlook.com"}, item)
        self.assertEqual(normalized["to"], ["user+c2api1@outlook.com", "copy@example.com"])

    def test_imap_authenticates_with_original_login_email(self):
        captured: dict[str, bytes] = {}

        class FakeImap:
            def __init__(self, host):
                self.host = host

            def authenticate(self, mechanism, callback):
                captured["auth"] = callback(None)
                return "OK", []

            def select(self, mailbox, readonly=True):
                return "OK", []

            def uid(self, command, *args):
                return "OK", [b""]

            def logout(self):
                return "OK", []

        provider = object.__new__(mail_provider.OutlookTokenProvider)
        provider.imap_host = "outlook.office365.com"
        provider.message_limit = 10
        mailbox = {"address": "user+c2api1@outlook.com", "login_email": "user@outlook.com"}
        with mock.patch.object(mail_provider.imaplib, "IMAP4_SSL", FakeImap):
            self.assertEqual(provider._imap_messages(mailbox, "access-token"), [])
        self.assertIn(b"user=user@outlook.com", captured["auth"])
        self.assertNotIn(b"user=user+c2api1@outlook.com", captured["auth"])

    def test_alias_wait_for_code_requires_matching_recipient(self):
        now = datetime.now(timezone.utc)
        mailbox = {
            "address": "user+c2api2@outlook.com",
            "login_email": "user@outlook.com",
            "_code_requested_at": now - timedelta(seconds=1),
        }
        provider = FakeOutlookProvider(
            [
                {"provider": "outlook_token", "message_id": "missing", "subject": "Verification code: 111111", "received_at": now},
                {"provider": "outlook_token", "message_id": "other", "to": ["user+c2api1@outlook.com"], "subject": "Verification code: 222222", "received_at": now},
                {"provider": "outlook_token", "message_id": "target", "to": ["user+c2api2@outlook.com"], "subject": "Verification code: 333333", "received_at": now},
            ]
        )
        self.assertEqual(provider.wait_for_code(mailbox), "333333")

    def test_token_failure_marks_alias_and_login_mailbox(self):
        mailbox = {"provider": "outlook_token", "address": "user+c2api1@outlook.com", "login_email": "user@outlook.com"}
        with mock.patch.object(mail_provider, "_set_outlook_token_state") as set_state:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=mail_provider.OutlookTokenError("refresh failed"))
        self.assertEqual([call.args[:2] for call in set_state.call_args_list], [("user+c2api1@outlook.com", "token_invalid"), ("user@outlook.com", "token_invalid")])

    def test_wait_for_code_ignores_messages_before_current_otp_request(self):
        requested_at = datetime(2026, 6, 18, 3, 15, tzinfo=timezone.utc)
        mailbox = {"address": "user@example.com", "_code_requested_at": requested_at}
        provider = FakeOutlookProvider(
            [
                {
                    "provider": "outlook_token",
                    "mailbox": "user@example.com",
                    "message_id": "old",
                    "subject": "OpenAI verification code",
                    "text_content": "Verification code: 521839",
                    "html_content": "",
                    "received_at": requested_at - timedelta(minutes=5),
                },
                {
                    "provider": "outlook_token",
                    "mailbox": "user@example.com",
                    "message_id": "new",
                    "subject": "OpenAI verification code",
                    "text_content": "Verification code: 942731",
                    "html_content": "",
                    "received_at": requested_at + timedelta(seconds=15),
                },
            ]
        )

        self.assertEqual(provider.wait_for_code(mailbox), "942731")


if __name__ == "__main__":
    unittest.main()
