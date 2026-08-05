from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from services.register.mail_provider import TempMailLolProvider, _parse_received_at


class ReceivedAtTests(unittest.TestCase):
    def test_parses_unix_milliseconds(self) -> None:
        parsed = _parse_received_at(1_754_000_000_000)

        self.assertEqual(parsed, datetime.fromtimestamp(1_754_000_000, tz=timezone.utc))

    def test_parses_unix_milliseconds_string(self) -> None:
        parsed = _parse_received_at("1754000000000")

        self.assertEqual(parsed, datetime.fromtimestamp(1_754_000_000, tz=timezone.utc))


class TempMailLolProviderTests(unittest.TestCase):
    def test_wait_for_code_scans_every_email_in_response_batch(self) -> None:
        provider = TempMailLolProvider(
            {"api_key": "", "domain": []},
            {
                "request_timeout": 1,
                "wait_timeout": 1,
                "wait_interval": 0.2,
                "user_agent": "test",
                "proxy": "",
            },
        )
        provider._request = Mock(
            return_value={
                "expired": False,
                "emails": [
                    {
                        "from": "news@example.com",
                        "subject": "A newer unrelated message",
                        "body": "No verification code here.",
                        "date": 1_754_000_002_000,
                    },
                    {
                        "from": "noreply@openai.com",
                        "subject": "Your verification code",
                        "body": "Your verification code is 482913",
                        "date": 1_754_000_001_000,
                    },
                ],
            }
        )

        try:
            code = provider.wait_for_code(
                {
                    "address": "example@tempmail.lol",
                    "token": "secret-token",
                    "_code_requested_at": "2025-07-31T00:00:00+00:00",
                }
            )
        finally:
            provider.close()

        self.assertEqual(code, "482913")
        provider._request.assert_called_once_with("GET", "/inbox", params={"token": "secret-token"})


if __name__ == "__main__":
    unittest.main()
