"""OpenAI Sentinel Token (PoW) 生成与请求工具函数。

用于密码登录、注册等需要 sentinel token 的流程。
"""
from __future__ import annotations

import base64
import json
import random
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from utils.turnstile import solve_turnstile_token

if TYPE_CHECKING:
    from curl_cffi.requests import Session

DEFAULT_SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"
DEFAULT_SCREEN = "1920x1080"
NAVIGATOR_KEYS = (
    "vendorSub-undefined",
    "productSub-20030107",
    "plugins-[object PluginArray]",
    "mimeTypes-[object MimeTypeArray]",
    "hardwareConcurrency-8",
    "cookieEnabled-true",
    "pdfViewerEnabled-true",
)
DOCUMENT_KEYS = ("location", "implementation", "URL", "documentURI", "compatMode", "hidden", "visibilityState")
WINDOW_KEYS = ("Object", "Function", "Array", "Number", "parseFloat", "undefined", "Reflect", "performance")


class SentinelTokenGenerator:
    """Sentinel Token 生成器（PoW - Proof of Work）。"""

    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str, env: dict[str, Any] | None = None):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())
        self.env = dict(env or {})

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _language(self) -> str:
        language = str(self.env.get("language") or self.env.get("accept_language") or "en-US").strip()
        if "," in language:
            language = language.split(",", 1)[0].strip()
        if ";" in language:
            language = language.split(";", 1)[0].strip()
        return language or "en-US"

    def _screen(self) -> str:
        screen = str(self.env.get("screen") or "").strip()
        if screen:
            return screen
        width = str(self.env.get("screen_width") or "").strip()
        height = str(self.env.get("screen_height") or "").strip()
        if width.isdigit() and height.isdigit():
            return f"{width}x{height}"
        return DEFAULT_SCREEN

    def _cores(self) -> int:
        raw = self.env.get("hardware_concurrency") or self.env.get("cores")
        try:
            value = int(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
        return random.choice([4, 8, 12, 16])

    def _sdk_url(self) -> str:
        return str(self.env.get("sdk_url") or DEFAULT_SENTINEL_SDK_URL).strip() or DEFAULT_SENTINEL_SDK_URL

    def _local_time_string(self) -> str:
        tz_name = str(self.env.get("timezone") or "").strip()
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                now = datetime.now(ZoneInfo(tz_name))
                offset = now.utcoffset() or timezone.utc.utcoffset(None)
                total_seconds = int(offset.total_seconds()) if offset is not None else 0
                sign = "+" if total_seconds >= 0 else "-"
                total_seconds = abs(total_seconds)
                hours, rem = divmod(total_seconds, 3600)
                minutes = rem // 60
                tz_label = tz_name.replace("_", " ")
                return (
                    f"{now.strftime('%a %b %d %Y %H:%M:%S')} "
                    f"GMT{sign}{hours:02d}{minutes:02d} ({tz_label})"
                )
            except Exception:
                pass
        return time.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
            time.gmtime(),
        )

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            self._screen(),
            self._local_time_string(),
            4294705152,
            random.random(),
            self.user_agent,
            self._sdk_url(),
            None,
            None,
            self._language(),
            random.random(),
            random.choice(NAVIGATOR_KEYS),
            random.choice(DOCUMENT_KEYS),
            random.choice(WINDOW_KEYS),
            perf_now,
            self.sid,
            "",
            self._cores(),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


# ── 默认 User-Agent 和 sec-ch-ua ──────────────────────────────
DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
DEFAULT_SENTINEL_SEC_CH_UA = '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'


def apply_oai_sc_cookie(session: "Session", oai_sc_value: str) -> None:
    """Persist oai-sc on OpenAI auth cookie domains used by registration/login."""
    value = str(oai_sc_value or "").strip()
    if not value:
        return
    for domain in (".openai.com", "openai.com", ".auth.openai.com", "auth.openai.com"):
        try:
            session.cookies.set("oai-sc", value, domain=domain)
        except Exception:
            continue


def _platform_header(env: dict[str, Any] | None) -> str:
    raw = str((env or {}).get("sec_ch_ua_platform") or (env or {}).get("platform") or "Windows").strip()
    if not raw:
        return '"Windows"'
    if raw.startswith('"') and raw.endswith('"'):
        return raw
    return f'"{raw}"'


def _mobile_header(env: dict[str, Any] | None) -> str:
    raw = str((env or {}).get("sec_ch_ua_mobile") or "?0").strip()
    return raw if raw in {"?0", "?1"} else "?0"


def _request_sentinel_payload(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    env: dict[str, Any] | None = None,
    turnstile_wait: bool = False,
) -> tuple[str, str, str]:
    """Return (sentinel_header_value, so_token, oai_sc_cookie_value)."""
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    runtime_env = dict(env or {})
    generator = SentinelTokenGenerator(device_id, ua, runtime_env)
    requirements_token = generator.generate_requirements_token()
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": ua,
            "sec-ch-ua": ch_ua,
            "sec-ch-ua-mobile": _mobile_header(runtime_env),
            "sec-ch-ua-platform": _platform_header(runtime_env),
            "accept-language": str(runtime_env.get("accept_language") or runtime_env.get("language") or "en-US,en;q=0.9"),
        },
        timeout=20,
        verify=False,
    )

    try:
        data = resp.json() if resp.text else {}
    except Exception:
        fallback = json.dumps(
            {"p": generator.generate_requirements_token(), "t": "", "c": "", "id": device_id, "flow": flow},
            separators=(",", ":"),
        )
        return fallback, "", ""

    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")

    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )

    turnstile_data = data.get("turnstile") or {}
    so_token = ""
    if turnstile_data.get("required") and turnstile_data.get("dx"):
        if turnstile_wait:
            # Jittered wait to avoid fixed 5000ms clustering while still mimicking collection time.
            time.sleep(random.uniform(3.8, 7.2))
        so_token = solve_turnstile_token(str(turnstile_data.get("dx") or ""), requirements_token) or ""
        if not so_token:
            raise RuntimeError("sentinel_so_token_failed")

    sentinel_value = json.dumps(
        {"p": p_value, "t": so_token, "c": token, "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    oai_sc_value = "0" + token
    return sentinel_value, so_token, oai_sc_value


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    env: dict[str, Any] | None = None,
    apply_cookie: bool = False,
) -> tuple[str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "password_verify", "username_password_create" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖
        env: 可选的浏览器环境（language/timezone/screen/cores 等）
        apply_cookie: 为 True 时自动写入 oai-sc cookie

    Returns:
        (openai-sentinel-token header value, oai-sc cookie value) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    sentinel_value, _so_token, oai_sc_value = _request_sentinel_payload(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        env=env,
        turnstile_wait=False,
    )
    if apply_cookie:
        apply_oai_sc_cookie(session, oai_sc_value)
    return sentinel_value, oai_sc_value


def build_sentinel_with_so_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    env: dict[str, Any] | None = None,
    apply_cookie: bool = False,
) -> tuple[str, str, str]:
    """请求 sentinel token 并返回 (sentinel_header_value, so_token_header_value, oai_sc_cookie_value)。

    Args:
        session: curl_cffi Session 实例
        device_id: 设备 ID
        flow: 流程标识（如 "oauth_create_account" 等）
        user_agent: 可选的 User-Agent 覆盖
        sec_ch_ua: 可选的 sec-ch-ua 覆盖
        env: 可选的浏览器环境（language/timezone/screen/cores 等）
        apply_cookie: 为 True 时自动写入 oai-sc cookie

    Returns:
        (openai-sentinel-token, openai-sentinel-so-token, oai-sc cookie) 元组

    Raises:
        RuntimeError: sentinel 请求失败
    """
    sentinel_value, so_token, oai_sc_value = _request_sentinel_payload(
        session,
        device_id,
        flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        env=env,
        turnstile_wait=True,
    )
    if apply_cookie:
        apply_oai_sc_cookie(session, oai_sc_value)
    return sentinel_value, so_token, oai_sc_value
