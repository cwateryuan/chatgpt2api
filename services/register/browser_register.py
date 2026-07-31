from __future__ import annotations

import importlib.metadata
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from services.browser_fingerprint import make_fingerprint
from services.proxy_service import ClearanceBundle, proxy_settings
from services.register import browser_devtools, mail_provider
from services.register.openai_register import (
    _fingerprint_with_user_agent,
    _random_birthdate,
    _random_name,
    _random_password,
    auth_base,
    config,
    platform_auth0_client,
    platform_oauth_audience,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
    request_platform_oauth_token,
    step,
)
from utils.pkce import generate_pkce


BROWSER_NAVIGATION_TIMEOUT_MS = 45_000
BROWSER_TASK_TIMEOUT_SECONDS = 300
BROWSER_ERROR_RETRY_LIMIT = 2
BROWSER_ONE_TIME_CODE_RETRY_LIMIT = 3
BROWSER_AUTH_RESTART_LIMIT = 3
CHATGPT_BASE = "https://chatgpt.com"
CHATGPT_LOGIN_URL = f"{CHATGPT_BASE}/auth/login"
CHATGPT_SESSION_URL = f"{CHATGPT_BASE}/api/auth/session"
BROWSER_EMAIL_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[name="identifier"]',
    'input[autocomplete="username"]',
    'input[data-testid="email-input"]',
    'input[placeholder*="Email" i]',
    '#email',
)
BROWSER_ONE_TIME_CODE_SELECTORS = (
    'button:has-text("one-time code")',
    'a:has-text("one-time code")',
    '[role="button"]:has-text("one-time code")',
    'button:has-text("one time code")',
    'a:has-text("one time code")',
    'button:has-text("verification code")',
    'a:has-text("verification code")',
    'button:has-text("一次性")',
    'a:has-text("一次性")',
    'button:has-text("验证码")',
    'a:has-text("验证码")',
)
BROWSER_CHALLENGE_MARKERS = (
    "verify you are human",
    "checking your browser",
    "just a moment",
    "turnstile",
    "cf-chl-",
    "attention required",
)
_runtime_lock = threading.Lock()
_runtime_cache: dict[str, Any] | None = None


DEVTOOLS_STATE_JS = r"""
JSON.stringify({
  url: location.href,
  title: document.title,
  body: (document.body?.innerText || document.documentElement?.innerText || "").trim().slice(0, 5000),
  inputs: [...document.querySelectorAll("input,textarea")].map((element) => ({
    type: element.type || "",
    name: element.name || "",
    id: element.id || "",
    placeholder: element.placeholder || "",
    aria: element.getAttribute("aria-label") || "",
    autocomplete: element.autocomplete || "",
    inputmode: element.inputMode || "",
    maxLength: element.maxLength,
    visible: !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length)
  })),
  buttons: [...document.querySelectorAll("button,a,[role=button],input[type=submit],input[type=button]")]
    .filter((element) => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length))
    .map((element) => ({
      tag: element.tagName,
      text: (element.innerText || element.textContent || element.value || element.getAttribute("aria-label") || "")
        .trim().replace(/\s+/g, " ").slice(0, 160),
      type: element.type || "",
      id: element.id || ""
    })).slice(0, 60)
})
"""


DEVTOOLS_FINGERPRINT_JS = r"""
(async () => {
  const userAgentData = navigator.userAgentData || null;
  let highEntropy = {};
  if (userAgentData && userAgentData.getHighEntropyValues) {
    try {
      highEntropy = await userAgentData.getHighEntropyValues([
        "architecture", "bitness", "fullVersionList", "model", "platformVersion", "uaFullVersion"
      ]);
    } catch (_) {}
  }
  const storage = {};
  for (const storageName of ["localStorage", "sessionStorage"]) {
    try {
      const source = window[storageName];
      for (let index = 0; index < source.length; index += 1) {
        const key = source.key(index);
        if (key) storage[key] = source.getItem(key) || "";
      }
    } catch (_) {}
  }
  const storedUuid = (pattern) => {
    for (const [key, value] of Object.entries(storage)) {
      if (pattern.test(key) && /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(value)) return value;
    }
    return "";
  };
  return JSON.stringify({
    user_agent: navigator.userAgent || "",
    platform: (userAgentData && userAgentData.platform) || navigator.platform || "",
    mobile: !!(userAgentData && userAgentData.mobile),
    brands: (userAgentData && userAgentData.brands) || [],
    full_version_list: highEntropy.fullVersionList || [],
    ua_full_version: highEntropy.uaFullVersion || "",
    platform_version: highEntropy.platformVersion || "",
    architecture: highEntropy.architecture || "",
    bitness: highEntropy.bitness || "",
    model: highEntropy.model || "",
    language: navigator.language || "",
    languages: navigator.languages || [],
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    timezone_offset_min: new Date().getTimezoneOffset(),
    screen_width: screen.width || 0,
    screen_height: screen.height || 0,
    page_width: innerWidth || 0,
    page_height: innerHeight || 0,
    pixel_ratio: devicePixelRatio || 1,
    hardware_concurrency: navigator.hardwareConcurrency || 0,
    device_memory: navigator.deviceMemory || 0,
    stored_device_id: storedUuid(/device|oai.did/i),
    stored_session_id: storedUuid(/session/i)
  });
})()
"""


DEVTOOLS_CLICK_CONSENT_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => !!element && !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  const controls = [...document.querySelectorAll("button,a,[role=button],input[type=submit]")].filter(visible);
  const text = (element) => (element.innerText || element.textContent || element.value || "").trim();
  const target = controls.find((element) => /^(allow|agree|authorize|confirm|accept|同意|授权|确认)$/i.test(text(element)))
    || controls.find((element) => /allow|agree|authorize|confirm|accept|同意|授权|确认/i.test(text(element)))
    || controls.find((element) => /^(continue|next|继续|下一步)$/i.test(text(element)));
  if (!target) throw new Error("oauth_consent_button_missing");
  target.click();
  await sleep(1000);
  return JSON.stringify({url: location.href, title: document.title});
})()
"""


def _devtools_submit_email_js(email: str) -> str:
    return f"""
(async () => {{
  const email = {json.dumps(email)};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => {{
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }};
  const setValue = (element, value) => {{
    element.focus();
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value")
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.value = value;
    element.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: value }}));
    element.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};
  const input = [...document.querySelectorAll("input")]
    .find((element) => visible(element) && ((element.type || "").toLowerCase() === "email" || element.name === "email" || element.id === "email"));
  if (!input) throw new Error("email_input_missing");
  setValue(input, email);
  await sleep(300);
  const buttons = [...document.querySelectorAll("button,input[type=submit]")].filter(visible);
  const submit = buttons.find((element) => (element.innerText || element.value || "").trim() === "继续")
    || buttons.find((element) => (element.innerText || element.value || "").trim().toLowerCase() === "continue")
    || buttons.find((element) => String(element.type || "").toLowerCase() === "submit");
  if (!submit) throw new Error("email_submit_missing");
  submit.click();
  await sleep(2500);
  const codeLogin = [...document.querySelectorAll("button,a,[role=button]")].filter(visible)
    .find((element) => {{
      const text = (element.innerText || element.textContent || element.value || element.getAttribute("aria-label") || "").trim().toLowerCase();
      return text.includes("one-time code") || text.includes("one time code") || text.includes("verification code")
        || text.includes("一次性") || text.includes("验证码") || text.includes("驗證碼");
    }});
  if (codeLogin) {{
    codeLogin.click();
    await sleep(1500);
  }}
  return JSON.stringify({{ url: location.href, title: document.title }});
}})()
"""


DEVTOOLS_CLICK_ONE_TIME_CODE_JS = r"""
(async () => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  const textOf = (element) => (element.innerText || element.textContent || element.value || element.getAttribute("aria-label") || "")
    .trim().replace(/\s+/g, " ").toLowerCase();
  const target = [...document.querySelectorAll("button,a,[role=button],input[type=button],input[type=submit]")]
    .filter(visible)
    .find((element) => {
      const text = textOf(element);
      return text.includes("one-time code") || text.includes("one time code") || text.includes("verification code")
        || text.includes("一次性") || text.includes("验证码") || text.includes("驗證碼");
    });
  if (!target) throw new Error("one_time_code_button_missing");
  target.scrollIntoView({ block: "center", inline: "center" });
  target.click();
  await sleep(1500);
  return JSON.stringify({ url: location.href, title: document.title });
})()
"""


def _devtools_submit_code_js(code: str) -> str:
    return f"""
(async () => {{
  const code = {json.dumps(code)};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => {{
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }};
  const setValue = (element, value) => {{
    element.focus();
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value")
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.value = value;
    element.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: value }}));
    element.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};
  const input = [...document.querySelectorAll("input")]
    .find((element) => visible(element) && (element.name === "code" || /code/i.test(element.id || "")
      || /code/i.test(element.autocomplete || "") || (element.placeholder || "").includes("验证码") || element.maxLength === 6));
  if (!input) throw new Error("code_input_missing");
  setValue(input, code);
  await sleep(300);
  const buttons = [...document.querySelectorAll("button,input[type=submit]")].filter(visible);
  const submit = buttons.find((element) => (element.innerText || element.value || "").trim() === "继续")
    || buttons.find((element) => (element.innerText || element.value || "").trim().toLowerCase() === "continue")
    || buttons.find((element) => String(element.type || "").toLowerCase() === "submit");
  if (!submit) throw new Error("code_submit_missing");
  submit.click();
  await sleep(1000);
  return JSON.stringify({{ url: location.href, title: document.title }});
}})()
"""


def _devtools_submit_profile_js(name: str, birthdate: str) -> str:
    year, month, day = birthdate.split("-")
    today = datetime.now(timezone.utc).date()
    born = datetime.strptime(birthdate, "%Y-%m-%d").date()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return f"""
(async () => {{
  const profileName = {json.dumps(name)};
  const birthdate = {json.dumps(birthdate)};
  const age = {json.dumps(str(age))};
  const year = {json.dumps(year)};
  const month = {json.dumps(month)};
  const day = {json.dumps(day)};
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => !!element && !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  const setValue = (element, value) => {{
    element.focus();
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value")
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.value = value;
    element.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: value }}));
    element.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};
  const inputs = [...document.querySelectorAll("input")].filter(visible);
  const nameInput = inputs.find((element) => element.name === "name" || element.name === "fullName"
    || element.autocomplete === "name" || /full name|姓名/i.test(element.placeholder || element.getAttribute("aria-label") || ""));
  if (nameInput) setValue(nameInput, profileName);
  const ageInput = inputs.find((element) => element.name === "age" || element.id === "age"
    || /(^|\\s)age(\\s|$)|年龄/i.test(`${{element.name}} ${{element.id}} ${{element.placeholder}} ${{element.getAttribute("aria-label") || ""}}`));
  const birthInput = inputs.find((element) => element.type === "date" || /birth|birthday/i.test(element.name || element.id || ""));
  if (ageInput) {{
    setValue(ageInput, age);
  }} else if (birthInput) {{
    setValue(birthInput, birthdate);
  }} else {{
    const numeric = inputs.filter((element) => (element.inputMode || "").toLowerCase() === "numeric" && element.maxLength !== 6);
    const findPart = (pattern) => numeric.find((element) => pattern.test(`${{element.name}} ${{element.id}} ${{element.placeholder}} ${{element.getAttribute("aria-label") || ""}}`));
    const monthInput = findPart(/month|月份/i) || numeric[0];
    const dayInput = findPart(/day|日期|日/i) || numeric[1];
    const yearInput = findPart(/year|年份|年/i) || numeric[2];
    if (monthInput && dayInput && yearInput) {{
      setValue(monthInput, month);
      setValue(dayInput, day);
      setValue(yearInput, year);
    }}
  }}
  await sleep(300);
  const buttons = [...document.querySelectorAll("button,input[type=submit],[role=button]")].filter(visible);
  const submit = buttons.find((element) => /finish creating account/i.test((element.innerText || element.value || "").trim()))
    || buttons.find((element) => (element.innerText || element.value || "").trim().toLowerCase() === "continue")
    || buttons.find((element) => (element.innerText || element.value || "").trim() === "继续")
    || buttons.find((element) => /agree|accept|confirm|同意|确认/i.test((element.innerText || element.value || "").trim()))
    || buttons.find((element) => String(element.type || "").toLowerCase() === "submit");
  if (!submit) throw new Error("profile_submit_missing");
  submit.click();
  await sleep(1000);
  return JSON.stringify({{ url: location.href, title: document.title }});
}})()
"""


def _devtools_visible_inputs(state: dict[str, Any]) -> list[dict[str, Any]]:
    inputs = state.get("inputs") if isinstance(state.get("inputs"), list) else []
    return [item for item in inputs if isinstance(item, dict) and item.get("visible")]


def _devtools_is_logged_in(state: dict[str, Any]) -> bool:
    url = str(state.get("url") or "")
    body = str(state.get("body") or "")
    return url.rstrip("/") == CHATGPT_BASE or url.startswith(f"{CHATGPT_BASE}/?") or "Message ChatGPT" in body


def _devtools_is_password_page(state: dict[str, Any]) -> bool:
    url = str(state.get("url") or "")
    body = str(state.get("body") or "")
    title = str(state.get("title") or "")
    return (
        "/log-in/password" in url
        or "Enter your password" in title
        or "Enter your password" in body
        or "Log in with a one-time code" in body
        or "Sign up with a one-time code" in body
    )


def _devtools_is_code_page(state: dict[str, Any]) -> bool:
    url = str(state.get("url") or "")
    body = str(state.get("body") or "")
    has_code_input = any(
        str(item.get("name") or "").lower() == "code"
        or "code" in str(item.get("id") or "").lower()
        or "one-time-code" in str(item.get("autocomplete") or "").lower()
        or int(item.get("maxLength") or 0) == 6
        for item in _devtools_visible_inputs(state)
    )
    return ("email-verification" in url or "verification" in body.lower() or "检查你的收件箱" in body) and has_code_input


def _devtools_is_profile_page(state: dict[str, Any]) -> bool:
    url = str(state.get("url") or "")
    body = str(state.get("body") or "").lower()
    return "/about-you" in url or ("full name" in body and ("birthday" in body or "date of birth" in body))


class BrowserRegistrationError(RuntimeError):
    pass


def _sanitized_error(error: BaseException) -> str:
    text = str(error or error.__class__.__name__)
    text = re.sub(r"([?&](?:login_hint|username|email)=)[^&\s]+", r"\1***", text, flags=re.I)
    text = re.sub(
        r"((?:https?|socks5h?|socks)://)([^\s/@:]+):([^\s/@]+)@",
        r"\1***:***@",
        text,
        flags=re.I,
    )
    return text[:500]


def _probe_browser_runtime() -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright

        executable_override = str(os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or "").strip()
        with sync_playwright() as playwright:
            executable = executable_override or playwright.chromium.executable_path
            if not executable or not Path(executable).is_file():
                raise RuntimeError("Chromium executable is not installed")
            browser = playwright.chromium.launch(
                executable_path=executable_override or None,
                headless=_playwright_headless(),
                args=["--disable-dev-shm-usage"],
            )
            try:
                version = str(browser.version or importlib.metadata.version("playwright"))
            finally:
                browser.close()
        return {
            "browser_available": True,
            "browser_version": version,
            "browser_error": "",
        }
    except Exception as error:
        return {
            "browser_available": False,
            "browser_version": "",
            "browser_error": _sanitized_error(error),
        }


def browser_runtime_status(*, refresh: bool = False) -> dict[str, Any]:
    global _runtime_cache
    with _runtime_lock:
        if _runtime_cache is not None and not refresh:
            return dict(_runtime_cache)

        result: dict[str, Any] = {}

        def probe() -> None:
            result.update(_probe_browser_runtime())

        # Playwright's sync API refuses to run on FastAPI's asyncio thread.
        probe_thread = threading.Thread(target=probe, name="browser-runtime-probe", daemon=True)
        probe_thread.start()
        probe_thread.join()
        if not result:
            result = {
                "browser_available": False,
                "browser_version": "",
                "browser_error": "Browser runtime probe did not return a result",
            }
        _runtime_cache = result
        return dict(_runtime_cache)


def _mail_config() -> dict:
    return {**config["mail"], "proxy": config["proxy"]}


def _playwright_proxy() -> dict[str, str] | None:
    profile = proxy_settings.get_profile(proxy=str(config.get("proxy") or ""), upstream=True)
    value = str(profile.proxy_url or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        from urllib.parse import unquote

        result["username"] = unquote(parsed.username)
        result["password"] = unquote(parsed.password or "")
    return result


def _playwright_headless() -> bool:
    value = str(os.getenv("PLAYWRIGHT_HEADLESS") or "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _browser_auth_cookies(bundle: ClearanceBundle | None, device_id: str) -> list[dict[str, Any]]:
    parsed = urlparse(auth_base)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    cookies: list[dict[str, Any]] = []
    if bundle is not None:
        for name, value in bundle.cookies.items():
            if name and value:
                cookies.append({"name": str(name), "value": str(value), "url": base_url})
    if device_id and not any(item["name"] == "oai-did" for item in cookies):
        cookies.append({"name": "oai-did", "value": device_id, "url": base_url})
    return cookies


def _align_fingerprint_platform(fingerprint: dict[str, str]) -> dict[str, str]:
    result = dict(fingerprint)
    user_agent = str(result.get("user_agent") or "").lower()
    if "windows" in user_agent:
        result["sec_ch_ua_platform"] = '"Windows"'
    elif "android" in user_agent:
        result["sec_ch_ua_platform"] = '"Android"'
    elif "iphone" in user_agent or "ipad" in user_agent:
        result["sec_ch_ua_platform"] = '"iOS"'
    elif "mac os x" in user_agent or "macintosh" in user_agent:
        result["sec_ch_ua_platform"] = '"macOS"'
    elif "linux" in user_agent:
        result["sec_ch_ua_platform"] = '"Linux"'
    return result


class BrowserRegistrar:
    def __init__(self) -> None:
        self.fingerprint = make_fingerprint()
        self.callback_code = ""
        self._deadline = 0.0
        self._auth_responses: list[str] = []
        self._clearance_bundle: ClearanceBundle | None = None
        self.registration_proxy_url = ""
        self.registration_proxy_username = ""
        self.registration_proxy_password = ""

    def _load_clearance(self, index: int, *, force: bool = True) -> ClearanceBundle | None:
        proxy = str(config.get("proxy") or "")
        profile = proxy_settings.get_profile(proxy=proxy, upstream=True)
        clearance_mode = profile.clearance_mode if profile.clearance_enabled else "disabled"
        step(index, f"浏览器网络 proxy={profile.proxy_source} clearance={clearance_mode}")
        if not profile.clearance_enabled:
            return None
        bundle = proxy_settings.refresh_clearance(
            target_url=auth_base,
            proxy=proxy,
            force=force,
            upstream=True,
            clearance_scope=f"browser:{self.fingerprint['device_id']}",
        )
        if bundle is None:
            step(index, "浏览器 clearance 获取失败，请检查 FlareSolverr、代理和出口一致性", "yellow")
            return None
        self._clearance_bundle = bundle
        step(
            index,
            f"浏览器 clearance 已加载 cookies={len(bundle.cookies)} "
            f"cf_clearance={'yes' if bundle.cookies.get('cf_clearance') else 'no'} "
            f"user_agent={'yes' if bundle.user_agent else 'no'}",
            "yellow",
        )
        return bundle

    def _remaining_ms(self) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserRegistrationError("browser_task_timeout")
        return max(1, min(BROWSER_NAVIGATION_TIMEOUT_MS, int(remaining * 1000)))

    def _check_challenge(self, page) -> None:
        title = str(page.title() or "").lower()
        body = ""
        try:
            body = str(page.locator("body").inner_text(timeout=2_000) or "").lower()[:5000]
        except Exception:
            pass
        if any(marker in f"{title}\n{body}" for marker in BROWSER_CHALLENGE_MARKERS):
            raise BrowserRegistrationError("browser_interactive_challenge")
        try:
            if page.locator('iframe[src*="turnstile"], iframe[src*="challenge"], iframe[src*="captcha"]').count():
                raise BrowserRegistrationError("browser_interactive_challenge")
        except BrowserRegistrationError:
            raise
        except Exception:
            pass

    def _visible(self, page, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                matches = page.locator(selector)
                if matches.count() < 1:
                    continue
                locator = matches.first
                if locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def _editable(self, page, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                matches = page.locator(selector)
                for match_index in range(matches.count()):
                    locator = matches.nth(match_index)
                    if locator.is_visible() and locator.is_editable():
                        return locator
            except Exception:
                continue
        return None

    @staticmethod
    def _page_path(page) -> str:
        try:
            parsed = urlparse(str(page.url or ""))
            return (parsed.path.rstrip("/") or "/")[:120]
        except Exception:
            return "unknown"

    def _wait_for_transition(self, page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=min(5_000, self._remaining_ms()))
        except Exception:
            pass
        page.wait_for_timeout(750)
        self._check_challenge(page)

    def _wait_for_initial_hydration(self, page) -> None:
        try:
            page.wait_for_load_state("load", timeout=min(5_000, self._remaining_ms()))
        except Exception:
            pass
        page.wait_for_timeout(2_500)
        self._check_challenge(page)

    def _control_summary(self, page) -> str:
        inputs: list[str] = []
        buttons: list[str] = []
        try:
            fields = page.locator("input")
            for field_index in range(min(fields.count(), 10)):
                field = fields.nth(field_index)
                if not field.is_visible():
                    continue
                attrs = []
                for key in (
                    "type", "name", "autocomplete", "inputmode", "maxlength", "data-testid",
                    "readonly", "disabled", "aria-readonly",
                ):
                    value = str(field.get_attribute(key) or "").strip()
                    if value:
                        attrs.append(f"{key}={value[:40]}")
                inputs.append("{" + ",".join(attrs) + "}")
        except Exception:
            pass
        try:
            controls = page.locator("button")
            for button_index in range(min(controls.count(), 8)):
                button = controls.nth(button_index)
                if not button.is_visible():
                    continue
                label = " ".join(str(button.inner_text() or "").split())[:40]
                label = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "***", label, flags=re.I)
                button_type = str(button.get_attribute("type") or "").strip()
                buttons.append("{" + ",".join(filter(None, (f"type={button_type}" if button_type else "", f"text={label}" if label else ""))) + "}")
        except Exception:
            pass
        return f"inputs=[{','.join(inputs)}] buttons=[{','.join(buttons)}]"[:800]

    def _record_auth_response(self, response) -> None:
        try:
            parsed = urlparse(str(response.url or ""))
            auth_host = urlparse(auth_base).hostname
            chatgpt_host = urlparse(CHATGPT_BASE).hostname
            resource_type = str(response.request.resource_type or "")
            if parsed.hostname not in {auth_host, chatgpt_host} or resource_type not in {"document", "fetch", "xhr"}:
                return
            path = (parsed.path.rstrip("/") or "/")[:120]
            status = int(response.status)
            if status < 400 and not path.startswith(("/api/accounts/", "/api/auth/")):
                return
            entry = f"{status}:{path}"
            if not self._auth_responses or self._auth_responses[-1] != entry:
                self._auth_responses.append(entry)
                self._auth_responses = self._auth_responses[-8:]
        except Exception:
            pass

    def _error_summary(self, page) -> str:
        messages: list[str] = []
        try:
            controls = page.locator('[role="alert"], main h1, main h2, main p')
            for control_index in range(min(controls.count(), 8)):
                control = controls.nth(control_index)
                if not control.is_visible():
                    continue
                message = " ".join(str(control.inner_text() or "").split())[:120]
                message = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "***", message, flags=re.I)
                if message and message not in messages:
                    messages.append(message)
        except Exception:
            pass
        responses = ",".join(self._auth_responses[-5:])
        return f"text=[{' | '.join(messages)}] responses=[{responses}]"[:800]

    def _fill(self, page, selectors: tuple[str, ...], value: str, state: str) -> None:
        locator = self._editable(page, selectors)
        if locator is None:
            raise BrowserRegistrationError(f"browser_unexpected_state:{state}")
        locator.fill(value, timeout=self._remaining_ms())

    def _continue(self, page, state: str) -> None:
        locator = self._visible(page, (
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Create account")',
            'button:has-text("Sign up")',
            'button:has-text("Verify")',
            'button:has-text("Agree")',
        ))
        if locator is None:
            raise BrowserRegistrationError(f"browser_unexpected_state:{state}_continue")
        locator.click(timeout=self._remaining_ms())
        self._wait_for_transition(page)

    def _submit_email(self, page, email: str, index: int) -> None:
        original_url = str(page.url or "")
        email_input = self._editable(page, BROWSER_EMAIL_SELECTORS)
        if email_input is None:
            raise BrowserRegistrationError("browser_unexpected_state:email")
        email_input.fill(email, timeout=self._remaining_ms())
        self._continue(page, "email")
        for _ in range(8):
            self._capture_callback(page.url)
            if self.callback_code:
                return
            self._check_challenge(page)
            if str(page.url or "") != original_url:
                return
            if self._editable(page, BROWSER_EMAIL_SELECTORS) is None:
                return
            page.wait_for_timeout(500)
        path = self._page_path(page)
        step(
            index,
            f"浏览器邮箱提交未跳转 path={path} {self._control_summary(page)} {self._error_summary(page)}",
            "yellow",
        )
        raise BrowserRegistrationError(f"browser_email_transition_timeout:path={path}")

    @staticmethod
    def _registration_url() -> str:
        return CHATGPT_LOGIN_URL

    def _capture_callback(self, url: str) -> None:
        parsed = urlparse(str(url or ""))
        if parsed.netloc.lower() != "platform.openai.com" or parsed.path.rstrip("/") != "/auth/callback":
            return
        self.callback_code = str((parse_qs(parsed.query).get("code") or [""])[0]).strip()

    def _registration_complete(self, page) -> bool:
        self._capture_callback(page.url)
        if self.callback_code:
            return True
        parsed = urlparse(str(page.url or ""))
        return (
            parsed.hostname == urlparse(CHATGPT_BASE).hostname
            and parsed.path.rstrip("/") == ""
        )

    @staticmethod
    def _find_access_token(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("accessToken", "access_token"):
                token = str(payload.get(key) or "").strip()
                if token:
                    return token
            for value in payload.values():
                token = BrowserRegistrar._find_access_token(value)
                if token:
                    return token
        elif isinstance(payload, list):
            for value in payload:
                token = BrowserRegistrar._find_access_token(value)
                if token:
                    return token
        return ""

    def _chatgpt_session_tokens(self, page, index: int) -> dict[str, str]:
        step(index, "ChatGPT 注册完成，读取浏览器 session token")
        last_status = 0
        for _ in range(10):
            result = page.evaluate(
                """
                async (sessionUrl) => {
                    const response = await fetch(sessionUrl, {
                        credentials: "include",
                        cache: "no-store",
                    });
                    let data = null;
                    try { data = await response.json(); } catch (_) {}
                    return {status: response.status, data};
                }
                """,
                CHATGPT_SESSION_URL,
            )
            if isinstance(result, dict):
                last_status = int(result.get("status") or 0)
                access_token = self._find_access_token(result.get("data"))
                if access_token:
                    return {
                        "access_token": access_token,
                        "refresh_token": "",
                        "id_token": "",
                    }
            page.wait_for_timeout(1_000)
        raise BrowserRegistrationError(f"browser_session_token_missing:http_{last_status or 'unknown'}")

    def _devtools_timeout(self, limit: float = BROWSER_NAVIGATION_TIMEOUT_MS / 1000) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserRegistrationError("browser_task_timeout")
        return max(1.0, min(float(limit), remaining))

    def _devtools_state(self, port: int) -> dict[str, Any]:
        state = browser_devtools.evaluate_json(
            port,
            DEVTOOLS_STATE_JS,
            timeout=self._devtools_timeout(20),
            hosts=("chatgpt.com", "auth.openai.com", "platform.openai.com"),
        )
        title = str(state.get("title") or "")
        body = str(state.get("body") or "")
        haystack = f"{title}\n{body}".lower()
        if any(marker in haystack for marker in BROWSER_CHALLENGE_MARKERS):
            raise BrowserRegistrationError("browser_interactive_challenge")
        terminal_markers = {
            "account_deactivated": "account_deactivated",
            "deleted or deactivated": "account_deleted_or_deactivated",
            "account has been deleted": "account_deleted_or_deactivated",
            "account is disabled": "account_disabled",
        }
        for marker, reason in terminal_markers.items():
            if marker in haystack:
                raise BrowserRegistrationError(f"terminal_auth_error:{reason}")
        return state

    @staticmethod
    def _devtools_state_summary(state: dict[str, Any]) -> str:
        path = urlparse(str(state.get("url") or "")).path or "/"
        inputs = [
            "{" + ",".join(filter(None, (
                f"type={str(item.get('type') or '')[:20]}" if item.get("type") else "",
                f"name={str(item.get('name') or '')[:30]}" if item.get("name") else "",
                f"autocomplete={str(item.get('autocomplete') or '')[:30]}" if item.get("autocomplete") else "",
                f"maxlength={item.get('maxLength')}" if item.get("maxLength") not in (None, -1) else "",
            ))) + "}"
            for item in _devtools_visible_inputs(state)[:8]
        ]
        buttons = state.get("buttons") if isinstance(state.get("buttons"), list) else []
        safe_buttons = [
            "{" + ",".join(filter(None, (
                f"type={str(item.get('type') or '')[:20]}" if item.get("type") else "",
                f"text={re.sub(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', '***', ' '.join(str(item.get('text') or '').split()), flags=re.I)[:40]}" if item.get("text") else "",
            ))) + "}"
            for item in buttons[:8]
            if isinstance(item, dict)
        ]
        return f"path={path[:120]} inputs=[{','.join(inputs)}] buttons=[{','.join(safe_buttons)}]"[:800]

    def _launch_devtools_browser(self, profile_dir: Path, index: int) -> tuple[subprocess.Popen[Any], int]:
        executable = browser_devtools.find_browser_executable()
        port = browser_devtools.free_local_port()
        profile = proxy_settings.get_profile(proxy=str(config.get("proxy") or ""), upstream=False)
        proxy_url = str(profile.proxy_url or "").strip()
        if not proxy_url:
            raise BrowserRegistrationError("browser_registration_proxy_missing")
        try:
            proxy_server, proxy_username, proxy_password = browser_devtools.browser_proxy_config(proxy_url)
        except RuntimeError as error:
            raise BrowserRegistrationError(str(error)) from error
        self.registration_proxy_url = proxy_url
        self.registration_proxy_username = proxy_username
        self.registration_proxy_password = proxy_password
        step(
            index,
            f"浏览器网络 proxy={profile.proxy_source} proxy_server=yes "
            f"proxy_auth={'yes' if proxy_username else 'no'} engine=chrome-devtools clearance=disabled",
        )
        command = [
            executable,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-quic",
            "--disable-features=UseDnsHttpsSvcb,AsyncDns",
            "--window-size=1365,768",
            "--new-window",
        ]
        if _playwright_headless():
            command.extend(["--headless=new", "--disable-gpu"])
        command.append(f"--proxy-server={proxy_server}")
        command.append("about:blank")
        process_options: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            process_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(command, **process_options)
        try:
            browser_devtools.wait_for_devtools(port, self._devtools_timeout(30))
        except Exception:
            browser_devtools.close_browser(port, process)
            raise
        return process, port

    def _devtools_access_token(self, port: int) -> str:
        last_status = "empty"
        for _ in range(10):
            text = browser_devtools.response_body_for_request(
                port,
                CHATGPT_SESSION_URL,
                timeout=self._devtools_timeout(20),
                hosts=("chatgpt.com",),
            )
            try:
                payload = json.loads(text)
            except Exception:
                payload = None
                last_status = "invalid_json"
            access_token = self._find_access_token(payload)
            if access_token:
                return access_token
            time.sleep(1)
        raise BrowserRegistrationError(f"browser_session_token_missing:{last_status}")

    def _platform_authorize_url(self, email: str, state: str, code_challenge: str) -> str:
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": str(self.fingerprint.get("device_id") or ""),
            "screen_hint": "login_or_signup",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": state,
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        return f"{auth_base}/api/accounts/authorize?{urlencode(params)}"

    @staticmethod
    def _oauth_consent_visible(state: dict[str, Any]) -> bool:
        path = urlparse(str(state.get("url") or "")).path.lower()
        buttons = state.get("buttons") if isinstance(state.get("buttons"), list) else []
        labels = " ".join(str(item.get("text") or "") for item in buttons if isinstance(item, dict)).lower()
        if any(word in labels for word in ("allow", "agree", "authorize", "confirm", "accept", "同意", "授权", "确认")):
            return True
        return any(marker in path for marker in ("/consent", "/authorize/resume", "/oauth/authorize")) and any(
            word in labels for word in ("continue", "next", "继续", "下一步")
        )

    def _exchange_browser_oauth_code(self, code: str, code_verifier: str) -> dict[str, str]:
        proxy = self.registration_proxy_url or str(config.get("proxy") or "")
        session = requests.Session(**proxy_settings.build_session_kwargs(
            proxy=proxy,
            upstream=False,
            impersonate=str(self.fingerprint.get("impersonate") or "chrome"),
            verify=False,
        ))
        try:
            tokens = request_platform_oauth_token(session, code, code_verifier, self.fingerprint) or {}
        finally:
            session.close()
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not access_token or not refresh_token:
            raise BrowserRegistrationError("browser_oauth_token_incomplete")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": str(tokens.get("id_token") or "").strip(),
        }

    def _devtools_platform_oauth_tokens(
        self,
        port: int,
        mailbox: dict,
        email: str,
        index: int,
    ) -> dict[str, str]:
        code_verifier, code_challenge = generate_pkce()
        expected_state = secrets.token_urlsafe(32)
        authorize_url = self._platform_authorize_url(email, expected_state, code_challenge)
        step(index, "浏览器注册完成，开始 Platform OAuth 授权")
        browser_devtools.navigate_to(port, authorize_url, self._devtools_timeout())

        email_submitted = False
        otp_entry_clicked = False
        otp_submitted = False
        consent_clicks = 0
        last_summary = ""
        while time.monotonic() < self._deadline:
            state = self._devtools_state(port)
            current_url = str(state.get("url") or "")
            parsed = urlparse(current_url)
            if parsed.hostname == "platform.openai.com" and parsed.path.rstrip("/") == "/auth/callback":
                query = parse_qs(parsed.query)
                callback_state = str((query.get("state") or [""])[0]).strip()
                code = str((query.get("code") or [""])[0]).strip()
                if callback_state != expected_state:
                    raise BrowserRegistrationError("browser_oauth_state_mismatch")
                if not code:
                    error = str((query.get("error") or [""])[0]).strip()
                    raise BrowserRegistrationError(f"browser_oauth_callback_missing_code:{error or 'unknown'}")
                tokens = self._exchange_browser_oauth_code(code, code_verifier)
                step(index, "Platform OAuth token 获取完成")
                return tokens

            summary = self._devtools_state_summary(state)
            if summary != last_summary:
                step(index, f"Platform OAuth 状态 {summary}")
                last_summary = summary

            if _devtools_is_code_page(state) and not otp_submitted:
                code = self._wait_for_otp(mailbox, index, login=True)
                browser_devtools.evaluate_json(
                    port,
                    _devtools_submit_code_js(code),
                    timeout=self._devtools_timeout(20),
                    hosts=("auth.openai.com",),
                )
                otp_submitted = True
            elif _devtools_is_password_page(state) and not otp_entry_clicked:
                mailbox["_code_requested_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                browser_devtools.evaluate_json(
                    port,
                    DEVTOOLS_CLICK_ONE_TIME_CODE_JS,
                    timeout=self._devtools_timeout(20),
                    hosts=("auth.openai.com",),
                )
                otp_entry_clicked = True
            elif any(str(item.get("type") or "").lower() == "email" for item in _devtools_visible_inputs(state)) and not email_submitted:
                browser_devtools.evaluate_json(
                    port,
                    _devtools_submit_email_js(email),
                    timeout=self._devtools_timeout(20),
                    hosts=("auth.openai.com",),
                )
                email_submitted = True
            elif self._oauth_consent_visible(state):
                if consent_clicks >= 3:
                    raise BrowserRegistrationError("browser_oauth_consent_stalled")
                browser_devtools.evaluate_json(
                    port,
                    DEVTOOLS_CLICK_CONSENT_JS,
                    timeout=self._devtools_timeout(20),
                    hosts=("auth.openai.com", "platform.openai.com"),
                )
                consent_clicks += 1
            else:
                time.sleep(0.5)
        raise BrowserRegistrationError("browser_oauth_callback_timeout")

    def _devtools_registration_tokens(
        self,
        port: int,
        mailbox: dict,
        email: str,
        index: int,
    ) -> dict[str, str]:
        token_mode = str(config.get("browser_token_mode") or "session").strip().lower()
        if token_mode != "oauth":
            step(index, "ChatGPT 注册完成，读取浏览器 session token")
            access_token = self._devtools_access_token(port)
            return {
                "access_token": access_token,
                "refresh_token": "",
                "id_token": "",
                "registration_token_mode": "session",
            }

        session_access_token = ""
        try:
            step(index, "ChatGPT 注册完成，缓存 session token 作为 OAuth 失败兜底")
            session_access_token = self._devtools_access_token(port)
        except Exception as error:
            step(index, f"session token 兜底缓存失败: {_sanitized_error(error)}", "yellow")

        try:
            oauth_tokens = self._devtools_platform_oauth_tokens(port, mailbox, email, index)
            return {**oauth_tokens, "registration_token_mode": "oauth"}
        except Exception as error:
            reason = _sanitized_error(error)
            if not session_access_token:
                raise BrowserRegistrationError(f"browser_oauth_and_session_failed:{reason}") from error
            step(index, f"Platform OAuth 获取失败，使用 session token 入池: {reason}", "yellow")
            return {
                "access_token": session_access_token,
                "refresh_token": "",
                "id_token": "",
                "registration_token_mode": "session_fallback",
            }

    @staticmethod
    def _client_hint_brands(items: object) -> str:
        if not isinstance(items, list):
            return ""
        values: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").replace("\\", "\\\\").replace('"', '\\"').strip()
            version = str(item.get("version") or "").replace("\\", "\\\\").replace('"', '\\"').strip()
            if brand and version:
                values.append(f'"{brand}";v="{version}"')
        return ", ".join(values)

    @staticmethod
    def _browser_accept_language(snapshot: dict[str, Any]) -> str:
        raw_languages = snapshot.get("languages")
        languages = [str(item or "").strip() for item in raw_languages] if isinstance(raw_languages, list) else []
        languages = [item for item in languages if item]
        if not languages:
            language = str(snapshot.get("language") or "").strip()
            languages = [language] if language else []
        if not languages:
            return ""
        parts = [languages[0]]
        for index, language in enumerate(languages[1:5], start=1):
            parts.append(f"{language};q={max(0.5, 1.0 - index * 0.1):.1f}")
        return ",".join(parts)

    def _capture_devtools_fingerprint(self, port: int, index: int) -> None:
        snapshot = browser_devtools.evaluate_json(
            port,
            DEVTOOLS_FINGERPRINT_JS,
            timeout=self._devtools_timeout(15),
            hosts=("chatgpt.com",),
        )
        actual_user_agent = str(snapshot.get("user_agent") or "").strip()
        if actual_user_agent:
            self.fingerprint = _align_fingerprint_platform(
                _fingerprint_with_user_agent(self.fingerprint, actual_user_agent)
            )

        updates: dict[str, str] = {}
        accept_language = self._browser_accept_language(snapshot)
        if accept_language:
            updates["accept_language"] = accept_language
        language = str(snapshot.get("language") or "").strip()
        if language:
            updates["language"] = language
        timezone_name = str(snapshot.get("timezone") or "").strip()
        if timezone_name:
            updates["timezone"] = timezone_name
        platform = str(snapshot.get("platform") or "").strip()
        if platform:
            updates["sec_ch_ua_platform"] = f'"{platform}"'
        brands = self._client_hint_brands(snapshot.get("brands"))
        if brands:
            updates["sec_ch_ua"] = brands
        full_version_list = self._client_hint_brands(snapshot.get("full_version_list"))
        if full_version_list:
            updates["sec_ch_ua_full_version_list"] = full_version_list
        for source_key, target_key in (
            ("platform_version", "platform_version"),
            ("architecture", "architecture"),
            ("bitness", "bitness"),
            ("model", "model"),
            ("timezone_offset_min", "timezone_offset_min"),
            ("screen_width", "screen_width"),
            ("screen_height", "screen_height"),
            ("page_width", "page_width"),
            ("page_height", "page_height"),
            ("pixel_ratio", "pixel_ratio"),
            ("hardware_concurrency", "hardware_concurrency"),
            ("device_memory", "device_memory"),
        ):
            value = str(snapshot.get(source_key) if snapshot.get(source_key) is not None else "").strip()
            if value:
                updates[target_key] = value
        updates["sec_ch_ua_mobile"] = "?1" if bool(snapshot.get("mobile")) else "?0"

        stored_device_id = str(snapshot.get("stored_device_id") or "").strip()
        stored_session_id = str(snapshot.get("stored_session_id") or "").strip()
        try:
            cookies = browser_devtools.get_all_cookies(
                port,
                timeout=self._devtools_timeout(10),
                hosts=("chatgpt.com", "auth.openai.com"),
            )
        except Exception:
            cookies = []
        cookie_device_id = next((
            str(item.get("value") or "").strip()
            for item in cookies
            if str(item.get("name") or "").lower() == "oai-did" and str(item.get("value") or "").strip()
        ), "")
        if cookie_device_id or stored_device_id:
            updates["device_id"] = cookie_device_id or stored_device_id
        if stored_session_id:
            updates["session_id"] = stored_session_id

        self.fingerprint.update(updates)
        major = str(self.fingerprint.get("major") or "")
        self.fingerprint["impersonate"] = f"chrome{major}" if major in {"131", "136", "142", "145", "146"} else "chrome"
        step(
            index,
            f"浏览器身份快照已保存 device_id={'browser' if cookie_device_id or stored_device_id else 'generated'} "
            f"timezone={'yes' if timezone_name else 'no'} screen={'yes' if updates.get('screen_width') else 'no'}",
        )

    def _run_devtools_registration(
        self,
        mailbox: dict,
        email: str,
        name: str,
        birthdate: str,
        index: int,
    ) -> dict[str, str]:
        process: subprocess.Popen[Any] | None = None
        port = 0
        proxy_auth: browser_devtools.ProxyAuthHandler | None = None
        with tempfile.TemporaryDirectory(prefix="chatgpt2api-browser-") as profile_dir:
            try:
                process, port = self._launch_devtools_browser(Path(profile_dir), index)
                if self.registration_proxy_username:
                    proxy_auth = browser_devtools.ProxyAuthHandler(
                        port,
                        self.registration_proxy_username,
                        self.registration_proxy_password,
                    )
                    proxy_auth.start(timeout=min(5.0, self._devtools_timeout(5)))
                    step(index, "浏览器注册代理认证已启用")
                browser_devtools.navigate_to(port, CHATGPT_LOGIN_URL, self._devtools_timeout())
                state_reader = lambda: self._devtools_state(port)
                step(index, "浏览器等待 ChatGPT 登录页")
                browser_devtools.wait_for(
                    state_reader,
                    lambda state: (
                        "chatgpt.com/auth/login" in str(state.get("url") or "")
                        and any(str(item.get("type") or "").lower() == "email" for item in _devtools_visible_inputs(state))
                    ),
                    timeout=self._devtools_timeout(),
                )

                step(index, "浏览器重复提交邮箱，直到页面状态改变")
                state = browser_devtools.submit_until(
                    lambda: browser_devtools.evaluate_json(
                        port,
                        _devtools_submit_email_js(email),
                        timeout=self._devtools_timeout(),
                        hosts=("chatgpt.com", "auth.openai.com"),
                    ),
                    state_reader,
                    lambda item: _devtools_is_code_page(item) or _devtools_is_logged_in(item) or _devtools_is_password_page(item),
                    timeout=self._devtools_timeout(),
                )
                step(index, f"浏览器邮箱状态已改变 {self._devtools_state_summary(state)}")

                if _devtools_is_password_page(state):
                    step(index, "浏览器重复切换一次性验证码，直到验证码页出现")
                    state = browser_devtools.submit_until(
                        lambda: browser_devtools.evaluate_json(
                            port,
                            DEVTOOLS_CLICK_ONE_TIME_CODE_JS,
                            timeout=self._devtools_timeout(),
                            hosts=("auth.openai.com", "chatgpt.com"),
                        ),
                        state_reader,
                        lambda item: _devtools_is_code_page(item) or _devtools_is_logged_in(item),
                        timeout=self._devtools_timeout(),
                    )
                    step(index, f"浏览器验证码入口状态已改变 {self._devtools_state_summary(state)}")

                if not _devtools_is_logged_in(state):
                    if not _devtools_is_code_page(state):
                        raise BrowserRegistrationError(
                            f"browser_unexpected_state_after_email:{self._devtools_state_summary(state)}"
                        )
                    code = self._wait_for_otp(mailbox, index)
                    step(index, "浏览器重复提交验证码，直到进入资料页或登录完成")
                    state = browser_devtools.submit_until(
                        lambda: browser_devtools.evaluate_json(
                            port,
                            _devtools_submit_code_js(code),
                            timeout=self._devtools_timeout(),
                            hosts=("auth.openai.com", "chatgpt.com"),
                        ),
                        state_reader,
                        lambda item: _devtools_is_profile_page(item) or _devtools_is_logged_in(item),
                        timeout=self._devtools_timeout(),
                    )
                    step(index, f"浏览器验证码状态已改变 {self._devtools_state_summary(state)}")

                if _devtools_is_profile_page(state):
                    step(index, "浏览器重复提交姓名生日，直到登录完成")
                    state = browser_devtools.submit_until(
                        lambda: browser_devtools.evaluate_json(
                            port,
                            _devtools_submit_profile_js(name, birthdate),
                            timeout=self._devtools_timeout(),
                            hosts=("auth.openai.com", "chatgpt.com"),
                        ),
                        state_reader,
                        _devtools_is_logged_in,
                        timeout=self._devtools_timeout(),
                    )
                    step(index, f"浏览器资料状态已改变 {self._devtools_state_summary(state)}")

                if not _devtools_is_logged_in(state):
                    raise BrowserRegistrationError(f"browser_login_incomplete:{self._devtools_state_summary(state)}")
                self._capture_devtools_fingerprint(port, index)
                return self._devtools_registration_tokens(port, mailbox, email, index)
            finally:
                if proxy_auth is not None:
                    proxy_auth.close()
                if port:
                    browser_devtools.close_browser(port, process)

    def _wait_for_otp(self, mailbox: dict, index: int, *, login: bool = False) -> str:
        mailbox["_code_requested_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        step(index, "等待浏览器登录验证码" if login else "等待浏览器注册验证码")
        mail_config = _mail_config()
        mail_config["wait_timeout"] = max(1, min(
            int(mail_config.get("wait_timeout") or 120),
            int(max(1, self._deadline - time.monotonic())),
        ))
        code = mail_provider.wait_for_code(mail_config, mailbox)
        if not code:
            raise BrowserRegistrationError("browser_otp_timeout")
        return code

    def _switch_to_one_time_code(self, page, index: int, *, login: bool) -> None:
        original_url = str(page.url or "")
        otp_selectors = (
            'input[autocomplete="one-time-code"]',
            'input[name="code"]',
            'input[name="otp"]',
            'input[name="otpCode"]',
            'input[data-testid*="otp" i]',
            'input[aria-label*="digit" i]',
            'input[inputmode="numeric"]',
            'input[maxlength="1"]',
        )
        for attempt in range(1, BROWSER_ONE_TIME_CODE_RETRY_LIMIT + 1):
            action = "登录" if login else "注册"
            step(index, f"浏览器切换一次性验证码{action} attempt={attempt}/{BROWSER_ONE_TIME_CODE_RETRY_LIMIT}")
            clicked = page.evaluate(
                r"""
                () => {
                    const visible = (element) => {
                        if (!element) return false;
                        const rect = element.getBoundingClientRect();
                        const style = getComputedStyle(element);
                        return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
                    };
                    const textOf = (element) => (element.innerText || element.textContent || element.value || element.getAttribute("aria-label") || "")
                        .trim().replace(/\s+/g, " ").toLowerCase();
                    const target = [...document.querySelectorAll("button,a,[role=button],input[type=button],input[type=submit]")]
                        .filter(visible)
                        .find((element) => {
                            const text = textOf(element);
                            return text.includes("one-time code") || text.includes("one time code") || text.includes("verification code")
                                || text.includes("一次性") || text.includes("验证码") || text.includes("驗證碼");
                        });
                    if (!target) return false;
                    target.scrollIntoView({block: "center", inline: "center"});
                    target.click();
                    return true;
                }
                """
            )
            if not clicked:
                break
            for _ in range(8):
                page.wait_for_timeout(500)
                self._capture_callback(page.url)
                if self.callback_code:
                    return
                self._check_challenge(page)
                if self._editable(page, otp_selectors) is not None:
                    return
                if str(page.url or "") != original_url:
                    return
                if self._visible(page, BROWSER_ONE_TIME_CODE_SELECTORS) is None:
                    return
        path = self._page_path(page)
        step(
            index,
            f"浏览器一次性验证码入口未跳转 path={path} {self._control_summary(page)} {self._error_summary(page)}",
            "yellow",
        )
        raise BrowserRegistrationError(f"browser_one_time_code_transition_timeout:path={path}")

    def _handle_existing_outlook(self, page, mailbox: dict, index: int) -> None:
        if str(mailbox.get("provider") or "") != "outlook_token":
            raise BrowserRegistrationError("browser_existing_account_login_unsupported")
        otp_selectors = (
            'input[autocomplete="one-time-code"]',
            'input[name="code"]',
            'input[name="otp"]',
            'input[name="otpCode"]',
        )
        alternative = self._visible(page, (
            'button:has-text("Try another way")',
            'a:has-text("Try another way")',
            'button:has-text("Use a code")',
            'a:has-text("Use a code")',
        ))
        if alternative is not None:
            alternative.click(timeout=self._remaining_ms())
            page.wait_for_timeout(500)
        code_option = self._visible(page, (
            'button:has-text("Email code")',
            'a:has-text("Email code")',
            'button:has-text("Continue with email")',
            'a:has-text("Continue with email")',
            'button:has-text("Email me a code")',
            'a:has-text("Email me a code")',
            *BROWSER_ONE_TIME_CODE_SELECTORS,
        ))
        if code_option is not None:
            code_option.click(timeout=self._remaining_ms())
        otp_input = self._editable(page, otp_selectors)
        for _ in range(12):
            if otp_input is not None:
                break
            page.wait_for_timeout(500)
            self._check_challenge(page)
            otp_input = self._editable(page, otp_selectors)
        if otp_input is None:
            step(index, f"浏览器已有账号登录控件 {self._control_summary(page)}", "yellow")
            raise BrowserRegistrationError("browser_existing_account_login_unsupported")
        code = self._wait_for_otp(mailbox, index, login=True)
        self._fill_otp(page, code)
        self._continue(page, "login_otp")

    def _complete_profile(self, page, name: str, birthdate: str) -> None:
        filled = False
        name_input = self._editable(page, (
            'input[name="name"]',
            'input[autocomplete="name"]',
            'input[data-testid="name-input"]',
        ))
        if name_input is not None:
            name_input.fill(name, timeout=self._remaining_ms())
            filled = True
        else:
            first_name, _, last_name = name.partition(" ")
            first_input = self._editable(page, ('input[name="firstName"]', 'input[name="first_name"]'))
            last_input = self._editable(page, ('input[name="lastName"]', 'input[name="last_name"]'))
            if first_input is not None and last_input is not None:
                first_input.fill(first_name, timeout=self._remaining_ms())
                last_input.fill(last_name, timeout=self._remaining_ms())
                filled = True
        birth_input = self._editable(page, ('input[name="birthdate"]', 'input[type="date"]'))
        if birth_input is not None:
            birth_input.fill(birthdate, timeout=self._remaining_ms())
            filled = True
        else:
            year, month, day = birthdate.split("-")
            fields = page.locator('input[inputmode="numeric"]')
            if fields.count() >= 3:
                fields.nth(0).fill(month)
                fields.nth(1).fill(day)
                fields.nth(2).fill(year)
                filled = True
        if not filled:
            raise BrowserRegistrationError(f"browser_unexpected_state:about_you:path={self._page_path(page)}")
        self._continue(page, "about_you")

    def _fill_otp(self, page, code: str) -> None:
        fields = page.locator(
            'input[inputmode="numeric"], input[maxlength="1"], '
            'input[data-testid*="otp" i], input[aria-label*="digit" i]'
        )
        visible_fields = []
        for field_index in range(fields.count()):
            field = fields.nth(field_index)
            try:
                if field.is_visible(timeout=100) and field.is_editable():
                    visible_fields.append(field)
            except Exception:
                continue
        if len(visible_fields) >= len(code) and len(code) > 1:
            for field, digit in zip(visible_fields, code):
                field.fill(digit, timeout=self._remaining_ms())
            return
        self._fill(
            page,
            (
                'input[autocomplete="one-time-code"]',
                'input[name="code"]',
                'input[name="otp"]',
                'input[name="otpCode"]',
                'input[data-testid="otp-input"]',
                'input[data-testid*="otp" i]',
                'input[aria-label*="digit" i]',
                'input[maxlength="1"]',
                'input[inputmode="numeric"]',
            ),
            code,
            "otp",
        )

    def _run_authorization_flow(
        self,
        page,
        mailbox: dict,
        email: str,
        password: str,
        name: str,
        birthdate: str,
        index: int,
    ) -> str:
        self._wait_for_initial_hydration(page)
        email_done = False
        password_done = False
        otp_done = False
        profile_done = False
        unknown_count = 0
        error_retry_counts: dict[str, int] = {}
        last_logged_state = ""
        last_action_key = ""
        repeated_action_count = 0

        while not self._registration_complete(page):
            self._check_challenge(page)
            path = self._page_path(page)
            url_lower = str(page.url or "").lower()

            otp_selectors = (
                'input[autocomplete="one-time-code"]',
                'input[name="code"]',
                'input[name="otp"]',
                'input[name="otpCode"]',
                'input[data-testid="otp-input"]',
                'input[data-testid*="otp" i]',
                'input[aria-label*="digit" i]',
            )
            if "/about-you" not in url_lower:
                otp_selectors += ('input[inputmode="numeric"]', 'input[maxlength="1"]')
            otp_input = self._editable(page, otp_selectors)
            profile_input = self._editable(page, (
                'input[name="birthdate"]',
                'input[type="date"]',
                'input[name="name"]',
                'input[data-testid="name-input"]',
            ))
            password_input = self._editable(page, (
                'input[type="password"]',
                'input[name="password"]',
                'input[name="newPassword"]',
                'input[autocomplete="new-password"]',
                'input[autocomplete="current-password"]',
            ))
            email_input = self._editable(page, BROWSER_EMAIL_SELECTORS)
            retry_control = self._visible(page, (
                'button:has-text("Try again")',
                'a:has-text("Try again")',
                'button:has-text("Retry")',
                'a:has-text("Retry")',
            ))
            one_time_code_control = self._visible(page, BROWSER_ONE_TIME_CODE_SELECTORS)

            state = "unknown"
            if retry_control is not None:
                state = "retry"
            elif password_input is not None and one_time_code_control is not None:
                state = "one_time_code"
            elif otp_input is not None and not otp_done:
                state = "otp"
            elif ("/about-you" in url_lower or profile_input is not None) and not profile_done:
                state = "about_you"
            elif password_input is not None and not password_done:
                state = "password"
            elif email_input is not None and not email_done:
                state = "email"
            else:
                consent_selectors = (
                    'button[name="action"][value="accept"]',
                    'button:has-text("Allow")',
                    'button:has-text("Agree")',
                    'button:has-text("Authorize")',
                    'button:has-text("Confirm")',
                )
                if profile_done or any(marker in url_lower for marker in ("/consent", "/authorize/resume", "/oauth/authorize")):
                    consent_selectors += (
                        'button[data-testid="continue-button"]',
                        'button:has-text("Continue")',
                        'button:has-text("Next")',
                    )
                consent = self._visible(page, consent_selectors)
                if consent is not None:
                    state = "consent"

            state_log_key = f"{state}:{path}"
            if state_log_key != last_logged_state:
                step(index, f"浏览器状态[{state}] path={path}")
                last_logged_state = state_log_key

            if state not in {"unknown", "retry"}:
                if state_log_key == last_action_key:
                    repeated_action_count += 1
                else:
                    last_action_key = state_log_key
                    repeated_action_count = 1
                if repeated_action_count >= 3:
                    step(
                        index,
                        f"浏览器状态停滞 path={path} {self._control_summary(page)} {self._error_summary(page)}",
                        "yellow",
                    )
                    raise BrowserRegistrationError(f"browser_state_stalled:{state}:path={path}")

            if state == "email":
                self._submit_email(page, email, index)
                email_done = True
            elif state == "password":
                is_login = (
                    "log-in" in url_lower
                    or "login" in url_lower
                    or str(password_input.get_attribute("autocomplete") or "") == "current-password"
                )
                if is_login:
                    self._handle_existing_outlook(page, mailbox, index)
                    password = ""
                else:
                    password_input.fill(password, timeout=self._remaining_ms())
                    self._continue(page, "password")
                password_done = True
            elif state == "one_time_code":
                is_login = "log-in" in url_lower or "login" in url_lower
                if is_login and str(mailbox.get("provider") or "") != "outlook_token":
                    raise BrowserRegistrationError("browser_existing_account_login_unsupported")
                mailbox["_code_requested_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                self._switch_to_one_time_code(page, index, login=is_login)
                password = ""
                password_done = True
            elif state == "otp":
                is_login = "log-in" in url_lower or "login" in url_lower
                code = self._wait_for_otp(mailbox, index, login=is_login)
                self._fill_otp(page, code)
                self._continue(page, "otp")
                if not password_done:
                    password = ""
                otp_done = True
            elif state == "about_you":
                self._complete_profile(page, name, birthdate)
                profile_done = True
            elif state == "consent":
                consent.click(timeout=self._remaining_ms())
                self._wait_for_transition(page)
            elif state == "retry":
                retry_count = error_retry_counts.get(path, 0) + 1
                error_retry_counts[path] = retry_count
                step(
                    index,
                    f"浏览器错误恢复 path={path} attempt={retry_count}/{BROWSER_ERROR_RETRY_LIMIT} "
                    f"{self._error_summary(page)}",
                    "yellow",
                )
                if retry_count > BROWSER_ERROR_RETRY_LIMIT:
                    raise BrowserRegistrationError(f"browser_retry_exhausted:path={path}")
                if "password" in url_lower or (password_done and not otp_done):
                    password_done = False
                elif "email" in url_lower or "identifier" in url_lower or not email_done:
                    email_done = False
                elif "verification" in url_lower or "otp" in url_lower:
                    otp_done = False
                elif "about-you" in url_lower:
                    profile_done = False
                last_action_key = ""
                repeated_action_count = 0
                retry_control.click(timeout=self._remaining_ms())
                self._wait_for_transition(page)
            else:
                unknown_count += 1
                if unknown_count == 1:
                    step(index, f"浏览器控件诊断 path={path} {self._control_summary(page)}", "yellow")
                if unknown_count >= 12:
                    raise BrowserRegistrationError(f"browser_unexpected_state:path={path}")
                page.wait_for_timeout(750)
                continue
            unknown_count = 0

        return password

    @staticmethod
    def _restartable_transition_error(error: BrowserRegistrationError) -> bool:
        reason = str(error or "")
        return reason.startswith((
            "browser_email_transition_timeout:",
            "browser_one_time_code_transition_timeout:",
            "browser_state_stalled:email:",
            "browser_state_stalled:one_time_code:",
        ))

    def _run_authorization_with_restarts(
        self,
        page,
        mailbox: dict,
        email: str,
        password: str,
        name: str,
        birthdate: str,
        index: int,
    ) -> str:
        total_attempts = BROWSER_AUTH_RESTART_LIMIT + 1
        for attempt in range(1, total_attempts + 1):
            try:
                return self._run_authorization_flow(
                    page,
                    mailbox,
                    email,
                    password,
                    name,
                    birthdate,
                    index,
                )
            except BrowserRegistrationError as error:
                if not self._restartable_transition_error(error) or attempt >= total_attempts:
                    raise
                next_attempt = attempt + 1
                step(
                    index,
                    f"浏览器授权未跳转，刷新并重新提交同一邮箱 "
                    f"attempt={next_attempt}/{total_attempts} reason={_sanitized_error(error)}",
                    "yellow",
                )
                self.callback_code = ""
                self._auth_responses.clear()
                page.goto(
                    self._registration_url(),
                    wait_until="domcontentloaded",
                    timeout=self._remaining_ms(),
                )
        raise BrowserRegistrationError("browser_authorization_retry_exhausted")

    def register(self, index: int) -> dict[str, Any]:
        status = browser_runtime_status()
        if not status["browser_available"]:
            raise BrowserRegistrationError("browser_runtime_unavailable")

        mailbox = mail_provider.create_mailbox(_mail_config())
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise BrowserRegistrationError("browser_mailbox_address_missing")
        password = _random_password()
        first_name, last_name = _random_name()
        name = f"{first_name} {last_name}"
        birthdate = _random_birthdate()
        self._deadline = time.monotonic() + BROWSER_TASK_TIMEOUT_SECONDS
        step(index, "浏览器注册任务启动")
        try:
            tokens = self._run_devtools_registration(mailbox, email, name, birthdate, index)
            password = ""
        except BrowserRegistrationError as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        except Exception as error:
            sanitized = _sanitized_error(error).replace(email, "***").replace(password, "***")
            wrapped = BrowserRegistrationError(f"browser_registration_failed:{sanitized}")
            mail_provider.mark_mailbox_result(mailbox, success=False, error=wrapped)
            raise wrapped from error
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return {
            "email": email,
            "password": password,
            **tokens,
            "source_type": "microsoft" if not password else "web",
            "registration_engine": "browser",
            "fp": dict(self.fingerprint),
            "status": "正常",
            "quota": 0,
            "image_quota_unknown": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int) -> dict[str, Any]:
    from services.register.openai_register import log

    started = time.time()
    try:
        result = BrowserRegistrar().register(index)
        account_service = __import__("services.account_service", fromlist=["account_service"]).account_service
        account_service.add_account_items([result])
        if str(config.get("mode") or "total") == "quota":
            try:
                from services.openai_backend_api import OpenAIBackendAPI

                with OpenAIBackendAPI(result["access_token"]) as backend:
                    quota_info = backend.get_image_quota_info()
                account_service.update_account(result["access_token"], quota_info, quiet=True)
            except Exception as error:
                step(index, f"账号已保存，额度初始化暂未成功: {_sanitized_error(error)}", "yellow")
        log(f'{result["email"]} 浏览器注册成功，本次耗时{time.time() - started:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except mail_provider.AllMailProvidersUnavailableError as error:
        sanitized = _sanitized_error(error)
        log(f"任务{index} 浏览器注册停止，本次耗时{time.time() - started:.1f}s，原因: {sanitized}", "red")
        return {"ok": False, "index": index, "error": sanitized, "stop_reason": error.stop_reason}
    except Exception as error:
        log(f"任务{index} 浏览器注册失败，本次耗时{time.time() - started:.1f}s，原因: {_sanitized_error(error)}", "red")
        return {"ok": False, "index": index, "error": _sanitized_error(error)}
