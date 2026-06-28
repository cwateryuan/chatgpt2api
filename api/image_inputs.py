from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, TypeGuard
from urllib.parse import unquote, unquote_to_bytes, urlparse

from curl_cffi import requests
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from starlette.datastructures import UploadFile

from services.proxy_service import proxy_settings
from services.protocol.error_response import error_message_from_detail

ImageInput = tuple[bytes, str, str]

MAX_IMAGE_REFERENCE_BYTES = 50 * 1024 * 1024
IMAGE_REFERENCE_FIELDS = {"image", "image[]", "images", "images[]", "image_url", "image_url[]"}
MASK_REFERENCE_FIELDS = {"mask", "mask[]"}


@dataclass(frozen=True)
class ImageSourceRef:
    source_type: str
    value: str
    filename: str = ""
    mime_type: str = ""


ImageSource = ImageSourceRef | UploadFile | ImageInput


class ImageInputError(HTTPException):
    def __init__(self, message: str, diagnostics: list[dict[str, Any]], *, role: str = "image") -> None:
        super().__init__(
            status_code=400,
            detail={
                "error": {
                    "message": str(message or "invalid image input"),
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_image_input",
                }
            },
        )
        self.diagnostics = diagnostics
        self.role = role


def _clean(value: object, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def _is_upload(value: object) -> TypeGuard[UploadFile]:
    return isinstance(value, UploadFile)


def _parse_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = _clean(value).lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    raise HTTPException(status_code=400, detail={"error": "stream must be a boolean"})


def _parse_count(value: object) -> int:
    try:
        count = int(value or 1)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": "n must be an integer"}) from exc
    if count < 1 or count > 4:
        raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
    return count


def _payload_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    prompt = _clean(fields.get("prompt"))
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "prompt is required"})
    payload = {
        "prompt": prompt,
        "model": _clean(fields.get("model"), "gpt-image-2"),
        "n": _parse_count(fields.get("n")),
        "size": _clean(fields.get("size")) or None,
        "quality": _clean(fields.get("quality"), "auto"),
        "response_format": _clean(fields.get("response_format"), "b64_json"),
        "stream": _parse_bool(fields.get("stream")),
    }
    if "client_task_id" in fields:
        payload["client_task_id"] = _clean(fields.get("client_task_id"))
    return payload


def _json_reference_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _base64_source(value: object, filename: str, mime_type: str, source_type: str) -> ImageSourceRef:
    return ImageSourceRef(source_type=source_type, value=str(value or ""), filename=filename, mime_type=mime_type)


def _source_from_object(value: dict[str, Any]) -> list[ImageSource]:
    has_url = "image_url" in value or "url" in value
    if value.get("file_id"):
        raise HTTPException(
            status_code=400,
            detail={"error": "file_id image references are not supported; use image_url instead"},
        )
    inline = value.get("b64_json") or value.get("base64")
    if inline:
        filename = _clean(value.get("filename") or value.get("file_name"), "image.png")
        mime_type = _clean(value.get("mime_type") or value.get("mimeType"), "image/png")
        return [_base64_source(inline, filename, mime_type, "json_b64")]
    if not has_url:
        raise HTTPException(status_code=400, detail={"error": "image reference must include image_url"})
    image_url = value.get("image_url", value.get("url"))
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    return _sources_from_value(image_url)


def _sources_from_value(value: object) -> list[ImageSource]:
    value = _json_reference_value(value)
    if _is_upload(value):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        lower = text.lower()
        if lower.startswith("data:"):
            return [ImageSourceRef(source_type="data_url", value=text)]
        if lower.startswith(("http://", "https://")):
            return [ImageSourceRef(source_type="url", value=text)]
        return [_base64_source(text, "image.png", "image/png", "base64")]
    if isinstance(value, list):
        sources: list[ImageSource] = []
        for item in value:
            sources.extend(_sources_from_value(item))
        return sources
    if isinstance(value, dict):
        return _source_from_object(value)
    if value is None:
        return []
    raise HTTPException(status_code=400, detail={"error": "invalid image reference"})


def _json_image_sources(body: dict[str, Any]) -> list[ImageSource]:
    sources: list[ImageSource] = []
    for key in ("images", "image", "image_url"):
        if key in body:
            sources.extend(_sources_from_value(body.get(key)))
    return sources


def _json_mask_sources(body: dict[str, Any]) -> list[ImageSource]:
    mask = body.get("mask")
    if mask is not None:
        return _sources_from_value(mask)
    return []


async def parse_image_edit_request(request: Request) -> tuple[dict[str, Any], list[ImageSource], list[ImageSource]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid JSON body"}) from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail={"error": "JSON body must be an object"})
        return _payload_from_fields(body), _json_image_sources(body), _json_mask_sources(body)

    form = await request.form()
    fields: dict[str, Any] = {}
    for key in ("client_task_id", "prompt", "model", "n", "size", "quality", "response_format", "stream"):
        value = form.get(key)
        if isinstance(value, str):
            fields[key] = value
    sources: list[ImageSource] = []
    mask_sources: list[ImageSource] = []
    for key, value in form.multi_items():
        if key in IMAGE_REFERENCE_FIELDS:
            sources.extend(_sources_from_value(value))
        elif key in MASK_REFERENCE_FIELDS:
            mask_sources.extend(_sources_from_value(value))
    return _payload_from_fields(fields), sources, mask_sources


def _extension_from_mime(mime_type: str) -> str:
    subtype = mime_type.split("/", 1)[1].split("+", 1)[0] if "/" in mime_type else "png"
    if subtype == "jpeg":
        return "jpg"
    return re.sub(r"[^a-z0-9]+", "", subtype.lower()) or "png"


def _safe_filename(name: str, mime_type: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not cleaned:
        cleaned = fallback
    if "." not in cleaned:
        cleaned = f"{cleaned}.{_extension_from_mime(mime_type)}"
    return cleaned


def _first_bytes_preview(data: bytes | bytearray | None) -> str:
    if not data:
        return ""
    return bytes(data[:32]).hex()


def _diag_url_fields(diag: dict[str, Any], url: str) -> None:
    parsed = urlparse(url)
    diag["url_scheme"] = parsed.scheme
    host = parsed.hostname or ""
    diag["url_host"] = f"{host}:{parsed.port}" if host and parsed.port else host
    diag["url_path"] = parsed.path or "/"


def _diag_data_url_fields(diag: dict[str, Any], url: str) -> None:
    header = str(url or "").split(",", 1)[0]
    mime_type = header.split(";", 1)[0].removeprefix("data:") or "image/png"
    diag["url_scheme"] = "data"
    diag["declared_mime"] = mime_type


def _invalid(message: str) -> ValueError:
    return ValueError(message)


def _decode_base64_data(value: str) -> bytes:
    try:
        return base64.b64decode(str(value).strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _invalid("invalid base64 image data") from exc


def _decode_data_url(url: str) -> ImageInput:
    header, separator, payload = url.partition(",")
    if not separator:
        raise _invalid("invalid data image URL")
    mime_type = header.split(";", 1)[0].removeprefix("data:") or "image/png"
    if not mime_type.startswith("image/"):
        raise _invalid("image_url must point to an image")
    try:
        data = base64.b64decode(payload, validate=True) if ";base64" in header else unquote_to_bytes(payload)
    except (binascii.Error, ValueError) as exc:
        raise _invalid("invalid data image URL") from exc
    return data, f"image_url.{_extension_from_mime(mime_type)}", mime_type


def _response_mime_type(response: requests.Response, parsed_path: str) -> str:
    header_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    guessed_type = mimetypes.guess_type(parsed_path)[0] or ""
    if header_type.startswith("image/"):
        return header_type
    if header_type and header_type not in {"application/octet-stream", "binary/octet-stream"}:
        raise _invalid("image_url must point to an image")
    if guessed_type.startswith("image/"):
        return guessed_type
    if not header_type or header_type in {"application/octet-stream", "binary/octet-stream"}:
        return "image/png"
    raise _invalid("image_url must point to an image")


def _filename_from_url(parsed_path: str, mime_type: str) -> str:
    raw_name = PurePosixPath(unquote(parsed_path)).name
    return _safe_filename(raw_name, mime_type, "image_url")


def _download_image_url_with_diagnostics(url: str, diag: dict[str, Any]) -> ImageInput:
    source = _clean(url)
    _diag_url_fields(diag, source)
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _invalid("image_url must be an http or https URL")
    try:
        response = requests.get(
            source,
            headers={"Accept": "image/*,*/*;q=0.8", "User-Agent": "chatgpt2api image fetcher"},
            timeout=60,
            allow_redirects=True,
            **proxy_settings.build_session_kwargs(),
        )
    except Exception as exc:
        error_name = exc.__class__.__name__
        raise _invalid(f"image_url fetch failed: {error_name}") from exc
    diag["http_status"] = int(response.status_code)
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip()
    if content_type:
        diag["response_content_type"] = content_type
    content_length = _clean(response.headers.get("content-length"))
    if content_length:
        diag["response_content_length"] = int(content_length) if content_length.isdigit() else content_length
    if not 200 <= response.status_code < 300:
        diag["first_bytes_preview"] = _first_bytes_preview(response.content)
        raise _invalid(f"image_url fetch failed: HTTP {response.status_code}")
    if content_length and content_length.isdigit() and int(content_length) > MAX_IMAGE_REFERENCE_BYTES:
        raise _invalid("image_url exceeds 50MB limit")
    data = response.content
    if not data:
        raise _invalid("image_url returned empty content")
    if len(data) > MAX_IMAGE_REFERENCE_BYTES:
        raise _invalid("image_url exceeds 50MB limit")
    diag["bytes"] = len(data)
    try:
        mime_type = _response_mime_type(response, parsed.path)
    except Exception:
        diag["first_bytes_preview"] = _first_bytes_preview(data)
        raise
    return data, _filename_from_url(parsed.path, mime_type), mime_type


def _image_mime(format_name: str) -> str:
    if not format_name:
        return ""
    return Image.MIME.get(format_name.upper(), f"image/{format_name.lower()}")


def _inspect_image(data: bytes, diag: dict[str, Any]) -> tuple[str, str]:
    try:
        with Image.open(BytesIO(data)) as image:
            format_name = str(image.format or "").upper()
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise _invalid(f"image parse failed: {exc}") from exc
    diag["detected_format"] = format_name
    detected_mime = _image_mime(format_name)
    if detected_mime:
        diag["detected_mime"] = detected_mime
    diag["width"] = int(width)
    diag["height"] = int(height)
    return format_name, detected_mime


def _ensure_image_limits(data: bytes, source_name: str) -> None:
    if not data:
        raise _invalid(f"{source_name} is empty")
    if len(data) > MAX_IMAGE_REFERENCE_BYTES:
        raise _invalid(f"{source_name} exceeds 50MB limit")


def _read_source_sync(source: ImageSourceRef, diag: dict[str, Any]) -> ImageInput:
    if source.source_type == "url":
        return _download_image_url_with_diagnostics(source.value, diag)
    if source.source_type == "data_url":
        _diag_data_url_fields(diag, source.value)
        return _decode_data_url(source.value)
    data = _decode_base64_data(source.value)
    return data, source.filename or "image.png", source.mime_type or "image/png"


def _error_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return error_message_from_detail(exc.detail)
    return str(exc) or "invalid image input"


async def read_image_sources_with_diagnostics(
    sources: list[ImageSource],
    *,
    role: str = "image",
) -> tuple[list[ImageInput], list[dict[str, Any]]]:
    images: list[ImageInput] = []
    diagnostics: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        data: bytes | None = None
        diag: dict[str, Any] = {"role": role, "index": index}
        try:
            if _is_upload(source):
                diag["source_type"] = "upload"
                if source.filename:
                    diag["filename"] = source.filename
                if source.content_type:
                    diag["declared_mime"] = source.content_type
                try:
                    data = await source.read()
                finally:
                    await source.close()
                filename = source.filename or "image.png"
                mime_type = source.content_type or "image/png"
            elif isinstance(source, tuple):
                diag["source_type"] = "inline"
                data, filename, mime_type = source
                if filename:
                    diag["filename"] = filename
                if mime_type:
                    diag["declared_mime"] = mime_type
            else:
                diag["source_type"] = source.source_type
                if source.filename:
                    diag["filename"] = source.filename
                if source.mime_type:
                    diag["declared_mime"] = source.mime_type
                data, filename, mime_type = await run_in_threadpool(_read_source_sync, source, diag)
                if filename:
                    diag.setdefault("filename", filename)
                if mime_type:
                    diag.setdefault("declared_mime", mime_type)
            _ensure_image_limits(data, "image file" if role == "image" else "mask file")
            diag["bytes"] = len(data)
            _format, detected_mime = _inspect_image(data, diag)
            if not str(mime_type or "").lower().startswith("image/") and detected_mime:
                mime_type = detected_mime
            images.append((data, filename, mime_type or detected_mime or "image/png"))
            diagnostics.append(diag)
        except Exception as exc:
            message = _error_message(exc)
            diag["parse_error"] = message
            preview = _first_bytes_preview(data)
            if preview:
                diag["first_bytes_preview"] = preview
            diagnostics.append(diag)
            raise ImageInputError(message, diagnostics, role=role) from exc
    if not images:
        raise ImageInputError("image file is required", diagnostics, role=role)
    return images, diagnostics


async def read_image_sources(sources: list[ImageSource]) -> list[ImageInput]:
    images, _diagnostics = await read_image_sources_with_diagnostics(sources)
    return images
