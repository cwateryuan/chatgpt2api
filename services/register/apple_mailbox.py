from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from threading import Condition
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from services.config import DATA_DIR


STATE_FILE = DATA_DIR / "apple_mailbox_state.json"
_condition = Condition()
_state: dict[str, dict] = {}
_signature: tuple | None = None
_owner = uuid.uuid4().hex
_cancelled: Callable[[], bool] = lambda: False
_revision = 0


class AppleMailboxError(RuntimeError):
    def __init__(self, reason: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(f"Apple mailbox {reason}" + (f" (HTTP/API {status_code})" if status_code else ""))


class ApplePoolUnavailableError(RuntimeError):
    pass


class ApplePoolBusyError(ApplePoolUnavailableError):
    pass


class AppleMailboxCancelledError(RuntimeError):
    pass


def clean_email(value: str) -> str:
    return str(value or "").strip().split(":", 1)[0].strip()


def mother_address(email: str) -> str:
    local, _, domain = clean_email(email).lower().partition("@")
    if domain in {"icloud.com", "me.com", "mac.com"}:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def _valid_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


@lru_cache(maxsize=8)
def _parse_credentials(text: str) -> tuple[dict, ...]:
    records: dict[str, dict] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("\ufeff").strip()
        if not line:
            continue
        if _valid_url(line):
            query = parse_qs(urlsplit(line).query.replace("+", "%2B"))
            email = clean_email((query.get("e") or query.get("email") or [""])[0])
            share_url, api_url = "", line
        else:
            email, separator, api_url = line.partition("----")
            if not separator:
                raise ValueError(f"iCloud import line {number}: invalid format")
            email, share_url, api_url = clean_email(email), "", api_url.strip()
            first_url, separator, second_url = api_url.partition("----")
            if separator and not second_url.strip():
                raise ValueError(f"iCloud import line {number}: API URL missing")
            if separator and _valid_url(first_url.strip()) and _valid_url(second_url.strip()):
                share_url, api_url = first_url.strip(), second_url.strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@:]+", email) or not _valid_url(api_url):
            raise ValueError(f"iCloud import line {number}: email or API URL missing/invalid")
        if share_url and not _valid_url(share_url):
            raise ValueError(f"iCloud import line {number}: invalid share URL")
        records[email.lower()] = {"email": email, "share_url": share_url, "mail_api_url": api_url}
    return tuple(records.values())


def parse_credentials(text: str) -> list[dict]:
    return [dict(item) for item in _parse_credentials(str(text or ""))]


def serialize_credentials(records: list[dict]) -> str:
    return "\n".join(
        f"{item['email']}----{item['share_url']}----{item['mail_api_url']}"
        if item.get("share_url") else f"{item['email']}----{item['mail_api_url']}"
        for item in records
    )


def merge_credentials(old: str, new: str) -> str:
    records = {item["email"].lower(): item for item in parse_credentials(old)}
    records.update({item["email"].lower(): item for item in parse_credentials(new)})
    return serialize_credentials(list(records.values()))


def _file_signature() -> tuple:
    try:
        stat = STATE_FILE.stat()
        return (str(STATE_FILE), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return (str(STATE_FILE), None)


def _load() -> dict[str, dict]:
    global _state, _signature
    signature = _file_signature()
    if signature != _signature:
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8")) if signature[1] is not None else {}
            if not isinstance(raw, dict) or any(not isinstance(value, dict) for value in raw.values()):
                raise ValueError("invalid state")
        except (OSError, ValueError):
            raise AppleMailboxError("state file unreadable; restore or reset it") from None
        _state, _signature = raw, signature
    return _state


def _save(store: dict[str, dict]) -> None:
    global _signature
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE_FILE.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(store, handle, ensure_ascii=True)
            handle.write("\n")
        os.replace(temp_path, STATE_FILE)
        _signature = _file_signature()
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def begin_run(owner: str, cancelled: Callable[[], bool]) -> None:
    global _owner, _cancelled, _revision
    with _condition:
        store = _load()
        stale = {key: value for key, value in store.items() if value.get("state") == "in_use" and value.get("owner") != owner}
        for key in stale:
            store.pop(key)
        if stale:
            try:
                _save(store)
            except Exception:
                store.update(stale)
                raise
        _owner, _cancelled = owner, cancelled
        _revision += 1
        _condition.notify_all()


def check_cancelled() -> None:
    if _cancelled():
        raise AppleMailboxCancelledError("Apple mailbox task cancelled")


def revision() -> int:
    with _condition:
        return _revision


def wait_available(previous_revision: int) -> None:
    with _condition:
        while _revision == previous_revision:
            check_cancelled()
            _condition.wait(timeout=0.5)
    check_cancelled()


def pool_stats(records: list[dict]) -> dict[str, int]:
    counts = {key: 0 for key in ("unused", "in_use", "used", "token_invalid", "failed")}
    with _condition:
        store = _load()
        for item in records:
            state = store.get(item["email"].lower(), {}).get("state", "unused")
            counts[state if state in counts else "unused"] += 1
    return counts


def has_pending(records: list[dict]) -> bool:
    with _condition:
        store = _load()
        return any(store.get(item["email"].lower(), {}).get("state") not in {"used", "failed", "token_invalid"} for item in records)


def claim(records: list[dict], provider_id: str) -> dict:
    check_cancelled()
    with _condition:
        store = _load()
        busy_mothers = {value.get("mother") or mother_address(key) for key, value in store.items() if value.get("state") == "in_use"}
        pending = False
        for item in records:
            key, mother = item["email"].lower(), mother_address(item["email"])
            entry = store.get(key, {})
            if entry.get("state") in {"used", "failed", "token_invalid"}:
                continue
            pending = True
            if entry.get("state") == "in_use" or mother in busy_mothers:
                continue
            claim_id = uuid.uuid4().hex
            store[key] = {"state": "in_use", "mother": mother, "owner": _owner, "claim_id": claim_id, "provider_id": provider_id}
            try:
                _save(store)
            except Exception:
                store.pop(key)
                if entry:
                    store[key] = entry
                raise
            return {**item, "_apple_claim_id": claim_id}
    if pending:
        raise ApplePoolBusyError("Apple mailbox mother inbox busy")
    raise ApplePoolUnavailableError("Apple mailbox pool exhausted or empty")


def finish(mailbox: dict, state: str | None, reason: str = "") -> None:
    global _revision
    key = str(mailbox.get("address") or "").lower()
    with _condition:
        store = _load()
        entry = store.get(key, {})
        if not mailbox.get("_apple_claim_id") or entry.get("claim_id") != mailbox["_apple_claim_id"] or entry.get("state") != "in_use":
            return
        if state:
            store[key] = {**entry, "state": state, "reason": reason, "updated_at": datetime.now(timezone.utc).isoformat()}
        else:
            store.pop(key)
        try:
            _save(store)
        except Exception:
            store[key] = entry
            raise
        _revision += 1
        _condition.notify_all()


def reset(records: list[dict], scope: str) -> int:
    global _revision
    with _condition:
        store = _load()
        keys = {item["email"].lower() for item in records}
        removed = {key: value for key, value in store.items() if key in keys and (scope == "all" or value.get("state") in {"failed", "token_invalid", "in_use"})}
        for key in removed:
            store.pop(key)
        if removed:
            try:
                _save(store)
            except Exception:
                store.update(removed)
                raise
        _revision += 1
        _condition.notify_all()
        return len(removed)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _TextParser()
    parser.feed(str(value or ""))
    return " ".join(html.unescape("".join(parser.parts)).split())


def extract_code(message: dict) -> str | None:
    explicit = str(message.get("verification_code") or "").strip()
    if re.fullmatch(r"\d{4,8}", explicit):
        return explicit
    content = f"{message.get('subject', '')} {message.get('text_content', '')}"
    match = re.search(r"(?:code|验证码|verification|otp|passcode)[^\d]{0,12}(\d{4,8})(?!\d)", content, re.I)
    if not match:
        match = re.search(r"\b(\d{4,8})\b", content)
    return match.group(1) if match else None


def message_ref(message: dict) -> str:
    content = json.dumps([message.get(key) for key in ("subject", "sender", "text_content", "verification_code")], ensure_ascii=True)
    digest = hashlib.sha256(content.encode()).hexdigest()
    return f"{message.get('message_id', 'latest')}:{digest}"


def parse_response(response, mailbox: dict, parse_time: Callable) -> list[dict]:
    status = int(response.status_code)
    if not 200 <= status < 300:
        raise AppleMailboxError("request rejected", status)
    try:
        payload = response.json()
    except Exception:
        body = str(response.text or "")
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type and not re.search(r"<(?:html|body)\b", body, re.I):
            raise AppleMailboxError("invalid response") from None
        items = [{"id": "latest", "subject": "OpenAI verification code", "content": body}]
    else:
        if not isinstance(payload, dict):
            raise AppleMailboxError("invalid response envelope")
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        effective = data.get("status_code") or payload.get("status_code")
        try:
            effective = int(effective or 0)
            api_code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            raise AppleMailboxError("invalid status code") from None
        if effective >= 400 or api_code != 0 or payload.get("success") is False:
            raise AppleMailboxError("request rejected", effective if effective >= 400 else api_code if api_code >= 400 else 400)
        if payload.get("success") is True and isinstance(payload.get("data"), dict):
            items = [data] if any(data.get(key) for key in ("subject", "content", "verification_code")) else []
        elif "code" in payload and api_code == 0 and isinstance(data.get("messages"), list):
            items = data["messages"]
        else:
            raise AppleMailboxError("invalid response envelope")
    messages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        received = item.get("received_at") or item.get("arrived_at")
        body = item.get("text_body") or item.get("html_body") or item.get("snippet") or item.get("content") or ""
        messages.append({
            "provider": "apple", "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or item.get("message_id") or item.get("mail_id") or received or "latest"),
            "subject": plain_text(str(item.get("subject") or "")),
            "sender": str(item.get("sender") or "OpenAI <noreply@openai.com>"),
            "text_content": plain_text(str(body)), "html_content": "",
            "received_at": parse_time(received),
            "verification_code": str(item.get("verification_code") or ""),
        })
    def order(message):
        sender = message["sender"].lower()
        subject = message["subject"].lower()
        preferred = any(word in sender + subject for word in ("openai", "chatgpt")) and any(word in subject for word in ("verification", "code", "otp", "验证码"))
        date = message["received_at"]
        return preferred, date.timestamp() if date else 0
    return sorted(messages, key=order, reverse=True)
