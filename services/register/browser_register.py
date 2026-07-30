from __future__ import annotations

import importlib.metadata
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from services.browser_fingerprint import make_fingerprint
from services.proxy_service import proxy_settings
from services.register import mail_provider
from services.register.openai_register import (
    _generate_pkce,
    _fingerprint_with_user_agent,
    _random_birthdate,
    _random_name,
    _random_password,
    auth_base,
    config,
    platform_auth0_client,
    platform_base,
    platform_oauth_audience,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
    step,
)


BROWSER_NAVIGATION_TIMEOUT_MS = 45_000
BROWSER_TASK_TIMEOUT_SECONDS = 300
BROWSER_ERROR_RETRY_LIMIT = 2
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


class BrowserRegistrationError(RuntimeError):
    pass


def _sanitized_error(error: BaseException) -> str:
    text = str(error or error.__class__.__name__)
    text = re.sub(r"([?&](?:login_hint|username|email)=)[^&\s]+", r"\1***", text, flags=re.I)
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
                headless=True,
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


class BrowserRegistrar:
    def __init__(self) -> None:
        self.fingerprint = make_fingerprint()
        self.code_verifier = ""
        self.callback_code = ""
        self._deadline = 0.0
        self._auth_responses: list[str] = []

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
                for key in ("type", "name", "autocomplete", "inputmode", "maxlength", "data-testid"):
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
            resource_type = str(response.request.resource_type or "")
            if parsed.hostname != auth_host or resource_type not in {"document", "fetch", "xhr"}:
                return
            path = (parsed.path.rstrip("/") or "/")[:120]
            status = int(response.status)
            if status < 400 and not path.startswith("/api/accounts/"):
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
        locator = self._visible(page, selectors)
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

    def _authorize_url(self, email: str) -> str:
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.fingerprint["device_id"],
            "screen_hint": "signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": os.urandom(24).hex(),
            "nonce": os.urandom(24).hex(),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        return f"{auth_base}/api/accounts/authorize?{urlencode(params)}"

    def _capture_callback(self, url: str) -> None:
        parsed = urlparse(str(url or ""))
        if parsed.netloc.lower() != "platform.openai.com" or parsed.path.rstrip("/") != "/auth/callback":
            return
        self.callback_code = str((parse_qs(parsed.query).get("code") or [""])[0]).strip()

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

    def _handle_existing_outlook(self, page, mailbox: dict, index: int) -> None:
        if str(mailbox.get("provider") or "") != "outlook_token":
            raise BrowserRegistrationError("browser_existing_account_login_unsupported")
        alternative = self._visible(page, ('button:has-text("Try another way")', 'button:has-text("Use a code")'))
        if alternative is not None:
            alternative.click(timeout=self._remaining_ms())
            page.wait_for_timeout(500)
        code_option = self._visible(page, ('button:has-text("Email code")', 'button:has-text("Continue with email")'))
        if code_option is not None:
            code_option.click(timeout=self._remaining_ms())
            page.wait_for_timeout(500)
        if self._visible(page, (
            'input[autocomplete="one-time-code"]',
            'input[name="code"]',
            'input[name="otp"]',
            'input[name="otpCode"]',
        )) is None:
            raise BrowserRegistrationError("browser_existing_account_login_unsupported")
        code = self._wait_for_otp(mailbox, index, login=True)
        self._fill_otp(page, code)
        self._continue(page, "login_otp")

    def _complete_profile(self, page, name: str, birthdate: str) -> None:
        filled = False
        name_input = self._visible(page, (
            'input[name="name"]',
            'input[autocomplete="name"]',
            'input[data-testid="name-input"]',
        ))
        if name_input is not None:
            name_input.fill(name, timeout=self._remaining_ms())
            filled = True
        else:
            first_name, _, last_name = name.partition(" ")
            first_input = self._visible(page, ('input[name="firstName"]', 'input[name="first_name"]'))
            last_input = self._visible(page, ('input[name="lastName"]', 'input[name="last_name"]'))
            if first_input is not None and last_input is not None:
                first_input.fill(first_name, timeout=self._remaining_ms())
                last_input.fill(last_name, timeout=self._remaining_ms())
                filled = True
        birth_input = self._visible(page, ('input[name="birthdate"]', 'input[type="date"]'))
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
                if field.is_visible(timeout=100):
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
        email_done = False
        password_done = False
        otp_done = False
        profile_done = False
        unknown_count = 0
        error_retry_counts: dict[str, int] = {}
        last_logged_state = ""
        last_action_key = ""
        repeated_action_count = 0

        while not self.callback_code:
            self._capture_callback(page.url)
            if self.callback_code:
                break
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
            otp_input = self._visible(page, otp_selectors)
            profile_input = self._visible(page, (
                'input[name="birthdate"]',
                'input[type="date"]',
                'input[name="name"]',
                'input[data-testid="name-input"]',
            ))
            password_input = self._visible(page, (
                'input[type="password"]',
                'input[name="password"]',
                'input[name="newPassword"]',
                'input[autocomplete="new-password"]',
                'input[autocomplete="current-password"]',
            ))
            email_input = self._visible(page, (
                'input[type="email"]',
                'input[name="email"]',
                'input[name="username"]',
                'input[name="identifier"]',
                'input[autocomplete="username"]',
                'input[data-testid="email-input"]',
                'input[placeholder*="Email" i]',
                '#email',
            ))
            retry_control = self._visible(page, (
                'button:has-text("Try again")',
                'a:has-text("Try again")',
                'button:has-text("Retry")',
                'a:has-text("Retry")',
            ))

            state = "unknown"
            if retry_control is not None:
                state = "retry"
            elif otp_input is not None and not otp_done:
                state = "otp"
            elif ("/about-you" in url_lower or profile_input is not None) and not profile_done:
                state = "about_you"
            elif password_input is not None and not password_done:
                state = "password"
            elif email_input is not None and not email_done:
                state = "email"
            else:
                consent = self._visible(page, (
                    'button[name="action"][value="accept"]',
                    'button[data-testid="continue-button"]',
                    'button:has-text("Continue")',
                    'button:has-text("Allow")',
                    'button:has-text("Agree")',
                    'button:has-text("Authorize")',
                    'button:has-text("Next")',
                    'button:has-text("Confirm")',
                ))
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
                    raise BrowserRegistrationError(f"browser_state_stalled:{state}:path={path}")

            if state == "email":
                email_input.fill(email, timeout=self._remaining_ms())
                self._continue(page, "email")
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
            elif state == "otp":
                code = self._wait_for_otp(mailbox, index, login=not bool(password))
                self._fill_otp(page, code)
                self._continue(page, "otp")
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
                if "password" in url_lower:
                    password_done = False
                elif "email" in url_lower or "identifier" in url_lower:
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

    def _exchange_token(self, context) -> dict[str, str]:
        if not self.callback_code:
            raise BrowserRegistrationError("browser_callback_code_missing")
        response = context.request.post(
            f"{auth_base}/api/accounts/oauth/token",
            headers={
                "accept": "*/*",
                "auth0-client": platform_auth0_client,
                "content-type": "application/json",
                "origin": platform_base,
                "referer": f"{platform_base}/",
            },
            data={
                "client_id": platform_oauth_client_id,
                "code_verifier": self.code_verifier,
                "grant_type": "authorization_code",
                "code": self.callback_code,
                "redirect_uri": platform_oauth_redirect_uri,
            },
            timeout=self._remaining_ms(),
        )
        data = response.json() if response.ok else {}
        access_token = str(data.get("access_token") or "").strip() if isinstance(data, dict) else ""
        if not access_token:
            raise BrowserRegistrationError(f"browser_oauth_token_http_{response.status}")
        return {
            "access_token": access_token,
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "id_token": str(data.get("id_token") or "").strip(),
        }

    def register(self, index: int) -> dict[str, Any]:
        status = browser_runtime_status()
        if not status["browser_available"]:
            raise BrowserRegistrationError("browser_runtime_unavailable")
        from playwright.sync_api import sync_playwright

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
            with sync_playwright() as playwright:
                executable = str(os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or "").strip() or None
                browser = playwright.chromium.launch(
                    executable_path=executable,
                    headless=True,
                    proxy=_playwright_proxy(),
                    args=["--disable-dev-shm-usage"],
                )
                try:
                    version = str(browser.version or "").strip()
                    if version:
                        full_version = version.split(" ")[-1]
                        major = full_version.split(".", 1)[0]
                        user_agent = (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            f"Chrome/{full_version} Safari/537.36"
                        )
                        self.fingerprint = _fingerprint_with_user_agent(self.fingerprint, user_agent)
                        self.fingerprint["impersonate"] = "chrome"
                    context = browser.new_context(
                        user_agent=self.fingerprint["user_agent"],
                        locale=self.fingerprint["accept_language"].split(",", 1)[0],
                        viewport={"width": 1365, "height": 768},
                        ignore_https_errors=True,
                    )
                    context.set_default_timeout(BROWSER_NAVIGATION_TIMEOUT_MS)
                    context.on("request", lambda request: self._capture_callback(request.url))
                    page = context.new_page()
                    page.on("response", self._record_auth_response)
                    page.on("framenavigated", lambda frame: self._capture_callback(frame.url))
                    page.goto(self._authorize_url(email), wait_until="domcontentloaded", timeout=self._remaining_ms())
                    password = self._run_authorization_flow(
                        page,
                        mailbox,
                        email,
                        password,
                        name,
                        birthdate,
                        index,
                    )
                    tokens = self._exchange_token(context)
                finally:
                    browser.close()
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
    except Exception as error:
        log(f"任务{index} 浏览器注册失败，本次耗时{time.time() - started:.1f}s，原因: {_sanitized_error(error)}", "red")
        return {"ok": False, "index": index, "error": _sanitized_error(error)}
