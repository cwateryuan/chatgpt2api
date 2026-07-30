from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse
from urllib.request import urlopen


def find_browser_executable() -> str:
    candidates = [
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", ""),
        os.getenv("CHROME_PATH", ""),
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("chrome") or "",
        shutil.which("chrome.exe") or "",
        shutil.which("msedge") or "",
        shutil.which("msedge.exe") or "",
        str(Path(os.getenv("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.getenv("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.getenv("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.getenv("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("browser_not_found: install Chrome/Edge or set PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")


def browser_proxy_arg(proxy: str) -> str:
    value = str(proxy or "").strip()
    if value.startswith("socks5h://"):
        return "socks5://" + value[len("socks5h://"):]
    return value


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("websocket_closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class DevToolsSocket:
    def __init__(self, websocket_url: str, timeout: float) -> None:
        self.websocket_url = websocket_url
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.next_id = 1

    def __enter__(self) -> "DevToolsSocket":
        parsed = urlparse(self.websocket_url)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 80)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        sock = socket.create_connection((host, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("devtools_websocket_handshake_failed")
        self.sock = sock
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _send_frame(self, text: str) -> None:
        if not self.sock:
            raise RuntimeError("devtools_socket_not_open")
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65_536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> str:
        if not self.sock:
            raise RuntimeError("devtools_socket_not_open")
        while True:
            first, second = _read_exact(self.sock, 2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _read_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _read_exact(self.sock, 8))[0]
            mask = _read_exact(self.sock, 4) if masked else b""
            payload = _read_exact(self.sock, length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 8:
                raise RuntimeError("devtools_websocket_closed")
            if opcode in (1, 2):
                return payload.decode("utf-8", errors="replace")

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        call_id = self.next_id
        self.next_id += 1
        self._send_frame(json.dumps({"id": call_id, "method": method, "params": params or {}}, separators=(",", ":")))
        deadline = time.monotonic() + float(timeout or self.timeout)
        while time.monotonic() < deadline:
            message = json.loads(self._recv_frame())
            if message.get("id") != call_id:
                continue
            if "error" in message:
                raise RuntimeError(f"devtools_{method}_error: {message['error']}")
            return message.get("result") if isinstance(message.get("result"), dict) else {}
        raise RuntimeError(f"devtools_{method}_timeout")


def devtools_json(port: int, path: str, timeout: float) -> Any:
    with urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_devtools(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            devtools_json(port, "/json/version", 2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("browser_devtools_timeout")


def page_websocket(port: int, hosts: tuple[str, ...] = ()) -> str:
    tabs = devtools_json(port, "/json", 5)
    pages = [tab for tab in tabs if isinstance(tab, dict) and tab.get("type") == "page"]
    if not pages:
        raise RuntimeError("no_page_tab")

    def score(tab: dict[str, Any]) -> tuple[int, int]:
        url = str(tab.get("url") or "")
        if hosts and any(host in url for host in hosts):
            return (0, 0)
        if url.startswith(("https://", "http://")):
            return (1, 0)
        if url.startswith("chrome-extension://"):
            return (3, 0)
        return (2, 0)

    for tab in sorted(pages, key=score):
        websocket_url = str(tab.get("webSocketDebuggerUrl") or "")
        if websocket_url:
            return websocket_url
    raise RuntimeError("page_websocket_missing")


def evaluate_json(port: int, expression: str, *, timeout: float, hosts: tuple[str, ...] = ()) -> dict[str, Any]:
    with DevToolsSocket(page_websocket(port, hosts), timeout) as devtools:
        devtools.call("Runtime.enable")
        devtools.call("Page.enable")
        result = devtools.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
    value = ((result.get("result") or {}).get("value") or "{}") if isinstance(result, dict) else "{}"
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        return {"raw": value}


def response_body_for_request(port: int, url: str, *, timeout: float, hosts: tuple[str, ...] = ()) -> str:
    expression = f"""
(async () => {{
  try {{
    const response = await fetch({json.dumps(url)}, {{ credentials: "include", cache: "no-store" }});
    return await response.text();
  }} catch (error) {{
    return String(error && error.message ? error.message : error);
  }}
}})()
"""
    with DevToolsSocket(page_websocket(port, hosts), timeout) as devtools:
        devtools.call("Runtime.enable")
        result = devtools.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
    return str((((result.get("result") or {}) if isinstance(result, dict) else {}).get("value") or ""))


def get_all_cookies(port: int, *, timeout: float, hosts: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    with DevToolsSocket(page_websocket(port, hosts), timeout) as devtools:
        devtools.call("Network.enable")
        result = devtools.call("Network.getAllCookies", timeout=timeout)
    cookies = result.get("cookies") if isinstance(result, dict) else []
    return [item for item in cookies if isinstance(item, dict)] if isinstance(cookies, list) else []


def navigate_to(port: int, url: str, timeout: float) -> None:
    with DevToolsSocket(page_websocket(port), timeout) as devtools:
        devtools.call("Page.enable")
        try:
            devtools.call("Page.navigate", {"url": url}, timeout=min(timeout, 10))
        except Exception:
            pass


def close_browser(port: int, process: subprocess.Popen[Any] | None) -> None:
    try:
        version = devtools_json(port, "/json/version", 3)
        websocket_url = str(version.get("webSocketDebuggerUrl") or "") if isinstance(version, dict) else ""
        if websocket_url:
            with DevToolsSocket(websocket_url, 5) as devtools:
                try:
                    devtools.call("Browser.close", {}, timeout=5)
                except Exception:
                    pass
    except Exception:
        pass
    if process and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def wait_for(
    state_reader: Callable[[], dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float,
    interval: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    last_error = ""
    while time.monotonic() < deadline:
        try:
            last = state_reader()
            if predicate(last):
                return last
        except Exception as error:
            if str(error).startswith(("terminal_auth_error:", "browser_interactive_challenge")):
                raise
            last_error = " ".join(str(error or error.__class__.__name__).split())
        time.sleep(interval)
    if last_error and not last:
        raise RuntimeError(f"wait_timeout: {last_error}")
    raise RuntimeError(f"wait_timeout: url={last.get('url', '')} title={last.get('title', '')}")


def submit_until(
    action: Callable[[], Any],
    state_reader: Callable[[], dict[str, Any]],
    done: Callable[[dict[str, Any]], bool],
    *,
    timeout: float,
    retry_interval: float = 4.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    last_error = ""
    while time.monotonic() < deadline:
        try:
            action()
        except Exception as error:
            if str(error).startswith(("terminal_auth_error:", "browser_interactive_challenge")):
                raise
            last_error = " ".join(str(error or error.__class__.__name__).split())
        check_until = min(deadline, time.monotonic() + retry_interval)
        while time.monotonic() < check_until:
            try:
                last = state_reader()
                if done(last):
                    return last
            except Exception as error:
                if str(error).startswith(("terminal_auth_error:", "browser_interactive_challenge")):
                    raise
                last_error = " ".join(str(error or error.__class__.__name__).split())
            time.sleep(1)
    if last:
        raise RuntimeError(f"submit_retry_timeout: url={last.get('url', '')} title={last.get('title', '')}")
    raise RuntimeError(f"submit_retry_timeout: {last_error}")


def open_page(port: int, url: str, timeout: float) -> None:
    try:
        devtools_json(port, "/json/new?" + quote(url, safe=":/?&=%"), 2)
    except Exception:
        navigate_to(port, url, timeout)
