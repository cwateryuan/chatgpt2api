"""Browser identity helpers for registration hardening.

Email-seeded fingerprint: same email => stable hardware/GPU/screen/UA profile;
different emails diverge. UA spoofing is packaged with matching Client Hints.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import asdict, dataclass
from typing import Any


_GPU_POOL: tuple[tuple[str, str], ...] = (
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti (0x00002182) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
)

_SCREEN_POOL: tuple[tuple[int, int], ...] = (
    (1920, 1080),
    (1536, 864),
    (1366, 768),
    (1600, 900),
    (2560, 1440),
    (1440, 900),
)
_CORE_POOL: tuple[int, ...] = (4, 6, 8, 12, 16)
_MEM_POOL: tuple[int, ...] = (4, 8, 16)
_PLATFORM_VERSION_POOL: tuple[str, ...] = ("10.0.0", "15.0.0", "19.0.0")

# Spoofed Chrome profiles for UA + Client Hints. Prefer majors commonly supported
# by curl_cffi impersonate when later reusing account fp on HTTP paths.
_CHROME_PROFILES: tuple[dict[str, str], ...] = (
    {"major": "131", "full_version": "131.0.6778.205", "impersonate": "chrome131"},
    {"major": "136", "full_version": "136.0.7103.113", "impersonate": "chrome136"},
    {"major": "138", "full_version": "138.0.7204.183", "impersonate": "chrome"},
    {"major": "142", "full_version": "142.0.7444.175", "impersonate": "chrome142"},
    {"major": "145", "full_version": "145.0.7632.117", "impersonate": "chrome145"},
    {"major": "146", "full_version": "146.0.7680.80", "impersonate": "chrome146"},
    {"major": "149", "full_version": "149.0.7727.55", "impersonate": "chrome"},
    {"major": "150", "full_version": "150.0.7871.181", "impersonate": "chrome"},
)


@dataclass(frozen=True)
class BrowserIdentity:
    email: str
    seed: int
    screen_width: int
    screen_height: int
    window_width: int
    window_height: int
    hardware_concurrency: int
    device_memory: int
    gpu_vendor: str
    gpu_renderer: str
    platform_version: str
    chrome_major: str
    chrome_full_version: str
    impersonate: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_full_version_list: str

    def window_size_arg(self) -> str:
        return f"{self.window_width},{self.window_height}"

    def as_fingerprint_updates(self) -> dict[str, str]:
        return {
            "screen_width": str(self.screen_width),
            "screen_height": str(self.screen_height),
            "page_width": str(self.window_width),
            "page_height": str(self.window_height),
            "hardware_concurrency": str(self.hardware_concurrency),
            "device_memory": str(self.device_memory),
            "gpu_vendor": self.gpu_vendor,
            "gpu_renderer": self.gpu_renderer,
            "platform_version": self.platform_version,
            "identity_seed": str(self.seed),
            "major": self.chrome_major,
            "full_version": self.chrome_full_version,
            "user_agent": self.user_agent,
            "impersonate": self.impersonate,
            "sec_ch_ua": self.sec_ch_ua,
            "sec_ch_ua_full_version_list": self.sec_ch_ua_full_version_list,
            "sec_ch_ua_platform": '"Windows"',
            "sec_ch_ua_mobile": "?0",
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def email_seed(email: str) -> int:
    digest = hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def parse_chrome_version(product: str) -> tuple[str, str]:
    text = str(product or "")
    match = re.search(r"(?:HeadlessChrome|Chrome|Chromium)/([0-9]+(?:\.[0-9]+)*)", text)
    if not match:
        return "142", "142.0.0.0"
    full = match.group(1)
    major = full.split(".", 1)[0]
    return major or "142", full or "142.0.0.0"


def build_user_agent(major: str, full_version: str = "") -> str:
    # HTTP UA commonly uses major.0.0.0; full build lives in Client Hints.
    _ = full_version
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def build_sec_ch_ua(major: str) -> str:
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="24"'


def build_sec_ch_ua_full_version_list(major: str, full_version: str) -> str:
    full = full_version or f"{major}.0.0.0"
    return (
        f'"Chromium";v="{full}", '
        f'"Google Chrome";v="{full}", '
        '"Not_A Brand";v="24.0.0.0"'
    )


def build_user_agent_metadata(major: str, full_version: str, platform_version: str) -> dict[str, Any]:
    full = full_version or f"{major}.0.0.0"
    return {
        "brands": [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            {"brand": "Not_A Brand", "version": "24"},
        ],
        "fullVersionList": [
            {"brand": "Chromium", "version": full},
            {"brand": "Google Chrome", "version": full},
            {"brand": "Not_A Brand", "version": "24.0.0.0"},
        ],
        "platform": "Windows",
        "platformVersion": platform_version or "10.0.0",
        "architecture": "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
    }


def _chrome_profiles_for_runtime(runtime_major: str = "") -> tuple[dict[str, str], ...]:
    """Prefer spoofed majors <= real Chromium major to reduce engine mismatch."""
    try:
        runtime = int(str(runtime_major or "").strip() or "0")
    except ValueError:
        runtime = 0
    if runtime <= 0:
        return _CHROME_PROFILES
    filtered = [item for item in _CHROME_PROFILES if int(item["major"]) <= runtime]
    return tuple(filtered) or _CHROME_PROFILES


def build_browser_identity(email: str, *, runtime_product: str = "") -> BrowserIdentity:
    seed = email_seed(email)
    rng = random.Random(seed)
    screen_w, screen_h = rng.choice(_SCREEN_POOL)
    window_w = max(1000, screen_w - 40 - rng.randint(0, 120))
    window_h = max(700, screen_h - 120 - rng.randint(0, 120))
    gpu_vendor, gpu_renderer = rng.choice(_GPU_POOL)
    runtime_major, _runtime_full = parse_chrome_version(runtime_product)
    profile = dict(rng.choice(_chrome_profiles_for_runtime(runtime_major)))
    major = str(profile["major"])
    full_version = str(profile["full_version"])
    impersonate = str(profile.get("impersonate") or "chrome")
    user_agent = build_user_agent(major, full_version)
    return BrowserIdentity(
        email=str(email or "").strip().lower(),
        seed=seed,
        screen_width=screen_w,
        screen_height=screen_h,
        window_width=window_w,
        window_height=window_h,
        hardware_concurrency=rng.choice(_CORE_POOL),
        device_memory=rng.choice(_MEM_POOL),
        gpu_vendor=gpu_vendor,
        gpu_renderer=gpu_renderer,
        platform_version=rng.choice(_PLATFORM_VERSION_POOL),
        chrome_major=major,
        chrome_full_version=full_version,
        impersonate=impersonate,
        user_agent=user_agent,
        sec_ch_ua=build_sec_ch_ua(major),
        sec_ch_ua_full_version_list=build_sec_ch_ua_full_version_list(major, full_version),
    )


def stealth_and_identity_script(identity: BrowserIdentity, *, accept_language: str = "") -> str:
    """Init script: stealth patches + hardware/WebGL/Canvas/Audio noise + UA surface."""
    languages = [part.strip() for part in str(accept_language or "en-US,en;q=0.9").split(",") if part.strip()]
    language_codes: list[str] = []
    for item in languages:
        code = item.split(";", 1)[0].strip()
        if code and code not in language_codes:
            language_codes.append(code)
    if not language_codes:
        language_codes = ["en-US", "en"]
    primary_language = language_codes[0]
    return f"""(() => {{
  try {{
    const def = (obj, prop, value) => {{
      try {{
        Object.defineProperty(obj, prop, {{ get: () => value, configurable: true }});
      }} catch (_) {{}}
    }};

    // Basic automation markers
    def(navigator, 'webdriver', undefined);
    try {{ delete Navigator.prototype.webdriver; }} catch (_) {{}}
    def(navigator, 'webdriver', false);
    if (!window.chrome) {{
      window.chrome = {{ runtime: {{}}, app: {{}}, csi: () => ({{}}), loadTimes: () => ({{}}) }};
    }}
    def(navigator, 'userAgent', {json.dumps(identity.user_agent)});
    def(navigator, 'appVersion', {json.dumps(identity.user_agent.replace("Mozilla/", ""))});
    def(navigator, 'language', {json.dumps(primary_language)});
    def(navigator, 'languages', Object.freeze({json.dumps(language_codes)}));
    def(navigator, 'plugins', [1, 2, 3, 4, 5]);
    def(navigator, 'hardwareConcurrency', {identity.hardware_concurrency});
    def(navigator, 'deviceMemory', {identity.device_memory});
    def(navigator, 'platform', 'Win32');
    def(navigator, 'maxTouchPoints', 0);

    const sw = {identity.screen_width};
    const sh = {identity.screen_height};
    def(screen, 'width', sw);
    def(screen, 'height', sh);
    def(screen, 'availWidth', sw);
    def(screen, 'availHeight', Math.max(600, sh - 40));
    def(screen, 'colorDepth', 24);
    def(screen, 'pixelDepth', 24);
    def(window, 'outerWidth', {identity.window_width});
    def(window, 'outerHeight', {identity.window_height});
    def(window, 'devicePixelRatio', 1);

    // Permissions query noise for notifications
    try {{
      const originalQuery = window.Permissions && Permissions.prototype.query;
      if (originalQuery) {{
        Permissions.prototype.query = function(parameters) {{
          if (parameters && parameters.name === 'notifications') {{
            return Promise.resolve({{ state: Notification.permission }});
          }}
          return originalQuery.call(this, parameters);
        }};
      }}
    }} catch (_) {{}}

    // WebGL vendor/renderer
    const vendor = {json.dumps(identity.gpu_vendor)};
    const renderer = {json.dumps(identity.gpu_renderer)};
    const patchGL = (proto) => {{
      if (!proto || !proto.getParameter) return;
      const original = proto.getParameter;
      proto.getParameter = function(parameter) {{
        if (parameter === 37445) return vendor;
        if (parameter === 37446) return renderer;
        if (parameter === 7936) return vendor;
        if (parameter === 7937) return renderer;
        return original.apply(this, arguments);
      }};
    }};
    try {{ patchGL(WebGLRenderingContext && WebGLRenderingContext.prototype); }} catch (_) {{}}
    try {{ patchGL(WebGL2RenderingContext && WebGL2RenderingContext.prototype); }} catch (_) {{}}

    // Canvas / Audio micro-noise keyed by identity seed
    let seed = {identity.seed} >>> 0;
    const rnd = () => {{
      seed = (Math.imul(1664525, seed) + 1013904223) >>> 0;
      return seed / 4294967296;
    }};
    const noisify = (canvas) => {{
      try {{
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        const w = canvas.width, h = canvas.height;
        if (!w || !h) return;
        const img = ctx.getImageData(0, 0, w, h);
        for (let i = 0; i < img.data.length; i += 4) {{
          if (rnd() < 0.02) img.data[i] = img.data[i] ^ (rnd() < 0.5 ? 1 : 0);
        }}
        ctx.putImageData(img, 0, 0);
      }} catch (_) {{}}
    }};
    try {{
      const toDataURL = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function() {{
        noisify(this);
        return toDataURL.apply(this, arguments);
      }};
    }} catch (_) {{}}
    try {{
      const toBlob = HTMLCanvasElement.prototype.toBlob;
      if (toBlob) {{
        HTMLCanvasElement.prototype.toBlob = function() {{
          noisify(this);
          return toBlob.apply(this, arguments);
        }};
      }}
    }} catch (_) {{}}
    try {{
      const original = AnalyserNode.prototype.getFloatFrequencyData;
      AnalyserNode.prototype.getFloatFrequencyData = function(array) {{
        original.apply(this, arguments);
        for (let i = 0; i < array.length; i += 1) {{
          array[i] = array[i] + (rnd() - 0.5) * 1e-4;
        }}
      }};
    }} catch (_) {{}}
  }} catch (_) {{}}
}})();"""


def human_type_js(selector_hint: str, text: str, min_delay_ms: int = 40, max_delay_ms: int = 120) -> str:
    """Type text with per-character delays (used by form helpers if needed)."""
    return f"""
(async () => {{
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const value = {json.dumps(text)};
  const nodes = [...document.querySelectorAll({json.dumps(selector_hint)})];
  const target = nodes.find((el) => el && !el.disabled && el.offsetParent !== null) || nodes[0];
  if (!target) throw new Error('human_type_target_missing');
  target.focus();
  target.value = '';
  target.dispatchEvent(new Event('input', {{ bubbles: true }}));
  for (const ch of value) {{
    target.value += ch;
    target.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: ch, inputType: 'insertText' }}));
    await sleep({min_delay_ms} + Math.floor(Math.random() * Math.max(1, {max_delay_ms - min_delay_ms})));
  }}
  target.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return JSON.stringify({{ ok: true, length: value.length }});
}})()
"""
