from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from services.account_service import account_service
from services.browser_fingerprint import make_fingerprint
from services.memory import trim_memory
from services.proxy_service import ClearanceBundle, proxy_settings
from services.register import mail_provider
from utils.log import logger

base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
    "mode": "total",
    "browser_token_mode": "session",
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads", "browser_token_mode") if key in saved_config})
except Exception:
    pass


def registration_proxy_pool(value: object) -> list[str]:
    proxies: list[str] = []
    seen: set[str] = set()
    for line in str(value or "").splitlines():
        proxy = line.strip()
        if proxy and proxy not in seen:
            proxies.append(proxy)
            seen.add(proxy)
    return proxies


def normalize_registration_proxy_text(value: object) -> str:
    return "\n".join(registration_proxy_pool(value))


def select_registration_proxy(value: object, index: int) -> str:
    proxies = registration_proxy_pool(value)
    if not proxies:
        return ""
    task_index = max(1, int(index or 1))
    return proxies[(task_index - 1) % len(proxies)]

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
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


def _chrome_user_agent(major: str, full_version: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full_version} Safari/537.36"
    )


def _chrome_sec_ch_ua(major: str) -> str:
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"'


def _chrome_sec_ch_ua_full_version_list(major: str, full_version: str) -> str:
    return (
        f'"Chromium";v="{full_version}", '
        f'"Google Chrome";v="{full_version}", '
        '"Not_A Brand";v="99.0.0.0"'
    )


def _complete_browser_fingerprint(profile: dict[str, str]) -> dict[str, str]:
    major = str(profile.get("major") or "142").strip()
    full_version = str(profile.get("full_version") or f"{major}.0.0.0").strip()
    return {
        **profile,
        "major": major,
        "full_version": full_version,
        "user_agent": str(profile.get("user_agent") or _chrome_user_agent(major, full_version)),
        "sec_ch_ua": str(profile.get("sec_ch_ua") or _chrome_sec_ch_ua(major)),
        "sec_ch_ua_full_version_list": str(
            profile.get("sec_ch_ua_full_version_list") or _chrome_sec_ch_ua_full_version_list(major, full_version)
        ),
        "accept_language": str(profile.get("accept_language") or "en-US,en;q=0.9"),
        "platform_version": str(profile.get("platform_version") or "10.0.0"),
        "impersonate": str(profile.get("impersonate") or "chrome"),
    }


DEFAULT_BROWSER_FINGERPRINT = _complete_browser_fingerprint(REGISTER_BROWSER_PROFILES[0])
user_agent = DEFAULT_BROWSER_FINGERPRINT["user_agent"]
sec_ch_ua = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua"]
sec_ch_ua_full_version_list = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua_full_version_list"]
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None

common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}


def _browser_fingerprint(fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    return _complete_browser_fingerprint(fingerprint or DEFAULT_BROWSER_FINGERPRINT)


def _header_fingerprint(headers: dict[str, str], fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    fp = _browser_fingerprint(fingerprint)
    next_headers = dict(headers)
    next_headers["user-agent"] = fp["user_agent"]
    next_headers["sec-ch-ua"] = fp["sec_ch_ua"]
    if "sec-ch-ua-full-version-list" in next_headers:
        next_headers["sec-ch-ua-full-version-list"] = fp["sec_ch_ua_full_version_list"]
    if "sec-ch-ua-platform-version" in next_headers:
        next_headers["sec-ch-ua-platform-version"] = f'"{fp["platform_version"]}"'
    if "accept-language" in next_headers:
        next_headers["accept-language"] = fp["accept_language"]
    return next_headers


def _extract_chrome_version_from_user_agent(value: str) -> tuple[str, str]:
    ua = str(value or "")
    for marker in ("Chrome/", "Chromium/", "Edg/"):
        if marker not in ua:
            continue
        tail = ua.split(marker, 1)[1]
        version = tail.split(" ", 1)[0].strip()
        major = version.split(".", 1)[0].strip()
        if major.isdigit():
            return major, version or f"{major}.0.0.0"
    return "", ""


def _fingerprint_with_user_agent(fingerprint: dict[str, str] | None, value: str) -> dict[str, str]:
    ua = str(value or "").strip()
    if not ua:
        return _browser_fingerprint(fingerprint)
    fp = _browser_fingerprint(fingerprint)
    major, full_version = _extract_chrome_version_from_user_agent(ua)
    major = major or fp["major"]
    full_version = full_version or f"{major}.0.0.0"
    return {
        **fp,
        "major": major,
        "full_version": full_version,
        "user_agent": ua,
        "sec_ch_ua": _chrome_sec_ch_ua(major),
        "sec_ch_ua_full_version_list": _chrome_sec_ch_ua_full_version_list(major, full_version),
    }


def _make_browser_fingerprint() -> dict[str, str]:
    return _complete_browser_fingerprint(make_fingerprint())


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    if not logger._enabled("info"):
        return
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


from utils.pkce import generate_pkce as _generate_pkce  # noqa: F401


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(data, ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={str(getattr(resp, 'text', '') or '')[:limit]}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )


def _mail_config(proxy: str | None = None) -> dict:
    return {**config["mail"], "proxy": config["proxy"] if proxy is None else proxy}


def _authorize_landed_page(resp) -> str:
    """诊断用：粗判 authorize 之后落在哪个页面。返回 signup / login / "" 仅供日志。

    注意：email-verification / email_otp_verification 在注册和登录流程里都会出现，
    无法据此可靠区分，所以这里只用于打日志，绝不据此中断注册流程。
    """
    if resp is None:
        return ""
    final_url = str(getattr(resp, "url", "") or "").lower()
    data = _response_json(resp)
    page_type = ""
    page = data.get("page") if isinstance(data, dict) else None
    if isinstance(page, dict):
        page_type = str(page.get("type") or "").lower()
    if "create-account" in final_url or "signup" in final_url or "create_account" in page_type:
        return "signup"
    if "/log-in" in final_url or "/login" in final_url or page_type in {"login", "password_verification"}:
        return "login"
    return ""


def _safe_url_for_log(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "-"
    try:
        parsed = urlparse(value)
    except Exception:
        return value[:160]
    if parsed.scheme and parsed.netloc:
        return f"{parsed.netloc}{parsed.path}"[:160]
    return parsed.path[:160] if parsed.path else value[:160]


def _url_path(url: str) -> str:
    value = str(url or "").strip()
    if value.startswith("/"):
        return urlparse(f"https://auth.openai.com{value}").path.rstrip("/") or "/"
    try:
        return urlparse(value).path.rstrip("/") or "/"
    except Exception:
        return ""


def create_mailbox(username: str | None = None, *, proxy: str | None = None) -> dict:
    return mail_provider.create_mailbox(_mail_config(proxy), username)


def wait_for_code(mailbox: dict, *, proxy: str | None = None) -> str | None:
    return mail_provider.wait_for_code(_mail_config(proxy), mailbox)


from utils.sentinel import (
    SentinelTokenGenerator,
    build_sentinel_token as _build_sentinel_token_tuple,
    build_sentinel_with_so_token,
)


def build_sentinel_token(
    session: requests.Session,
    device_id: str,
    flow: str,
    fingerprint: dict[str, str] | None = None,
) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    fp = _browser_fingerprint(fingerprint)
    sentinel_val, _oai_sc_val = _build_sentinel_token_tuple(
        session,
        device_id,
        flow,
        user_agent=fp["user_agent"],
        sec_ch_ua=fp["sec_ch_ua"],
    )
    return sentinel_val


def create_session(proxy: str = "", fingerprint: dict[str, str] | None = None) -> Any:
    fp = _browser_fingerprint(fingerprint)
    kwargs = proxy_settings.build_session_kwargs(
        proxy=proxy,
        upstream=True,
        impersonate=fp["impersonate"] if fingerprint else "chrome",
        verify=False,
    )
    session = requests.Session(**kwargs)
    session.headers.update({"user-agent": fp["user_agent"]})
    return session


def _apply_clearance_to_session(session: requests.Session, bundle: ClearanceBundle | None) -> None:
    if bundle is None:
        return
    if bundle.user_agent:
        session.headers["User-Agent"] = bundle.user_agent
        session.headers["user-agent"] = bundle.user_agent
    for name, value in bundle.cookies.items():
        try:
            session.cookies.set(name, value, domain=f".{bundle.target_host or 'openai.com'}")
            session.cookies.set(name, value, domain=bundle.target_host or "auth.openai.com")
        except Exception:
            continue


def _headers_with_clearance(
    headers: dict[str, str],
    target_url: str,
    proxy: str = "",
    user_agent_override: str = "",
    clearance_scope: str = "",
) -> dict[str, str]:
    merged = proxy_settings.build_headers(
        headers=headers,
        target_url=target_url,
        proxy=proxy,
        upstream=True,
        clearance_scope=clearance_scope,
    )
    normalized = {str(key): str(value) for key, value in merged.items()}
    if user_agent_override:
        ua_key = next((key for key in normalized if key.lower() == "user-agent"), "user-agent")
        normalized[ua_key] = user_agent_override
    return normalized


def _cloudflare_block_message(resp, prefix: str = "被 Cloudflare 拦截", reason: str = "") -> str:
    status = getattr(resp, "status_code", "unknown")
    debug = _response_debug_detail(resp)
    reason = reason or "clearance 刷新失败或重试后仍失败，请更换 IP/代理重试"
    return f"{prefix}，{reason}: status={status}, {debug}"


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def validate_otp(session: requests.Session, device_id: str, code: str, fingerprint: dict[str, str] | None = None) -> tuple[Any, str]:
    headers = _header_fingerprint(common_headers, fingerprint)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue", fingerprint)
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def request_platform_oauth_token(
    session: requests.Session,
    code: str,
    code_verifier: str,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    headers = _header_fingerprint({
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": user_agent,
    }, fingerprint)
    resp = session.post(
        f"{auth_base}/api/accounts/oauth/token",
        headers=headers,
        json={
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
        },
        verify=False,
        timeout=60,
    )
    if resp.status_code != 200:
        logger.warning({
            "event": "platform_oauth_token_failed",
            "status_code": resp.status_code,
            "body_preview": str(resp.text or "")[:500],
        })
        return None
    return _response_json(resp)


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.fingerprint = _make_browser_fingerprint()
        self.session = create_session(self.proxy, self.fingerprint)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = self.fingerprint["device_id"]
        self.session_id = self.fingerprint["session_id"]
        self.code_verifier = ""
        self.platform_auth_code = ""

    def close(self) -> None:
        self.session.close()

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = _header_fingerprint(navigate_headers, self.fingerprint)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = _header_fingerprint(common_headers, self.fingerprint)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _refresh_cloudflare_clearance(self, target_url: str, index: int) -> ClearanceBundle | None:
        self.clearance_failure_reason = ""
        profile = proxy_settings.get_profile(proxy=self.proxy, upstream=True) if hasattr(proxy_settings, "get_profile") else None
        if profile is not None and not profile.clearance_enabled:
            self.clearance_failure_reason = (
                "可尝试使用 FlareSolverr 清障方式，注意需要 Docker 部署 flaresolverr、privoxy、warp-proxy 等相关容器"
            )
            step(index, f"检测到 Cloudflare 拦截，{self.clearance_failure_reason}", "yellow")
            return None
        step(index, "检测到 Cloudflare 拦截，尝试刷新 clearance", "yellow")
        bundle = proxy_settings.refresh_clearance(
            target_url=target_url,
            proxy=self.proxy,
            force=True,
            upstream=True,
            clearance_scope=self.device_id,
        )
        if bundle is not None:
            _apply_clearance_to_session(self.session, bundle)
            self.clearance_user_agent = bundle.user_agent or self.clearance_user_agent
            if bundle.user_agent:
                self.fingerprint = _fingerprint_with_user_agent(self.fingerprint, bundle.user_agent)
            step(index, "Cloudflare clearance 刷新完成，重试当前请求", "yellow")
        else:
            self.clearance_failure_reason = "clearance 刷新未返回可用 Cookie，请检查 FlareSolverr URL、代理和出口 IP"
            step(index, f"Cloudflare clearance 刷新失败：{self.clearance_failure_reason}", "yellow")
        return bundle

    def _platform_authorize(self, email: str, index: int, screen_hint: str = "signup") -> str:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        target_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        headers = self._navigate_headers(f"{platform_base}/")
        headers = _headers_with_clearance(headers, target_url, self.proxy, self.clearance_user_agent, self.device_id)
        resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            retry_headers = _headers_with_clearance(self._navigate_headers(f"{platform_base}/"), target_url, self.proxy, self.clearance_user_agent, self.device_id)
            resp, error = request_with_local_retry(self.session, "get", target_url, headers=retry_headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        landed = _authorize_landed_page(resp)
        step(index, f"platform authorize 完成[{landed or '?'}] url={str(getattr(resp, 'url', '') or '')[:160]}")
        return landed

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create", self.fingerprint)
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent, self.device_id)
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create", self.fingerprint)
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent, self.device_id)
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        url = f"{auth_base}/api/accounts/email-otp/send"
        headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent, self.device_id)
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent, self.device_id)
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code, self.fingerprint)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")

    def _open_about_you(self, index: int) -> None:
        step(index, "进入账号资料页")
        url = f"{auth_base}/about-you"
        headers = _headers_with_clearance(
            self._navigate_headers(f"{auth_base}/email-verification"),
            url,
            self.proxy,
            self.clearance_user_agent,
            self.device_id,
        )
        resp, error = request_with_local_retry(
            self.session,
            "get",
            url,
            headers=headers,
            allow_redirects=True,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"about_you_http_{getattr(resp, 'status_code', 'unknown')}")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._json_headers(f"{auth_base}/about-you")

        fp = _browser_fingerprint(self.fingerprint)
        sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
            self.session,
            self.device_id,
            "oauth_create_account",
            user_agent=fp["user_agent"],
            sec_ch_ua=fp["sec_ch_ua"],
        )

        headers["openai-sentinel-token"] = sentinel_token
        if so_token:
            headers["openai-sentinel-so-token"] = so_token

        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent, self.device_id)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/about-you")

            sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
                self.session,
                self.device_id,
                "oauth_create_account",
                user_agent=fp["user_agent"],
                sec_ch_ua=fp["sec_ch_ua"],
            )

            headers["openai-sentinel-token"] = sentinel_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token

            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent, self.device_id)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        callback_params = extract_oauth_callback_params_from_url(str(data.get("continue_url") or "").strip())
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        if not self.platform_auth_code:
            continue_hint = str(data.get("continue_url") or getattr(resp, "headers", {}).get("Location") or getattr(resp, "url", "") or "")
            raise RuntimeError(f"create_account_missing_callback: continue={_safe_url_for_log(continue_hint)}")
        step(index, "创建账号资料完成")

    def _reset_auth_cookies(self) -> None:
        jar = getattr(self.session.cookies, "jar", self.session.cookies)
        for cookie in list(jar):
            domain = str(getattr(cookie, "domain", "") or "")
            if "auth.openai.com" not in domain:
                continue
            try:
                self.session.cookies.delete(
                    str(getattr(cookie, "name", "") or ""),
                    domain=domain,
                    path=str(getattr(cookie, "path", "/") or "/"),
                )
            except Exception:
                continue
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

    def _authorize_continue_login(self, email: str, index: int) -> dict:
        step(index, "提交 Microsoft 邮箱进入登录验证")
        url = f"{auth_base}/api/accounts/authorize/continue"

        def send():
            headers = self._json_headers(f"{auth_base}/log-in?usernameKind=email")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "authorize_continue", self.fingerprint)
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent, self.device_id)
            return request_with_local_retry(
                self.session,
                "post",
                url,
                json={"username": {"kind": "email", "value": email}},
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = send()
        if resp is not None and resp.status_code == 409:
            step(index, "登录会话过期，重新发起登录授权", "yellow")
            self._reset_auth_cookies()
            self._platform_authorize(email, index, screen_hint="login_or_signup")
            resp, error = send()
        if resp is None or resp.status_code != 200:
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"login_continue_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")
        data = _response_json(resp)
        if ((data.get("page") or {}).get("payload") or {}).get("passwordless_disabled"):
            raise RuntimeError("Microsoft 邮箱登录流不支持 passwordless，请换邮箱或使用已有密码重登")
        return data

    def _send_passwordless_otp(self, index: int) -> None:
        step(index, "发送 Microsoft 登录验证码")
        url = f"{auth_base}/api/accounts/passwordless/send-otp"
        headers = self._json_headers(f"{auth_base}/log-in/password")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent, self.device_id)
        resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, allow_redirects=False, verify=False)
        if resp is None or resp.status_code not in (200, 201, 204):
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"passwordless_send_otp_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")

    @staticmethod
    def _is_passwordless_invalid_state(resp) -> bool:
        if resp is None or getattr(resp, "status_code", None) != 409:
            return False
        data = _response_json(resp)
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return False
        code = str(error.get("code") or "").strip().lower()
        message = str(error.get("message") or "").strip().lower()
        return code == "invalid_state" or "sign-in session is no longer valid" in message

    def _passwordless_login(self, email: str, mailbox: dict, index: int) -> dict:
        if str(mailbox.get("provider") or "") != "outlook_token":
            raise RuntimeError("OpenAI 返回登录流，当前邮箱来源无法读取 Microsoft 登录验证码")
        step(index, "OpenAI 返回登录流，转入 Microsoft passwordless 登录", "yellow")
        for attempt in range(2):
            if attempt:
                step(index, "Microsoft 登录会话失效，重新发起 passwordless 登录", "yellow")
                self._reset_auth_cookies()
                self._platform_authorize(email, index, screen_hint="login_or_signup")
            self._authorize_continue_login(email, index)
            mailbox["_code_requested_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            self._send_passwordless_otp(index)
            step(index, "开始等待 Microsoft 登录验证码")
            code = wait_for_code(mailbox, proxy=self.proxy)
            if not code:
                raise RuntimeError("等待 Microsoft 登录验证码超时")
            step(index, f"收到 Microsoft 登录验证码: {code}")
            resp, error = validate_otp(self.session, self.device_id, code, self.fingerprint)
            if resp is not None and resp.status_code == 200:
                break
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            if attempt == 0 and self._is_passwordless_invalid_state(resp):
                continue
            raise RuntimeError(error or f"passwordless_validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        data = _response_json(resp)
        continue_url = str(data.get("continue_url") or "").strip() or f"{auth_base}/sign-in-with-chatgpt/platform/consent"
        if _url_path(continue_url) == "/about-you":
            first_name, last_name = _random_name()
            step(index, "Microsoft 登录验证完成，需要完善账号资料")
            self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            return self._exchange_registered_tokens(index)
        step(index, "Microsoft 登录验证完成，开始换 token")
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier, self.fingerprint)
        if not tokens:
            raise RuntimeError("Microsoft passwordless token 换取失败")
        step(index, "Microsoft passwordless token 换取完成")
        return tokens

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier, self.fingerprint)
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox(proxy=self.proxy)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            password = _random_password()
            first_name, last_name = _random_name()
            landed = self._platform_authorize(email, index)
            source_type = "web"
            if landed == "login":
                tokens = self._passwordless_login(email, mailbox, index)
                password = ""
                source_type = "microsoft"
            else:
                self._register_user(email, password, index)
                mailbox["_code_requested_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                self._send_otp(index)
                step(index, "开始等待注册验证码")
                code = wait_for_code(mailbox, proxy=self.proxy)
                if not code:
                    raise RuntimeError("等待注册验证码超时")
                step(index, f"收到注册验证码: {code}")
                self._validate_otp(code, index)
                self._open_about_you(index)
                self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
                tokens = self._exchange_registered_tokens(index)
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        result = {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "source_type": source_type,
            "registration_engine": "http",
            "fp": dict(self.fingerprint),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not result["access_token"]:
            error = RuntimeError("token换取成功但 access_token 为空")
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise error
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return result


def worker(index: int) -> dict:
    start = time.time()
    proxies = registration_proxy_pool(config.get("proxy"))
    proxy = select_registration_proxy(config.get("proxy"), index)
    registrar = PlatformRegistrar(proxy)
    try:
        proxy_position = (max(1, int(index)) - 1) % len(proxies) + 1 if proxies else 0
        step(index, f"任务启动 proxy={proxy_position}/{len(proxies)}" if proxies else "任务启动 proxy=none")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        result.setdefault("status", "正常")
        result.setdefault("quota", 0)
        result.setdefault("image_quota_unknown", True)
        account_service.add_account_items([result])
        if str(config.get("mode") or "total") == "quota":
            try:
                from services.openai_backend_api import OpenAIBackendAPI
                with OpenAIBackendAPI(access_token) as backend:
                    quota_info = backend.get_image_quota_info()
                account_service.update_account(access_token, quota_info, quiet=True)
            except Exception as error:
                step(index, f"账号已保存，额度初始化暂未成功: {error}", "yellow")
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except mail_provider.AllMailProvidersUnavailableError as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 停止，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e), "stop_reason": e.stop_reason}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
