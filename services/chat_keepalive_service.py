"""Low-frequency chat keepalive / liveness probe for account pool.

Stores only optional fields on existing account JSON payloads (no schema migration).
"""
from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.account_service import account_service
from services.config import BASE_DIR, DATA_DIR, config
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation import conversation_events, is_token_invalid_error
from services.runtime_state import is_multi_worker_runtime, runtime_state
from utils.log import logger

# Prefer repo-tracked corpus; fall back to runtime data dir copies.
CORPUS_CANDIDATES = (
    BASE_DIR / "services" / "chat_keepalive_corpus.json",
    DATA_DIR / "chat_keepalive_corpus.json",
    BASE_DIR / "data" / "chat_keepalive_corpus.json",
)
CHAT_KEEPALIVE_LOCK = "lock:chat_keepalive"
DEFAULT_INTERVAL_SECONDS = 4 * 3600
DEFAULT_SCAN_SECONDS = 300
DEFAULT_BATCH_SIZE = 3
DEFAULT_TURNS_MIN = 2
DEFAULT_TURNS_MAX = 4
DEFAULT_JITTER_RATIO = 0.3
DEFAULT_ACCOUNT_GAP_SECONDS = 8.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _bool_config(key: str, default: bool) -> bool:
    raw = config.data.get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw) if raw is not None else default


def _int_config(key: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(config.data.get(key, default)))
    except (TypeError, ValueError):
        return default


def _float_config(key: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(config.data.get(key, default)))
    except (TypeError, ValueError):
        return default


class ChatKeepaliveService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._corpus: dict[str, list[dict[str, Any]]] | None = None
        self._recent_prompt_ids: dict[str, list[str]] = {}

    @property
    def enabled(self) -> bool:
        return _bool_config("chat_keepalive_enabled", True)

    @property
    def interval_seconds(self) -> float:
        return _float_config("chat_keepalive_interval_seconds", DEFAULT_INTERVAL_SECONDS, 600)

    @property
    def scan_seconds(self) -> float:
        return _float_config("chat_keepalive_scan_seconds", DEFAULT_SCAN_SECONDS, 30)

    @property
    def batch_size(self) -> int:
        return _int_config("chat_keepalive_batch_size", DEFAULT_BATCH_SIZE, 1)

    @property
    def turns_min(self) -> int:
        return _int_config("chat_keepalive_turns_min", DEFAULT_TURNS_MIN, 1)

    @property
    def turns_max(self) -> int:
        value = _int_config("chat_keepalive_turns_max", DEFAULT_TURNS_MAX, 1)
        return max(self.turns_min, value)

    @property
    def jitter_ratio(self) -> float:
        return min(0.8, _float_config("chat_keepalive_jitter_ratio", DEFAULT_JITTER_RATIO, 0.0))

    def _load_corpus(self) -> dict[str, list[dict[str, Any]]]:
        if self._corpus is not None:
            return self._corpus
        path = next((item for item in CORPUS_CANDIDATES if item.is_file()), CORPUS_CANDIDATES[0])
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            logger.warning({"event": "chat_keepalive_corpus_load_failed", "error": str(error), "path": str(path)})
            raw = {}
        corpus: dict[str, list[dict[str, Any]]] = {"zh": [], "en": []}
        if isinstance(raw, dict):
            for lang in ("zh", "en"):
                items = raw.get(lang)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    followups = [
                        str(part).strip()
                        for part in (item.get("followups") or [])
                        if str(part).strip()
                    ]
                    corpus[lang].append({
                        "id": str(item.get("id") or text[:32]),
                        "text": text,
                        "followups": followups,
                    })
        if not corpus["zh"] and not corpus["en"]:
            corpus = {
                "zh": [{"id": "zh_fallback", "text": "今天过得怎么样？简单回一句就行。", "followups": ["谢谢，先这样。"]}],
                "en": [{"id": "en_fallback", "text": "How is your day? One short sentence is enough.", "followups": ["Thanks, that is all."]}],
            }
        self._corpus = corpus
        return corpus

    def _pick_language(self, account: dict[str, Any]) -> str:
        fp = account.get("fp") if isinstance(account.get("fp"), dict) else {}
        language = str(fp.get("language") or fp.get("accept_language") or "").lower()
        if language.startswith("zh"):
            return "zh"
        if language.startswith("en"):
            return "en"
        return random.choice(["zh", "en"])

    def _pick_script(self, account: dict[str, Any], access_token: str) -> dict[str, Any]:
        corpus = self._load_corpus()
        lang = self._pick_language(account)
        pool = list(corpus.get(lang) or [])
        if not pool:
            pool = list(corpus.get("en") or corpus.get("zh") or [])
        recent = set(self._recent_prompt_ids.get(access_token) or [])
        fresh = [item for item in pool if item["id"] not in recent] or pool
        chosen = dict(random.choice(fresh))
        history = self._recent_prompt_ids.setdefault(access_token, [])
        history.append(str(chosen["id"]))
        self._recent_prompt_ids[access_token] = history[-8:]
        return chosen

    def _due_accounts(self, now: datetime) -> list[tuple[str, dict[str, Any], float]]:
        interval = self.interval_seconds
        jitter = self.jitter_ratio
        due: list[tuple[str, dict[str, Any], float]] = []
        for account in account_service.list_accounts():
            if not isinstance(account, dict):
                continue
            status = str(account.get("status") or "").strip()
            if status in {"禁用", "异常"}:
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                continue
            last_at = _parse_time(account.get("last_chat_keepalive_at"))
            # Per-account deterministic-ish jitter from token suffix keeps stagger stable.
            seed = sum(ord(ch) for ch in token[-12:]) or 1
            ratio = 1.0 + (((seed % 1000) / 1000.0) * 2 - 1) * jitter
            need_seconds = max(600.0, interval * ratio)
            if last_at is None:
                score = 0.0
            else:
                age = (now - last_at).total_seconds()
                if age < need_seconds:
                    continue
                score = -age
            due.append((token, account, score))
        due.sort(key=lambda item: item[2])
        return due

    def _build_turns(self, script: dict[str, Any]) -> list[str]:
        turns = random.randint(self.turns_min, self.turns_max)
        lines = [str(script.get("text") or "").strip()]
        followups = [str(item).strip() for item in (script.get("followups") or []) if str(item).strip()]
        random.shuffle(followups)
        for item in followups:
            if len(lines) >= turns:
                break
            lines.append(item)
        while len(lines) < turns:
            lines.append(random.choice([
                "嗯，继续随便聊聊就行。",
                "Okay, just one more short thought.",
                "好的，简单补充一句就够。",
                "Thanks, one last short reply is enough.",
            ]))
        return lines[:turns]

    def _collect_reply(self, access_token: str, messages: list[dict[str, str]]) -> str:
        """Chat on a fixed account. Do not use pool text routing (it may switch tokens)."""
        backend = OpenAIBackendAPI(access_token=access_token)
        chunks: list[str] = []
        try:
            for event in conversation_events(backend, messages=list(messages), model="auto"):
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    chunks.append(delta)
        finally:
            backend.close()
        return "".join(chunks).strip()

    def _run_account(self, access_token: str, account: dict[str, Any]) -> dict[str, Any]:
        email = str(account.get("email") or "").strip()
        script = self._pick_script(account, access_token)
        turns = self._build_turns(script)
        messages: list[dict[str, str]] = []
        replies: list[str] = []
        active_token = access_token
        started = time.time()
        try:
            for index, user_text in enumerate(turns, start=1):
                messages.append({"role": "user", "content": user_text})
                try:
                    reply = self._collect_reply(active_token, messages)
                except Exception as error:
                    message = str(error)
                    if is_token_invalid_error(message):
                        refreshed = account_service.refresh_access_token(
                            active_token,
                            force=True,
                            event="chat_keepalive",
                        )
                        if refreshed and refreshed != active_token:
                            active_token = refreshed
                            reply = self._collect_reply(active_token, messages)
                        else:
                            account_service.remove_invalid_token(active_token, "chat_keepalive")
                            raise
                    else:
                        raise
                if not reply:
                    raise RuntimeError("chat_keepalive_empty_reply")
                messages.append({"role": "assistant", "content": reply})
                replies.append(reply[:200])
                if index < len(turns):
                    time.sleep(random.uniform(0.8, 2.2))
            account_service.mark_text_used(active_token)
            account_service.update_account(
                active_token,
                {
                    "last_chat_keepalive_at": _now_iso(),
                    "last_chat_keepalive_error": "",
                    "last_chat_keepalive_prompt_id": str(script.get("id") or ""),
                    "last_chat_keepalive_turns": len(turns),
                },
                quiet=True,
            )
            logger.info({
                "event": "chat_keepalive_success",
                "email": email or None,
                "prompt_id": script.get("id"),
                "turns": len(turns),
                "duration_ms": int((time.time() - started) * 1000),
            })
            return {"ok": True, "token": active_token, "turns": len(turns)}
        except Exception as error:
            message = str(error)[:500]
            # Still stamp last_at to avoid tight failure loops; existing refresh/invalid paths already ran above when applicable.
            try:
                account_service.update_account(
                    active_token,
                    {
                        "last_chat_keepalive_at": _now_iso(),
                        "last_chat_keepalive_error": message,
                        "last_chat_keepalive_prompt_id": str(script.get("id") or ""),
                    },
                    quiet=True,
                )
            except Exception:
                pass
            if is_token_invalid_error(message):
                try:
                    account_service.remove_invalid_token(active_token, "chat_keepalive")
                except Exception:
                    pass
            logger.warning({
                "event": "chat_keepalive_failed",
                "email": email or None,
                "prompt_id": script.get("id"),
                "error": message,
                "duration_ms": int((time.time() - started) * 1000),
            })
            return {"ok": False, "token": active_token, "error": message}

    def run_once(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "selected": 0, "success": 0, "failed": 0}
        now = datetime.now(timezone.utc)
        due = self._due_accounts(now)
        selected = due[: self.batch_size]
        success = 0
        failed = 0
        for index, (token, account, _) in enumerate(selected):
            result = self._run_account(token, account)
            if result.get("ok"):
                success += 1
            else:
                failed += 1
            if index + 1 < len(selected):
                time.sleep(DEFAULT_ACCOUNT_GAP_SECONDS + random.uniform(0, 4))
        summary = {
            "enabled": True,
            "due": len(due),
            "selected": len(selected),
            "success": success,
            "failed": failed,
        }
        if selected:
            logger.info({"event": "chat_keepalive_cycle", **summary})
        return summary

    def start(self, stop_event: threading.Event) -> threading.Thread:
        def worker() -> None:
            # Stagger startup so multi-instance deploys do not align.
            stop_event.wait(random.uniform(15, 90))
            while not stop_event.is_set():
                if not self.enabled:
                    stop_event.wait(self.scan_seconds)
                    continue
                lock_owner = runtime_state.acquire_lock(
                    CHAT_KEEPALIVE_LOCK,
                    ttl_seconds=max(120, int(self.scan_seconds * 2)),
                    allow_memory_fallback=not is_multi_worker_runtime(),
                )
                if not lock_owner:
                    stop_event.wait(min(30.0, self.scan_seconds))
                    continue
                try:
                    self.run_once()
                except Exception as error:
                    logger.warning({"event": "chat_keepalive_cycle_failed", "error": str(error)})
                finally:
                    runtime_state.release_lock(CHAT_KEEPALIVE_LOCK, lock_owner)
                stop_event.wait(self.scan_seconds + random.uniform(0, 30))

        thread = threading.Thread(target=worker, name="chat-keepalive", daemon=True)
        thread.start()
        return thread


chat_keepalive_service = ChatKeepaliveService()


def start_chat_keepalive_worker(stop_event: threading.Event) -> threading.Thread:
    return chat_keepalive_service.start(stop_event)
