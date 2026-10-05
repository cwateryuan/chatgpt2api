"""Exercise the registration UI against mocked APIs; no live accounts are used."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ.get("REGISTRATION_UI_URL", "http://127.0.0.1:3012")
    output = Path(tempfile.gettempdir()) / "chatgpt2api-icloud-ui"
    output.mkdir(exist_ok=True)
    records = [
        {"access_token": "local-http", "email": "local-http@icloud.com", "registration_engine": "http", "type": "Free", "status": "正常", "source_type": "web", "quota": 25, "success": 0, "fail": 0},
        {"access_token": "local-browser", "email": "local-browser@icloud.com", "registration_engine": "browser", "registration_token_mode": "oauth", "type": "Plus", "status": "异常", "source_type": "web", "quota": 5, "success": 0, "fail": 0},
        {"access_token": "imported", "email": "imported@example.com", "type": "Plus", "status": "正常", "source_type": "web", "quota": 25, "success": 0, "fail": 0},
    ]
    config = {
        "enabled": False, "engine": "http", "browser_token_mode": "session", "browser_available": True,
        "browser_version": "test", "browser_error": "", "proxy": "", "total": 3, "threads": 3,
        "mode": "total", "target_quota": 100, "target_available": 10, "check_interval": 5,
        "mail": {"request_timeout": 30, "wait_timeout": 120, "wait_interval": 2, "auto_disable": True, "failure_threshold": 10, "providers": [
            {"id": "apple-main", "type": "apple", "enable": True, "mailboxes": "", "mailboxes_count": 3, "mailboxes_preview": ["lo***e@icloud.com"], "mailboxes_stats": {"unused": 1, "in_use": 0, "used": 1, "failed": 1, "token_invalid": 0}, "health": {}},
        ]},
        "stats": {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": 3}, "logs": [],
    }
    resets = []

    def mock_api(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/auth/login":
            payload = {"ok": True, "role": "admin", "subject_id": "test", "name": "UI test", "version": "test"}
        elif path == "/api/accounts":
            payload = {"items": records, "icloud_stats": None}
        elif path == "/api/accounts/image-cooldown-metrics":
            payload = {"cooldown_minutes": 30, "cooling_accounts": 0, "thawing_within_hour": 0, "next_thaw_at": None, "as_of": 0}
        elif path == "/api/accounts/auto-refresh":
            payload = {"enabled": False, "interval_seconds": 60}
        elif path == "/v1/models":
            payload = {"data": []}
        elif path == "/api/register/apple-pool/reset":
            resets.append(request.post_data_json)
            payload = {"register": config}
        elif path == "/api/register":
            payload = {"register": config}
        elif path == "/api/register/events":
            route.fulfill(status=200, content_type="text/event-stream", body="data: {}\n\n", headers={"Access-Control-Allow-Origin": "*"})
            return
        else:
            payload = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload), headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"})

    with sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
        for width, height in ((1440, 1000), (390, 844)):
            context = browser.new_context(viewport={"width": width, "height": height})
            context.route("http://127.0.0.1:8000/**", mock_api)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/login/")
            page.locator("input").fill("ui-test-key")
            page.get_by_role("button", name="登录", exact=True).click()
            expect(page.get_by_text("local-http@icloud.com", exact=True)).to_be_visible()
            expect(page.get_by_text("本地 web", exact=True)).to_be_visible()
            expect(page.get_by_text("本地 OAuth", exact=True)).to_be_visible()
            page.get_by_role("combobox").filter(has_text="全部类型").click()
            page.get_by_role("option", name="本地", exact=True).click()
            expect(page.locator("tbody tr")).to_have_count(2)
            expect(page.get_by_text("imported@example.com", exact=True)).to_have_count(0)
            page.get_by_role("combobox").filter(has_text="全部状态").click()
            page.get_by_role("option", name="正常", exact=True).click()
            expect(page.locator("tbody tr")).to_have_count(1)
            page.screenshot(path=str(output / f"accounts-{width}.png"), full_page=True)
            page.get_by_role("combobox").filter(has_text="本地").click()
            page.get_by_role("option", name="Plus", exact=True).click()
            expect(page.locator("tbody tr")).to_have_count(1)
            expect(page.get_by_text("imported@example.com", exact=True)).to_be_visible()

            page.goto(base + "/register/")
            expect(page.get_by_text("iCloud 邮箱池导入", exact=True)).to_be_visible()
            expect(page.get_by_role("combobox").filter(has_text="iCloud")).to_be_visible()
            page.get_by_text("iCloud 邮箱池导入", exact=True).scroll_into_view_if_needed()
            page.screenshot(path=str(output / f"register-{width}.png"), full_page=True)
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="重置失败项", exact=True).click()
            expect(page.get_by_text("iCloud 失败项已重置", exact=True)).to_be_visible()
            assert resets[-1] == {"provider_id": "apple-main", "scope": "failed"}
            page.get_by_role("button", name="重置全部", exact=True).click()
            expect(page.get_by_text("iCloud 邮箱池状态已全部重置", exact=True)).to_be_visible()
            assert resets[-1] == {"provider_id": "apple-main", "scope": "all"}
            assert not errors, errors
            context.close()
        browser.close()
    print(f"UI checks passed; screenshots: {output}")


if __name__ == "__main__":
    main()
