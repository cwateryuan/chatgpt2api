from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from services.sentinel_solver_client import js_timezone_offset_min, solver_timezone


class _HealthResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class EgressTimezoneTests(unittest.TestCase):
    def test_js_offset_matches_browser_sign(self):
        # Date.getTimezoneOffset() is the inverse of the UTC offset: Shanghai
        # is UTC+8, so the conversation field is -480. Los Angeles is UTC-7 in
        # this date, so the field is 420.
        summer = datetime(2026, 7, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
        self.assertEqual(int(summer.utcoffset().total_seconds() // 60), -420)
        self.assertEqual(js_timezone_offset_min("America/Los_Angeles"), 420)
        self.assertEqual(js_timezone_offset_min("Asia/Shanghai"), -480)
        self.assertIsNone(js_timezone_offset_min("not a zone"))
        self.assertIsNone(js_timezone_offset_min("Mars/Olympus"))

    def test_solver_timezone_follows_health(self):
        response = _HealthResponse({
            "user_agent": "solver-ua",
            "timezone": "America/Los_Angeles",
            "timezone_source": "egress",
            "egress_ip": "1.2.3.4",
        })
        with (
            mock.patch.dict(os.environ, {"CHATGPT2API_SENTINEL_SOLVER_URL": "http://solver"}),
            mock.patch("services.sentinel_solver_client.requests.get", return_value=response),
            mock.patch("services.sentinel_solver_client._user_agent_cache", {
                "url": "",
                "value": "",
                "timezone": "",
                "timezone_offset_min": None,
                "expires_at": 0.0,
            }),
        ):
            self.assertEqual(solver_timezone(), ("America/Los_Angeles", 420))

    def test_geo_payload_accepts_warp_timezone_and_rejects_junk(self):
        from sentinel_solver.egress_timezone import timezone_from_payload

        zone, ip = timezone_from_payload({
            "success": True,
            "ip": "104.28.0.1",
            "timezone": {"id": "America/Los_Angeles"},
        })
        self.assertEqual((zone, ip), ("America/Los_Angeles", "104.28.0.1"))
        zone, ip = timezone_from_payload({
            "status": "success",
            "query": "1.1.1.1",
            "timezone": "America/Los_Angeles",
        })
        self.assertEqual((zone, ip), ("America/Los_Angeles", "1.1.1.1"))
        self.assertEqual(timezone_from_payload({"success": False, "timezone": "America/Los_Angeles"}), ("", ""))
        self.assertEqual(timezone_from_payload({"timezone": "Not A Zone", "ip": "1.1.1.1"}), ("", ""))
        self.assertEqual(timezone_from_payload(["nope"]), ("", ""))

    def test_configured_timezone_is_not_overwritten_by_lookup(self):
        from sentinel_solver import egress_timezone

        with mock.patch.object(egress_timezone, "detect_egress_timezone") as detect:
            zone, source, ip = egress_timezone.resolve_page_timezone("Asia/Tokyo", "socks5://warp:1080")
        detect.assert_not_called()
        self.assertEqual((zone, source, ip), ("Asia/Tokyo", "configured", ""))

    def test_empty_timezone_uses_egress_lookup(self):
        from sentinel_solver import egress_timezone

        with mock.patch.object(
            egress_timezone,
            "detect_egress_timezone",
            return_value=("America/Los_Angeles", "8.8.8.8"),
        ):
            zone, source, ip = egress_timezone.resolve_page_timezone("", "socks5://warp:1080")
        self.assertEqual((zone, source, ip), ("America/Los_Angeles", "egress", "8.8.8.8"))
        self.assertEqual(json.loads(json.dumps({"timezone": zone}))["timezone"], "America/Los_Angeles")


if __name__ == "__main__":
    unittest.main()
