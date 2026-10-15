"""Map the browser egress IP to an IANA timezone.

The lookup uses the same proxy as the Playwright page, including a SOCKS5
Cloudflare WARP proxy. It never sees account tokens or prompts.
"""
from __future__ import annotations

import json
import re
import urllib.request
from urllib.parse import urlparse

# Geo services see the proxy's egress. Two providers so one being down does not
# leave the page on the container timezone while the IP is elsewhere.
GEO_LOOKUP_URLS = (
    "https://ipwho.is/",
    "http://ip-api.com/json/?fields=status,countryCode,city,timezone,query",
)
_IANA_TIMEZONE = re.compile(r"^[A-Za-z]+(?:[-+][A-Za-z0-9]+)?(?:/[A-Za-z0-9_+-]+){1,2}$")


def _proxy_opener(proxy_url: str) -> urllib.request.OpenerDirector:
    """Open URLs through the same proxy the browser uses, including SOCKS5 WARP."""
    if not proxy_url:
        return urllib.request.build_opener()
    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "http").lower()
    if scheme.startswith("socks"):
        try:
            import socks  # type: ignore
            from sockshandler import SocksiPyHandler  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PySocks is required for a socks sentinel proxy") from exc
        kind = {
            "socks5": socks.SOCKS5,
            "socks5h": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "socks4a": socks.SOCKS4,
        }.get(scheme, socks.SOCKS5)
        remote_dns = scheme in {"socks5h", "socks4a"}
        handler = SocksiPyHandler(
            kind,
            parsed.hostname or "",
            parsed.port or 1080,
            remote_dns,
            parsed.username or None,
            parsed.password or None,
        )
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))


def timezone_from_payload(payload: object) -> tuple[str, str]:
    """Return (iana timezone, egress ip) from one geo response, or empty strings."""
    if not isinstance(payload, dict):
        return "", ""
    if payload.get("success") is False or payload.get("status") in {"fail", "error"}:
        return "", ""
    timezone_value = payload.get("timezone")
    timezone_id = (
        str(timezone_value.get("id") or "")
        if isinstance(timezone_value, dict)
        else str(timezone_value or payload.get("timezone_id") or "")
    ).strip()
    if not _IANA_TIMEZONE.fullmatch(timezone_id):
        return "", ""
    ip = str(payload.get("ip") or payload.get("query") or "").strip()
    return timezone_id, ip


def resolve_page_timezone(configured: str, proxy_url: str) -> tuple[str, str, str]:
    """Return (timezone, source, egress ip).

    An explicit timezone always wins. Otherwise the proxy egress is queried.
    An unknown lookup returns source ``unresolved`` so the page keeps the
    container timezone instead of inventing one.
    """
    explicit = str(configured or "").strip()
    if explicit:
        return explicit, "configured", ""
    timezone_id, ip = detect_egress_timezone(proxy_url)
    if timezone_id:
        return timezone_id, "egress", ip
    return "", "unresolved", ""


def detect_egress_timezone(proxy_url: str, timeout: float = 8.0) -> tuple[str, str]:
    """Look up the timezone of the proxy egress. Returns ("", "") when unknown."""
    opener = _proxy_opener(proxy_url)
    request_headers = {"Accept": "application/json", "User-Agent": "chatgpt2api-sentinel-solver"}
    for url in GEO_LOOKUP_URLS:
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            print(f"[sentinel-solver] egress lookup failed via {url}: {exc}", flush=True)
            continue
        timezone_id, ip = timezone_from_payload(payload)
        if timezone_id:
            return timezone_id, ip
    return "", ""
