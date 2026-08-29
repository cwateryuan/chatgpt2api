from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from curl_cffi import requests as curl_requests

from utils.helper import UpstreamHTTPError


@dataclass(frozen=True)
class FailurePolicy:
    scope: str
    capability: str | None
    retryable: bool
    status_code: int
    error_type: str
    verify_account: bool = False

    @property
    def account_failure(self) -> bool:
        """Compatibility/readability alias for the account failure flag."""
        return self.verify_account


@dataclass(frozen=True)
class ImageFailure:
    code: str
    response_code: str | None
    param: str | None
    scope: str
    capability: str | None
    retryable: bool
    retry_after: int | None
    status_code: int
    error_type: str
    verify_account: bool = False
    raw_detail: Any = field(default=None, compare=False, repr=False)
    public_detail: str = field(default="", compare=False, repr=False)

    @property
    def outcome(self) -> str:
        return "text" if self.status_code == 400 else "failure"

    @property
    def switch_account(self) -> bool:
        return self.status_code != 400 and self.capability != "delivery"

    @property
    def account_failure(self) -> bool:
        return self.verify_account

    def with_raw_detail(self, value: Any) -> "ImageFailure":
        return replace(self, raw_detail=value)

    def with_public_detail(self, value: Any) -> "ImageFailure":
        return replace(self, public_detail=_safe_public_text(value))

    def with_response_fields(self, *, response_code: str | None = None, param: str | None = None) -> "ImageFailure":
        return replace(
            self,
            response_code=response_code if response_code is not None else self.response_code,
            param=param if param is not None else self.param,
        )

    def diagnostic_fields(self) -> dict[str, Any]:
        return {
            "failure_code": self.code,
            "failure_response_code": self.response_code,
            "failure_param": self.param,
            "failure_scope": self.scope,
            "failure_capability": self.capability,
            "failure_retryable": self.retryable,
            "failure_account_failure": self.verify_account,
            "failure_retry_after": self.retry_after,
            "status_code": self.status_code,
            "error_type": self.error_type,
        }


FAILURE_POLICIES: dict[str, FailurePolicy] = {
    "upstream_error": FailurePolicy("transient", None, True, 502, "server_error", True),
    "upstream_unavailable": FailurePolicy("transient", None, True, 502, "server_error", True),
    "upstream_connection_failed": FailurePolicy("transient", None, True, 502, "server_error", True),
    "upstream_connection_timeout": FailurePolicy("transient", None, True, 504, "server_error", True),
    "upstream_rate_limited": FailurePolicy("account", "image_generation", True, 429, "rate_limit_error", True),
    "file_upload_throttled": FailurePolicy("account", "file_upload", True, 429, "rate_limit_error", True),
    "image_quota_exhausted": FailurePolicy("account", "image_generation", False, 429, "insufficient_quota", True),
    "auth_invalid": FailurePolicy("account", "auth", False, 401, "authentication_error", True),
    "image_stream_timeout": FailurePolicy("transient", "image_generation", True, 502, "server_error", True),
    "image_stream_interrupted": FailurePolicy("transient", "image_generation", True, 502, "server_error", True),
    "image_poll_timeout": FailurePolicy("transient", "image_generation", True, 502, "server_error", True),
    "image_tool_error": FailurePolicy("account", "image_generation", False, 502, "server_error", True),
    "image_download_failed": FailurePolicy("delivery", None, False, 502, "server_error"),
    "content_policy_violation": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "invalid_image_input": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "upstream_text_reply": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "unsupported_model": FailurePolicy("request", None, False, 400, "invalid_request_error"),
    "no_image_generated": FailurePolicy("request", None, False, 502, "server_error"),
    # Keep the established public status for the request deadline timeout.
    "image_generation_timeout": FailurePolicy("transient", "image_generation", True, 502, "server_error", True),
    "task_interrupted": FailurePolicy("request", None, False, 503, "server_error"),
    "internal_error": FailurePolicy("internal", None, False, 500, "server_error"),
}


FAILURE_CODE_ALIASES = {
    "connection_failed": "upstream_connection_failed",
    "connection_timeout": "upstream_connection_timeout",
    "invalid_access_token": "auth_invalid",
    "moderation_blocked": "content_policy_violation",
    "quota_exhausted": "image_quota_exhausted",
    "rate_limit_exceeded": "upstream_rate_limited",
    "safety_blocked": "content_policy_violation",
    "token_invalid": "auth_invalid",
    "token_invalidated": "auth_invalid",
    "token_revoked": "auth_invalid",
    "token_expired": "auth_invalid",
    "unsupported_image_model": "unsupported_model",
    "upstream_timeout": "image_poll_timeout",
    "throttled": "file_upload_throttled",
    # Public compatibility code; internally this is a request input failure.
    "reference_image_required": "invalid_image_input",
}

_RESPONSE_CODE_ALIASES = {"reference_image_required": "reference_image_required"}
_FAILED_STATUSES = frozenset({"error", "fail", "failed", "limited", "rate_limited", "限流"})
_RATE_LIMIT_CODES = frozenset({"429", "limited", "quota_exhausted", "rate_limit", "rate_limit_exceeded", "rate_limited", "upstream_rate_limited", "限流"})
_TEXT_CODES = frozenset(code for code, policy in FAILURE_POLICIES.items() if policy.status_code == 400)
_REFERENCE_TERMS = (
    "reference_image_required",
    "reference image required",
    "reference image is required",
    "needs a reference image",
    "needs reference image",
    "需要参考图",
    "需要参考图片",
    "没有收到参考图",
    "请求没有收到参考图",
)
_QUOTA_EXHAUSTED_TERMS = (
    "you've hit the free plan limit",
    "you have hit the free plan limit",
    "hit the free plan limit",
    "free plan limit for image generations",
    "limit for image generations requests",
)
_AUTH_INVALID_TERMS = (
    "token_expired",
    "token expired",
    "token_invalidated",
    "token invalidated",
    "token_revoked",
    "token revoked",
    "authentication token is expired",
    "authentication token has been invalidated",
    "provided authentication token is expired",
    "invalidated oauth token",
)


def image_failure(
    code: str | None,
    *,
    retry_after: int | None = None,
    raw_detail: Any = None,
    response_code: str | None = None,
    param: str | None = None,
) -> ImageFailure:
    original = str(code or "upstream_error").strip().lower()
    canonical = FAILURE_CODE_ALIASES.get(original, original)
    if canonical not in FAILURE_POLICIES:
        canonical = "upstream_error"
    policy = FAILURE_POLICIES[canonical]
    if response_code is None and original in _RESPONSE_CODE_ALIASES:
        response_code = _RESPONSE_CODE_ALIASES[original]
    return ImageFailure(
        code=canonical,
        response_code=response_code,
        param=param,
        scope=policy.scope,
        capability=policy.capability,
        retryable=policy.retryable,
        retry_after=retry_after,
        status_code=policy.status_code,
        error_type=policy.error_type,
        verify_account=policy.verify_account,
        raw_detail=raw_detail,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _structured_codes(value: Any) -> set[str]:
    codes: set[str] = set()
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key in ("code", "error_code", "failure_code", "type", "status"):
                candidate = current.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    codes.add(candidate.strip().lower())
            pending.extend(child for child in current.values() if isinstance(child, (Mapping, list, tuple)))
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return codes


def _text_from_mapping(value: Any) -> str:
    item = _mapping(value)
    error = item.get("error")
    candidates = [
        error.get("message") if isinstance(error, Mapping) else None,
        item.get("message"),
        item.get("detail"),
        item.get("error_description"),
        error if isinstance(error, str) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, Mapping):
            nested = _text_from_mapping(candidate)
            if nested:
                return nested
    return ""


def _safe_public_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return _safe_public_text(_text_from_mapping(value))
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (Mapping, list)):
        return ""
    lower = text.lower()
    if any(term in lower for term in ("backend-api/", "upstreamhttperror", "status=", "body=", "chatgpt.com")):
        return ""
    return text


def _message(value: Any) -> Mapping[str, Any]:
    item = _mapping(value)
    message = item.get("message")
    if isinstance(message, Mapping):
        return message
    nested = item.get("v")
    if isinstance(nested, Mapping) and isinstance(nested.get("message"), Mapping):
        return nested["message"]
    return item


def _message_text(message: Mapping[str, Any]) -> str:
    content = _mapping(message.get("content"))
    parts = content.get("parts")
    values: list[str] = []
    if isinstance(parts, list):
        values.extend(str(item).strip() for item in parts if isinstance(item, str) and item.strip())
    if isinstance(content.get("text"), str) and content["text"].strip():
        values.append(content["text"].strip())
    if not values and isinstance(message.get("text"), str):
        values.append(message["text"].strip())
    return "\n".join(values)


def _quota_exhausted_text(text: Any) -> bool:
    normalized = str(text or "").strip().lower()
    return bool(normalized) and any(term in normalized for term in _QUOTA_EXHAUSTED_TERMS)


def is_auth_invalid_error(text: Any) -> bool:
    normalized = str(text or "").strip().lower()
    return bool(normalized) and any(term in normalized for term in _AUTH_INVALID_TERMS)


def _reference_required(text: Any, codes: Any = ()) -> bool:
    candidates = {str(code or "").strip().lower() for code in codes}
    if "reference_image_required" in candidates:
        return True
    normalized = str(text or "").strip().lower()
    return any(term in normalized for term in _REFERENCE_TERMS)


def _failure_from_codes(codes: Any, *, raw_detail: Any = None, param: str | None = None) -> ImageFailure | None:
    normalized = {str(code or "").strip().lower() for code in (codes if isinstance(codes, (set, list, tuple, frozenset)) else (codes,)) if str(code or "").strip()}
    if "reference_image_required" in normalized:
        return image_failure("invalid_image_input", raw_detail=raw_detail, response_code="reference_image_required", param=param or "image")
    if normalized.intersection({"content_policy_violation", "moderation_blocked", "safety_blocked"}):
        return image_failure("content_policy_violation", raw_detail=raw_detail)
    if normalized.intersection({"invalid_access_token", "token_invalid", "token_invalidated", "token_revoked", "token_expired"}):
        return image_failure("auth_invalid", raw_detail=raw_detail)
    if normalized.intersection({"insufficient_quota", "quota_exhausted", "image_quota_exhausted"}):
        return image_failure("image_quota_exhausted", raw_detail=raw_detail)
    if normalized.intersection(_RATE_LIMIT_CODES):
        return image_failure("upstream_rate_limited", raw_detail=raw_detail)
    for candidate in normalized:
        canonical = FAILURE_CODE_ALIASES.get(candidate, candidate)
        if canonical in FAILURE_POLICIES:
            return image_failure(canonical, raw_detail=raw_detail)
    return None


def classify_upstream_http_error(exc: UpstreamHTTPError) -> ImageFailure:
    body = getattr(exc, "body", None)
    codes = _structured_codes(body)
    raw_message = _text_from_mapping(body)
    param = None
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            param = str(error.get("param") or "").strip() or None
    status = int(getattr(exc, "status_code", 502) or 502)
    context = str(getattr(exc, "context", "") or "").strip().lower()
    if status == 400:
        if _reference_required(raw_message, codes):
            return image_failure("invalid_image_input", raw_detail=body, response_code="reference_image_required", param=param or "image").with_public_detail(raw_message)
        failure = _failure_from_codes(codes, raw_detail=body, param=param)
        if failure is None or failure.status_code != 400:
            failure = image_failure("invalid_image_input", raw_detail=body, param=param)
        return failure.with_public_detail(raw_message)
    if status == 401 or is_auth_invalid_error(raw_message):
        return image_failure("auth_invalid", raw_detail=body)
    if _quota_exhausted_text(raw_message):
        return image_failure("image_quota_exhausted", raw_detail=body)
    if status == 429:
        code = "file_upload_throttled" if "file" in context and "upload" in context else "upstream_rate_limited"
        return image_failure(code, retry_after=getattr(exc, "retry_after", None), raw_detail=body)
    if status in {408, 504}:
        return replace(image_failure("upstream_connection_timeout", raw_detail=body), status_code=status)
    if status >= 500 or status in {403, 423}:
        return replace(image_failure("upstream_unavailable", raw_detail=body), status_code=status)
    return replace(image_failure("upstream_error", raw_detail=body), status_code=status)


def classify_message_facts(
    *,
    role: str = "",
    content_type: str = "",
    status: str = "",
    end_turn: bool = False,
    is_error: bool = False,
    blocked: bool = False,
    has_image_output: bool = False,
    has_text: bool = False,
    codes: Any = (),
    raw_detail: Any = None,
    param: str | None = None,
) -> ImageFailure | None:
    if has_image_output:
        return None
    text = str(raw_detail or "")
    if _reference_required(text, codes):
        return image_failure("invalid_image_input", raw_detail=raw_detail, response_code="reference_image_required", param=param or "image").with_public_detail(raw_detail)
    if _quota_exhausted_text(text):
        return image_failure("image_quota_exhausted", raw_detail=raw_detail).with_public_detail(raw_detail)
    if is_auth_invalid_error(text):
        return image_failure("auth_invalid", raw_detail=raw_detail)
    if blocked:
        return image_failure("content_policy_violation", raw_detail=raw_detail)
    structured = _failure_from_codes(codes, raw_detail=raw_detail, param=param)
    if structured is not None:
        return structured
    normalized_role = str(role or "").strip().lower()
    normalized_type = str(content_type or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    if normalized_role == "assistant" and normalized_type in {"text", "code"} and (end_turn or normalized_status in {"finished_successfully", "finished", "completed", "done"}) and has_text:
        return image_failure("upstream_text_reply", raw_detail=raw_detail).with_public_detail(raw_detail)
    if normalized_role == "tool" and normalized_type == "system_error":
        return image_failure("image_tool_error", raw_detail=raw_detail)
    if is_error or normalized_status in _FAILED_STATUSES:
        return image_failure("upstream_rate_limited" if normalized_status in _RATE_LIMIT_CODES else "upstream_error", raw_detail=raw_detail)
    return None


def classify_upstream_message(value: Any) -> ImageFailure | None:
    outer = _mapping(value)
    event_type = str(outer.get("type") or "").strip().lower()
    if event_type in {"response.failed", "response.incomplete"}:
        response = _mapping(outer.get("response"))
        failure = _failure_from_codes(_structured_codes(outer), raw_detail=outer) or image_failure("image_tool_error", raw_detail=outer)
        return failure.with_public_detail(response.get("error") or outer.get("error"))
    message = _message(value)
    author = _mapping(message.get("author"))
    metadata = _mapping(message.get("metadata"))
    content = _mapping(message.get("content"))
    raw_detail = _message_text(message)
    return classify_message_facts(
        role=str(author.get("role") or ""),
        content_type=str(content.get("content_type") or ""),
        status=str(message.get("status") or metadata.get("status") or ""),
        end_turn=message.get("end_turn") is True,
        is_error=message.get("is_error") is True or metadata.get("is_error") is True or bool(message.get("error")) or bool(metadata.get("error")),
        blocked=message.get("blocked") is True or metadata.get("blocked") is True,
        has_text=bool(raw_detail),
        codes=_structured_codes(message),
        raw_detail=raw_detail,
        param=str(message.get("param") or metadata.get("param") or "").strip() or None,
    )


def classify_task_failure(task: Any) -> ImageFailure | None:
    image_message = _mapping(_mapping(task).get("image_gen_message"))
    if not image_message:
        return None
    failure = classify_upstream_message(image_message)
    if failure is None:
        return None
    message = _message(image_message)
    role = str(_mapping(message.get("author")).get("role") or "").strip().lower()
    content_type = str(_mapping(message.get("content")).get("content_type") or "").strip().lower()
    if role == "tool" and content_type != "system_error":
        return None
    if failure.code == "image_tool_error" and not (role == "tool" and content_type == "system_error") and not failure.response_code:
        return image_failure("upstream_error", raw_detail=failure.raw_detail)
    return failure


def _current_turn_messages(data: Any) -> list[Mapping[str, Any]]:
    mapping = _mapping(_mapping(data).get("mapping"))
    messages: list[Mapping[str, Any]] = []
    for node in mapping.values():
        message = _mapping(node).get("message")
        if isinstance(message, Mapping):
            messages.append(message)
    messages.sort(key=lambda item: float(item.get("create_time") or 0.0) if str(item.get("create_time") or "").replace(".", "", 1).isdigit() else 0.0)
    last_user = max((i for i, item in enumerate(messages) if str(_mapping(item.get("author")).get("role") or "").lower() == "user"), default=-1)
    return messages[last_user + 1:]


def _message_has_image_output(message: Mapping[str, Any]) -> bool:
    content = _mapping(message.get("content"))
    if content.get("content_type") == "multimodal_text":
        return any(isinstance(part, Mapping) and (part.get("content_type") == "image_asset_pointer" or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))) for part in content.get("parts") or [])
    return False


def classify_conversation_failure(data: Any) -> ImageFailure | None:
    messages = _current_turn_messages(data)
    if any(_message_has_image_output(message) for message in messages):
        return None
    failure: ImageFailure | None = None
    for message in messages:
        failure = merge_message_failure(failure, classify_upstream_message(message))
    return failure


def merge_message_failure(current: ImageFailure | None, candidate: ImageFailure | None) -> ImageFailure | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    priorities = {
        "auth_invalid": 8, "image_quota_exhausted": 7, "upstream_rate_limited": 6,
        "file_upload_throttled": 6, "content_policy_violation": 5, "invalid_image_input": 5,
        "unsupported_model": 5, "image_tool_error": 3, "upstream_text_reply": 2,
    }
    winner, other = (candidate, current) if priorities.get(candidate.code, 1) > priorities.get(current.code, 1) else (current, candidate)
    if not winner.raw_detail and other.raw_detail:
        winner = winner.with_raw_detail(other.raw_detail)
    if not winner.public_detail and other.public_detail:
        winner = winner.with_public_detail(other.public_detail)
    if not winner.response_code and other.response_code:
        winner = winner.with_response_fields(response_code=other.response_code, param=other.param)
    return winner


def classify_image_exception(exc: BaseException, *, code: str | None = None) -> ImageFailure:
    existing = getattr(exc, "failure", None)
    if isinstance(existing, ImageFailure):
        return existing
    if isinstance(exc, UpstreamHTTPError):
        failure = classify_upstream_http_error(exc)
    else:
        candidate = code or getattr(exc, "code", None)
        if candidate:
            failure = image_failure(candidate, raw_detail=str(exc), response_code=getattr(exc, "response_code", None), param=getattr(exc, "param", None))
        elif isinstance(exc, (TimeoutError, curl_requests.exceptions.Timeout)):
            failure = image_failure("upstream_connection_timeout", raw_detail=str(exc))
        elif isinstance(exc, (ConnectionError, curl_requests.exceptions.RequestException)):
            failure = image_failure("upstream_connection_failed", raw_detail=str(exc))
        elif _quota_exhausted_text(exc):
            failure = image_failure("image_quota_exhausted", raw_detail=str(exc)).with_public_detail(str(exc))
        elif is_auth_invalid_error(exc):
            failure = image_failure("auth_invalid", raw_detail=str(exc))
        else:
            failure = image_failure("internal_error", raw_detail=str(exc))
    try:
        setattr(exc, "failure", failure)
    except (AttributeError, TypeError):
        pass
    return failure


def _public_upstream_text(failure: ImageFailure, error: BaseException | None = None) -> str:
    candidates: list[Any] = [failure.public_detail]
    if error is not None:
        candidates.extend([getattr(error, "raw_upstream_message", None), getattr(error, "raw_error", None)])
    candidates.append(failure.raw_detail)
    for candidate in candidates:
        text = _safe_public_text(candidate)
        if text:
            return text
    return ""


def public_image_error_message(failure_or_message: ImageFailure | str, error: BaseException | None = None) -> str:
    if isinstance(failure_or_message, ImageFailure):
        failure = failure_or_message
        text = _public_upstream_text(failure, error)
        if text:
            return text
        if failure.code == "image_poll_timeout":
            return "Image generation timed out. Please try again."
        if failure.code == "image_tool_error":
            return "The image generation tool encountered an error. Please try again."
        if failure.code == "invalid_image_input":
            return "A reference image is required for this request. Upload image or image_url, or use /v1/images/edits."
        return "The image generation request failed. Please try again later."
    text = _safe_public_text(failure_or_message)
    return text or "The image generation request failed. Please try again later."


class ImageFailureError(RuntimeError):
    failure_code = "upstream_error"

    def __init__(self, message: str = "", *, failure: ImageFailure | None = None) -> None:
        self.failure = failure or image_failure(self.failure_code, raw_detail=message)
        super().__init__(message or public_image_error_message(self.failure, self))
        self.status_code = self.failure.status_code
        self.error_type = self.failure.error_type
        self.code = self.failure.response_code or self.failure.code
        self.param = self.failure.param


class ImageGenerationError(ImageFailureError):
    def __init__(
        self,
        message: str = "",
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        *,
        failure: ImageFailure | None = None,
        raw_error: str | None = None,
        upstream_error: str = "",
        raw_upstream_message: str = "",
        image_attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        resolved = failure or image_failure(code, raw_detail=raw_error or message, param=param)
        # ``throttled`` was the established public code for the exhausted file
        # upload path; retain it while keeping the richer internal classification.
        if failure is None and code == "throttled" and resolved.response_code is None:
            resolved = replace(resolved, response_code="throttled")
        if failure is None and code not in FAILURE_POLICIES and code not in FAILURE_CODE_ALIASES and code != "reference_image_required":
            resolved = replace(resolved, status_code=status_code, error_type=error_type)
        elif failure is None and code and status_code != 502 and resolved.status_code != status_code:
            resolved = replace(resolved, status_code=status_code, error_type=error_type)
        super().__init__(message, failure=resolved)
        self.status_code = resolved.status_code
        self.error_type = resolved.error_type
        self.code = resolved.response_code or resolved.code
        self.param = resolved.param or param
        self.account_email = account_email
        self.conversation_id = conversation_id
        self.raw_error = str(message if raw_error is None else raw_error or "").strip()
        self.upstream_error = upstream_error
        self.raw_upstream_message = raw_upstream_message
        self.image_attempts = [dict(item) for item in image_attempts or [] if isinstance(item, Mapping)]

    @property
    def public_error(self) -> str:
        return public_image_error_message(self.failure, self)

    def to_openai_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.public_error,
            "type": self.error_type,
            "param": self.param,
            "code": self.code,
        }
        if self.account_email:
            error["account_email"] = self.account_email
        return {"error": error}


class ImageGenerationTimeoutError(ImageGenerationError):
    def __init__(self, timeout_secs: float, *, account_email: str = "", conversation_id: str = "") -> None:
        super().__init__(
            f"ChatGPT 生图超时（已等待 {timeout_secs:g} 秒）。",
            failure=image_failure("image_generation_timeout"),
            account_email=account_email,
            conversation_id=conversation_id,
        )
        self.timeout_secs = timeout_secs


def image_failure_priority(failure: ImageFailure) -> int:
    return {
        "auth_invalid": 8,
        "image_quota_exhausted": 7,
        "upstream_rate_limited": 6,
        "file_upload_throttled": 6,
        "content_policy_violation": 5,
        "invalid_image_input": 5,
        "image_tool_error": 3,
        "upstream_text_reply": 2,
    }.get(failure.code, 1)
