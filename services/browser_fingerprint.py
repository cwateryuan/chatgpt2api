from __future__ import annotations

import secrets
import uuid
from typing import Any


REGISTER_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "impersonate": "chrome142",
        "major": "142",
        "full_version": "142.0.0.0",
        "platform_version": "10.0.0",
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "impersonate": "chrome136",
        "major": "136",
        "full_version": "136.0.0.0",
        "platform_version": "10.0.0",
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "impersonate": "chrome131",
        "major": "131",
        "full_version": "131.0.0.0",
        "platform_version": "10.0.0",
        "accept_language": "en-US,en;q=0.9",
    },
)

_ALIASES = {
    "user-agent": "user_agent",
    "oai-device-id": "device_id",
    "oai-session-id": "session_id",
    "sec-ch-ua": "sec_ch_ua",
    "sec-ch-ua-mobile": "sec_ch_ua_mobile",
    "sec-ch-ua-platform": "sec_ch_ua_platform",
    "accept-language": "accept_language",
}


def _chrome_user_agent(major: str, full_version: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full_version} Safari/537.36"
    )


def _chrome_sec_ch_ua(major: str) -> str:
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"'


def complete_fingerprint(
    raw: dict[str, Any] | None = None,
    *,
    generate_ids: bool = True,
) -> dict[str, str]:
    source = raw if isinstance(raw, dict) else {}
    normalized: dict[str, str] = {}
    for key, value in source.items():
        text = str(value or "").strip()
        if text:
            normalized[_ALIASES.get(str(key).lower(), str(key).lower().replace("-", "_"))] = text

    major = str(normalized.get("major") or "142")
    full_version = str(normalized.get("full_version") or f"{major}.0.0.0")
    normalized.setdefault("major", major)
    normalized.setdefault("full_version", full_version)
    normalized.setdefault("user_agent", _chrome_user_agent(major, full_version))
    normalized.setdefault("impersonate", "chrome")
    normalized.setdefault("accept_language", "en-US,en;q=0.9")
    normalized.setdefault("sec_ch_ua", _chrome_sec_ch_ua(major))
    normalized.setdefault("sec_ch_ua_mobile", "?0")
    normalized.setdefault("sec_ch_ua_platform", '"Windows"')
    normalized.setdefault("platform_version", "10.0.0")
    if generate_ids:
        normalized.setdefault("device_id", str(uuid.uuid4()))
        normalized.setdefault("session_id", str(uuid.uuid4()))
    return normalized


def make_fingerprint() -> dict[str, str]:
    return complete_fingerprint(dict(secrets.choice(REGISTER_BROWSER_PROFILES)))


def account_fingerprint(account: dict[str, Any] | None, *, generate_ids: bool = True) -> dict[str, str]:
    account = account if isinstance(account, dict) else {}
    raw = dict(account.get("fp") or {}) if isinstance(account.get("fp"), dict) else {}
    canonical_keys = {
        _ALIASES.get(str(key).lower(), str(key).lower().replace("-", "_"))
        for key in raw
    }
    for legacy_key in (*_ALIASES, "impersonate", "user_agent", "device_id", "session_id"):
        value = account.get(legacy_key)
        canonical_key = _ALIASES.get(legacy_key, legacy_key.replace("-", "_"))
        if value and canonical_key not in canonical_keys:
            raw[legacy_key] = value
            canonical_keys.add(canonical_key)
    return complete_fingerprint(raw, generate_ids=generate_ids)
