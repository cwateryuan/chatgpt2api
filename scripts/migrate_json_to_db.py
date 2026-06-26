from __future__ import annotations

import json
import os
import hashlib
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.storage.database_storage import DatabaseStorageBackend  # noqa: E402

DATA_DIR = ROOT / "data"
IMAGE_INDEX_FILE = DATA_DIR / "image_index.json"
TAGS_FILE = DATA_DIR / "image_tags.json"


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _backup(path: Path) -> None:
    if not path.exists():
        return
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        backup_path.write_bytes(path.read_bytes())


def _load_list(path: Path, *, unwrap_items: bool = False) -> list[dict[str, Any]]:
    raw = _read_json(path, [])
    if unwrap_items and isinstance(raw, dict):
        raw = raw.get("items", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _migrate_accounts(db: DatabaseStorageBackend) -> int:
    path = DATA_DIR / "accounts.json"
    items = _load_list(path)
    for item in items:
        db.upsert_account(item)
    _backup(path)
    return len(items)


def _migrate_auth_keys(db: DatabaseStorageBackend) -> int:
    path = DATA_DIR / "auth_keys.json"
    items = _load_list(path, unwrap_items=True)
    db.save_auth_keys(items)
    _backup(path)
    return len(items)


def _migrate_images(db: DatabaseStorageBackend) -> int:
    raw = _read_json(IMAGE_INDEX_FILE, {})
    items = raw.get("items") if isinstance(raw, dict) else {}
    if not isinstance(items, dict):
        items = {}
    count = 0
    for rel, item in items.items():
        if not isinstance(item, dict):
            continue
        db.save_image({**item, "rel": str(item.get("rel") or rel), "path": str(item.get("path") or rel)})
        count += 1
    _backup(IMAGE_INDEX_FILE)
    return count


def _migrate_image_tags(db: DatabaseStorageBackend) -> int:
    raw = _read_json(TAGS_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    count = 0
    for rel, tags in raw.items():
        if not isinstance(tags, list):
            continue
        db.set_image_tags(str(rel), [str(tag) for tag in tags if str(tag or "").strip()])
        count += 1
    _backup(TAGS_FILE)
    return count


def _migrate_logs(db: DatabaseStorageBackend) -> int:
    path = DATA_DIR / "logs.jsonl"
    if not path.exists():
        return 0
    count = 0
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            item = json.loads(raw_line)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        if not str(item.get("id") or "").strip():
            payload = f"{line_number}:{raw_line}".encode("utf-8", errors="ignore")
            item["id"] = hashlib.sha1(payload).hexdigest()[:24]
        if item is None:
            continue
        db.add_log(item)
        count += 1
    _backup(path)
    return count


def _migrate_named_items(db: DatabaseStorageBackend, filename: str, table: str) -> int:
    path = DATA_DIR / filename
    raw = _read_json(path, [])
    if isinstance(raw, dict) and "base_url" in raw:
        items = [raw]
    elif isinstance(raw, list):
        items = [item for item in raw if isinstance(item, dict)]
    else:
        items = []
    db.save_named_items(table, items)
    _backup(path)
    return len(items)


def _migrate_register(db: DatabaseStorageBackend) -> int:
    path = DATA_DIR / "register.json"
    raw = _read_json(path, {})
    if isinstance(raw, dict) and raw:
        db.save_named_config("register", raw)
        _backup(path)
        return 1
    return 0


def main() -> int:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    db = DatabaseStorageBackend(database_url)
    counts = {
        "accounts": _migrate_accounts(db),
        "auth_keys": _migrate_auth_keys(db),
        "images": _migrate_images(db),
        "image_tags": _migrate_image_tags(db),
        "logs": _migrate_logs(db),
        "cpa_pools": _migrate_named_items(db, "cpa_config.json", "cpa_pools"),
        "sub2api_servers": _migrate_named_items(db, "sub2api_config.json", "sub2api_servers"),
        "register_config": _migrate_register(db),
    }
    for table, count in counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
