import unittest
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
