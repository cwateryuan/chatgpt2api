from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    func,
    inspect,
    or_,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from services.storage.base import StorageBackend

Base = declarative_base()


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: object) -> object:
    try:
        return json.loads(str(value or "{}"))
    except Exception:
        return {}


def _as_dict(value: object) -> dict[str, Any]:
    loaded = _json_loads(value)
    return loaded if isinstance(loaded, dict) else {}


def _now() -> datetime:
    return datetime.now()


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw.replace("Z", "+0000"), fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _int_or_none(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string(value: object) -> str:
    return str(value or "").strip()


def _token_hash(access_token: object) -> str:
    return hashlib.sha256(_string(access_token).encode("utf-8")).hexdigest()


def _advisory_lock_id(value: object) -> int:
    digest = hashlib.sha256(_string(value).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class AccountModel(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(Text, nullable=False)
    access_token_hash = Column(String(64), unique=True, nullable=False, index=True)
    status = Column(String(64), nullable=True, index=True)
    source_type = Column(String(64), nullable=True, index=True)
    type = Column(String(64), nullable=True, index=True)
    quota = Column(Integer, nullable=True)
    success = Column(Integer, nullable=True)
    fail = Column(Integer, nullable=True)
    image_quota_unknown = Column(Boolean, default=False, nullable=False)
    last_used_at = Column(String(64), nullable=True)
    refresh_token = Column(Text, nullable=True)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class AuthKeyModel(Base):
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    role = Column(String(32), nullable=True, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class ImageModel(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rel = Column(String(2048), unique=True, nullable=False, index=True)
    name = Column(String(512), nullable=False, index=True)
    date = Column(String(32), nullable=True, index=True)
    size = Column(Integer, default=0, nullable=False)
    created_at = Column(String(64), nullable=True, index=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    storage = Column(String(32), default="local", nullable=False)
    local = Column(Boolean, default=True, nullable=False)
    webdav = Column(Boolean, default=False, nullable=False)
    remote_url = Column(Text, nullable=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class ImageTagModel(Base):
    __tablename__ = "image_tags"
    __table_args__ = (UniqueConstraint("image_rel", "tag", name="uq_image_tags_image_rel_tag"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    image_rel = Column(String(2048), nullable=False, index=True)
    tag = Column(String(255), nullable=False, index=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class LogModel(Base):
    __tablename__ = "logs"

    id = Column(String(64), primary_key=True)
    time = Column(String(64), nullable=True, index=True)
    type = Column(String(64), nullable=True, index=True)
    summary = Column(Text, nullable=True)
    detail = Column(Text, nullable=True)
    data = Column(Text, nullable=False)
    deleted_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=_now, nullable=False)


class CPAConfigModel(Base):
    __tablename__ = "cpa_pools"

    id = Column(String(255), primary_key=True)
    name = Column(String(255), nullable=True)
    base_url = Column(Text, nullable=True)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class Sub2APIServerModel(Base):
    __tablename__ = "sub2api_servers"

    id = Column(String(255), primary_key=True)
    name = Column(String(255), nullable=True)
    base_url = Column(Text, nullable=True)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class RegisterConfigModel(Base):
    __tablename__ = "register_config"

    key = Column(String(255), primary_key=True)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class AppConfigModel(Base):
    __tablename__ = "app_config"

    key = Column(String(255), primary_key=True)
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


_NAMED_TABLES = {
    "cpa_pools": CPAConfigModel,
    "sub2api_servers": Sub2APIServerModel,
}


class DatabaseStorageBackend(StorageBackend):
    """数据库存储后端。

    账号和鉴权密钥保留完整 JSON payload，同时把高频查询字段拆成列，支持行级
    upsert/delete。图片、标签、日志也在这里提供 DB-first 的服务层接口。
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        Base.metadata.create_all(self.engine)
        self._ensure_legacy_schema()
        self.Session = sessionmaker(bind=self.engine)

    def supports_database_features(self) -> bool:
        return True

    def load_accounts(self) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            return [item for row in session.query(AccountModel).order_by(AccountModel.id.asc()).all() if (item := self._account_from_row(row))]
        finally:
            session.close()

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        session = self.Session()
        try:
            next_hashes: set[str] = set()
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                token = _string(account.get("access_token") or account.get("accessToken"))
                if not token:
                    continue
                next_hashes.add(_token_hash(token))
                self._upsert_account_session(session, {**account, "access_token": token})
            if next_hashes:
                session.query(AccountModel).filter(~AccountModel.access_token_hash.in_(next_hashes)).delete(synchronize_session=False)
            else:
                session.query(AccountModel).delete()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def upsert_account(self, account: dict[str, Any]) -> None:
        token = _string(account.get("access_token") or account.get("accessToken"))
        if not token:
            return
        session = self.Session()
        try:
            self._upsert_account_session(session, {**account, "access_token": token})
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_account_tokens(self, tokens: list[str]) -> int:
        hashes = [_token_hash(token) for token in tokens if _string(token)]
        if not hashes:
            return 0
        session = self.Session()
        try:
            removed = session.query(AccountModel).filter(AccountModel.access_token_hash.in_(hashes)).delete(synchronize_session=False)
            session.commit()
            return int(removed or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_account(self, access_token: str) -> dict[str, Any] | None:
        token = _string(access_token)
        if not token:
            return None
        session = self.Session()
        try:
            row = session.query(AccountModel).filter(AccountModel.access_token_hash == _token_hash(token)).first()
            return self._account_from_row(row)
        finally:
            session.close()

    def list_image_candidate_accounts(self, excluded_tokens: list[str] | set[str] | None = None) -> list[dict[str, Any]]:
        excluded_hashes = {_token_hash(token) for token in (excluded_tokens or []) if _string(token)}
        session = self.Session()
        try:
            query = session.query(
                AccountModel.access_token,
                AccountModel.status,
                AccountModel.source_type,
                AccountModel.type,
                AccountModel.quota,
                AccountModel.success,
                AccountModel.fail,
                AccountModel.image_quota_unknown,
                AccountModel.last_used_at,
            )
            if excluded_hashes:
                query = query.filter(~AccountModel.access_token_hash.in_(excluded_hashes))
            rows = query.order_by(AccountModel.id.asc()).all()
            items: list[dict[str, Any]] = []
            for row in rows:
                items.append({
                    "access_token": row.access_token,
                    "status": row.status or "正常",
                    "source_type": row.source_type or "web",
                    "type": row.type or "free",
                    "quota": int(row.quota or 0),
                    "success": int(row.success or 0),
                    "fail": int(row.fail or 0),
                    "last_used_at": row.last_used_at,
                    "image_quota_unknown": bool(row.image_quota_unknown),
                })
            return items
        finally:
            session.close()

    def load_auth_keys(self) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(AuthKeyModel).order_by(AuthKeyModel.id.asc()).all():
                item = _as_dict(row.data)
                if item:
                    items.append(item)
            return items
        finally:
            session.close()

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        session = self.Session()
        try:
            next_ids: set[str] = set()
            for item in auth_keys:
                if not isinstance(item, dict):
                    continue
                key_id = _string(item.get("id"))
                if not key_id:
                    continue
                next_ids.add(key_id)
                self._upsert_auth_key_session(session, item)
            if next_ids:
                session.query(AuthKeyModel).filter(~AuthKeyModel.key_id.in_(next_ids)).delete(synchronize_session=False)
            else:
                session.query(AuthKeyModel).delete()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def upsert_auth_key(self, item: dict[str, Any]) -> None:
        session = self.Session()
        try:
            self._upsert_auth_key_session(session, item)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_auth_key_ids(self, key_ids: list[str]) -> int:
        cleaned = [_string(key_id) for key_id in key_ids if _string(key_id)]
        if not cleaned:
            return 0
        session = self.Session()
        try:
            removed = session.query(AuthKeyModel).filter(AuthKeyModel.key_id.in_(cleaned)).delete(synchronize_session=False)
            session.commit()
            return int(removed or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def save_image(self, item: dict[str, Any]) -> None:
        rel = _string(item.get("rel") or item.get("path"))
        if not rel:
            return
        payload = {**item, "rel": rel, "path": rel}
        session = self.Session()
        try:
            row = session.query(ImageModel).filter(ImageModel.rel == rel).first()
            if row is None:
                row = ImageModel(rel=rel)
                session.add(row)
            self._assign_image(row, payload)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_image(self, rel: str) -> dict[str, Any] | None:
        safe_rel = _string(rel)
        if not safe_rel:
            return None
        session = self.Session()
        try:
            row = session.query(ImageModel).filter(ImageModel.rel == safe_rel, ImageModel.deleted_at.is_(None)).first()
            return self._image_from_row(row)
        finally:
            session.close()

    def list_images(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        tag: str = "",
        q: str = "",
        page: int = 1,
        page_size: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        session = self.Session()
        try:
            query = session.query(ImageModel).filter(ImageModel.deleted_at.is_(None))
            if start_date:
                query = query.filter(ImageModel.date >= start_date)
            if end_date:
                query = query.filter(ImageModel.date <= end_date)
            if q:
                like = f"%{q}%"
                query = query.filter(or_(ImageModel.name.ilike(like), ImageModel.rel.ilike(like)))
            if tag:
                query = query.join(ImageTagModel, ImageTagModel.image_rel == ImageModel.rel).filter(ImageTagModel.tag == tag)
            total = int(query.count())
            query = query.order_by(ImageModel.created_at.desc(), ImageModel.id.desc())
            if page_size:
                safe_page = max(1, int(page or 1))
                safe_page_size = min(500, max(1, int(page_size or 60)))
                query = query.offset((safe_page - 1) * safe_page_size).limit(safe_page_size)
            items = [item for row in query.all() if (item := self._image_from_row(row))]
            return items, total
        finally:
            session.close()

    def delete_image_records(self, rels: list[str]) -> int:
        cleaned = list(dict.fromkeys(_string(rel) for rel in rels if _string(rel)))
        if not cleaned:
            return 0
        session = self.Session()
        try:
            removed_tags = session.query(ImageTagModel).filter(ImageTagModel.image_rel.in_(cleaned)).delete(synchronize_session=False)
            removed_images = session.query(ImageModel).filter(ImageModel.rel.in_(cleaned)).delete(synchronize_session=False)
            session.commit()
            return int(removed_images or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def image_storage_stats(self) -> dict[str, Any]:
        session = self.Session()
        try:
            query = session.query(ImageModel).filter(ImageModel.deleted_at.is_(None))
            return {
                "image_count": int(query.count()),
                "image_size_bytes": int(query.with_entities(func.coalesce(func.sum(ImageModel.size), 0)).scalar() or 0),
            }
        finally:
            session.close()

    def load_image_tags(self) -> dict[str, list[str]]:
        session = self.Session()
        try:
            result: dict[str, list[str]] = {}
            rows = session.query(ImageTagModel).order_by(ImageTagModel.image_rel.asc(), ImageTagModel.id.asc()).all()
            for row in rows:
                result.setdefault(str(row.image_rel), []).append(str(row.tag))
            return result
        finally:
            session.close()

    def set_image_tags(self, image_rel: str, tags: list[str]) -> list[str]:
        rel = _string(image_rel)
        cleaned = list(dict.fromkeys(_string(tag) for tag in tags if _string(tag)))
        if not rel:
            return []
        session = self.Session()
        try:
            session.query(ImageTagModel).filter(ImageTagModel.image_rel == rel).delete(synchronize_session=False)
            for tag in cleaned:
                session.add(ImageTagModel(image_rel=rel, tag=tag))
            session.commit()
            return cleaned
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def remove_image_tags(self, image_rel: str) -> None:
        self.remove_many_image_tags([image_rel])

    def remove_many_image_tags(self, image_rels: list[str] | set[str]) -> None:
        cleaned = list(dict.fromkeys(_string(rel) for rel in image_rels if _string(rel)))
        if not cleaned:
            return
        session = self.Session()
        try:
            session.query(ImageTagModel).filter(ImageTagModel.image_rel.in_(cleaned)).delete(synchronize_session=False)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_image_tag(self, tag: str) -> int:
        cleaned = _string(tag)
        if not cleaned:
            return 0
        session = self.Session()
        try:
            rels = {
                str(row.image_rel)
                for row in session.query(ImageTagModel.image_rel).filter(ImageTagModel.tag == cleaned).all()
            }
            session.query(ImageTagModel).filter(ImageTagModel.tag == cleaned).delete(synchronize_session=False)
            session.commit()
            return len(rels)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_image_tags(self) -> list[str]:
        session = self.Session()
        try:
            return [
                str(row[0])
                for row in session.query(ImageTagModel.tag).distinct().order_by(ImageTagModel.tag.asc()).all()
                if str(row[0] or "").strip()
            ]
        finally:
            session.close()

    def add_log(self, item: dict[str, Any]) -> None:
        log_id = _string(item.get("id"))
        if not log_id:
            return
        session = self.Session()
        try:
            row = session.query(LogModel).filter(LogModel.id == log_id).first()
            if row is None:
                row = LogModel(id=log_id)
                session.add(row)
            row.time = _string(item.get("time"))
            row.type = _string(item.get("type"))
            row.summary = _string(item.get("summary"))
            row.detail = _json_dumps(item.get("detail") if item.get("detail") is not None else {})
            row.data = _json_dumps(item)
            row.deleted_at = None
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_logs(
        self,
        *,
        type: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            query = session.query(LogModel).filter(LogModel.deleted_at.is_(None))
            if type:
                query = query.filter(LogModel.type == type)
            if start_date:
                query = query.filter(LogModel.time >= start_date)
            if end_date:
                query = query.filter(LogModel.time <= f"{end_date} 23:59:59")
            rows = query.order_by(LogModel.time.desc(), LogModel.created_at.desc()).limit(max(1, int(limit or 200))).all()
            return [item for row in rows if (item := self._log_from_row(row))]
        finally:
            session.close()

    def delete_logs(self, ids: list[str]) -> int:
        cleaned = list(dict.fromkeys(_string(item) for item in ids if _string(item)))
        if not cleaned:
            return 0
        session = self.Session()
        try:
            removed = session.query(LogModel).filter(LogModel.id.in_(cleaned)).delete(synchronize_session=False)
            session.commit()
            return int(removed or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def save_named_items(self, table: str, items: list[dict[str, Any]], key_field: str = "id") -> None:
        model = _NAMED_TABLES.get(str(table or ""))
        if model is None:
            raise ValueError(f"unknown named table: {table}")
        session = self.Session()
        try:
            next_ids: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = _string(item.get(key_field) or item.get("id"))
                if not item_id:
                    continue
                next_ids.add(item_id)
                row = session.query(model).filter(model.id == item_id).first()
                if row is None:
                    row = model(id=item_id)
                    session.add(row)
                if hasattr(row, "name"):
                    row.name = _string(item.get("name"))
                if hasattr(row, "base_url"):
                    row.base_url = _string(item.get("base_url"))
                row.data = _json_dumps(item)
            if next_ids:
                session.query(model).filter(~model.id.in_(next_ids)).delete(synchronize_session=False)
            else:
                session.query(model).delete()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def load_named_items(self, table: str) -> list[dict[str, Any]]:
        model = _NAMED_TABLES.get(str(table or ""))
        if model is None:
            raise ValueError(f"unknown named table: {table}")
        session = self.Session()
        try:
            return [
                item
                for row in session.query(model).order_by(model.updated_at.asc()).all()
                if (item := _as_dict(row.data))
            ]
        finally:
            session.close()

    def save_named_config(self, key: str, data: dict[str, Any]) -> None:
        config_key = _string(key) or "default"
        session = self.Session()
        try:
            row = session.query(RegisterConfigModel).filter(RegisterConfigModel.key == config_key).first()
            if row is None:
                row = RegisterConfigModel(key=config_key)
                session.add(row)
            row.data = _json_dumps(data if isinstance(data, dict) else {})
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def load_named_config(self, key: str) -> dict[str, Any] | None:
        config_key = _string(key) or "default"
        session = self.Session()
        try:
            row = session.query(RegisterConfigModel).filter(RegisterConfigModel.key == config_key).first()
            return _as_dict(row.data) if row else None
        finally:
            session.close()

    def save_app_config(self, key: str, data: dict[str, Any]) -> None:
        config_key = _string(key) or "default"
        session = self.Session()
        try:
            row = session.query(AppConfigModel).filter(AppConfigModel.key == config_key).first()
            if row is None:
                row = AppConfigModel(key=config_key)
                session.add(row)
            row.data = _json_dumps(data if isinstance(data, dict) else {})
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def load_app_config(self, key: str) -> dict[str, Any] | None:
        config_key = _string(key) or "default"
        session = self.Session()
        try:
            row = session.query(AppConfigModel).filter(AppConfigModel.key == config_key).first()
            return _as_dict(row.data) if row else None
        finally:
            session.close()

    @contextmanager
    def app_config_write_lock(self, key: str):
        session = self.Session()
        try:
            dialect = self.engine.dialect.name
            if dialect == "postgresql":
                lock_id = _advisory_lock_id(f"app_config:{_string(key) or 'default'}")
                session.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
            yield
        finally:
            try:
                if self.engine.dialect.name == "postgresql":
                    lock_id = _advisory_lock_id(f"app_config:{_string(key) or 'default'}")
                    session.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
            finally:
                session.close()

    def health_check(self) -> dict[str, Any]:
        try:
            session = self.Session()
            try:
                session.execute(text("SELECT 1"))
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": self._mask_password(self.database_url),
                    "account_count": session.query(AccountModel).count(),
                    "auth_key_count": session.query(AuthKeyModel).count(),
                    "image_count": session.query(ImageModel).filter(ImageModel.deleted_at.is_(None)).count(),
                    "log_count": session.query(LogModel).filter(LogModel.deleted_at.is_(None)).count(),
                }
            finally:
                session.close()
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "database",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        db_type = "unknown"
        if "sqlite" in self.database_url:
            db_type = "sqlite"
        elif "postgresql" in self.database_url or "postgres" in self.database_url:
            db_type = "postgresql"
        elif "mysql" in self.database_url:
            db_type = "mysql"

        return {
            "type": "database",
            "db_type": db_type,
            "description": f"数据库存储 ({db_type})",
            "database_url": self._mask_password(self.database_url),
        }

    def _account_from_row(self, row: AccountModel | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = _as_dict(row.data)
        if not item:
            item = {}
        item["access_token"] = row.access_token
        for key in ("status", "source_type", "type", "last_used_at", "refresh_token"):
            value = getattr(row, key)
            if value is not None and item.get(key) in {None, ""}:
                item[key] = value
        for key in ("quota", "success", "fail"):
            value = getattr(row, key)
            if value is not None:
                item[key] = int(value)
        return item

    def _upsert_account_session(self, session, account: dict[str, Any]) -> None:
        token = _string(account.get("access_token"))
        token_hash = _token_hash(token)
        row = session.query(AccountModel).filter(AccountModel.access_token_hash == token_hash).first()
        if row is None:
            row = AccountModel(access_token=token, access_token_hash=token_hash)
            session.add(row)
        row.access_token = token
        row.access_token_hash = token_hash
        row.status = _string(account.get("status")) or None
        row.source_type = _string(account.get("source_type")) or None
        row.type = _string(account.get("type")) or None
        row.quota = _int_or_none(account.get("quota"))
        row.success = _int_or_none(account.get("success"))
        row.fail = _int_or_none(account.get("fail"))
        row.image_quota_unknown = bool(account.get("image_quota_unknown"))
        row.last_used_at = _string(account.get("last_used_at")) or None
        row.refresh_token = _string(account.get("refresh_token")) or None
        row.data = _json_dumps(account)

    def _upsert_auth_key_session(self, session, item: dict[str, Any]) -> None:
        key_id = _string(item.get("id"))
        if not key_id:
            return
        row = session.query(AuthKeyModel).filter(AuthKeyModel.key_id == key_id).first()
        if row is None:
            row = AuthKeyModel(key_id=key_id)
            session.add(row)
        row.role = _string(item.get("role")) or None
        row.enabled = bool(item.get("enabled", True))
        row.data = _json_dumps(item)

    def _assign_image(self, row: ImageModel, item: dict[str, Any]) -> None:
        rel = _string(item.get("rel") or item.get("path"))
        row.rel = rel
        row.name = _string(item.get("name")) or rel.rsplit("/", 1)[-1]
        row.date = _string(item.get("date")) or (rel[:10].replace("/", "-") if len(rel) >= 10 else "")
        row.size = int(item.get("size") or 0)
        row.created_at = _string(item.get("created_at")) or ""
        row.width = _int_or_none(item.get("width"))
        row.height = _int_or_none(item.get("height"))
        row.storage = _string(item.get("storage")) or "local"
        row.local = bool(item.get("local", row.storage in {"local", "both"}))
        row.webdav = bool(item.get("webdav", row.storage in {"webdav", "both"}))
        row.remote_url = _string(item.get("remote_url")) or None
        row.deleted_at = _parse_datetime(item.get("deleted_at"))
        row.data = _json_dumps(item)

    def _image_from_row(self, row: ImageModel | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = _as_dict(row.data)
        item.update(
            {
                "rel": row.rel,
                "path": row.rel,
                "name": row.name,
                "date": row.date,
                "size": int(row.size or 0),
                "created_at": row.created_at,
                "storage": row.storage,
                "local": bool(row.local),
                "webdav": bool(row.webdav),
                "remote_url": row.remote_url or "",
            }
        )
        if row.width is not None:
            item["width"] = int(row.width)
        if row.height is not None:
            item["height"] = int(row.height)
        return item

    def _log_from_row(self, row: LogModel | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = _as_dict(row.data)
        if not item:
            item = {
                "id": row.id,
                "time": row.time,
                "type": row.type,
                "summary": row.summary,
                "detail": _json_loads(row.detail),
            }
        item["id"] = row.id
        return item

    @staticmethod
    def _mask_password(url: str) -> str:
        if "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _ = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url

    def _ensure_legacy_schema(self) -> None:
        inspector = inspect(self.engine)
        table_columns = {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
        }
        dialect = self.engine.dialect.name
        timestamp_type = "TIMESTAMP" if dialect in {"postgresql", "mysql"} else "DATETIME"
        additions: dict[str, dict[str, str]] = {
            "accounts": {
                "access_token_hash": "VARCHAR(64)",
                "status": "VARCHAR(64)",
                "source_type": "VARCHAR(64)",
                "type": "VARCHAR(64)",
                "quota": "INTEGER",
                "success": "INTEGER",
                "fail": "INTEGER",
                "image_quota_unknown": "BOOLEAN",
                "last_used_at": "VARCHAR(64)",
                "refresh_token": "TEXT",
                "updated_at": timestamp_type,
            },
            "auth_keys": {
                "role": "VARCHAR(32)",
                "enabled": "BOOLEAN",
                "updated_at": timestamp_type,
            },
        }
        with self.engine.begin() as conn:
            if "accounts" in table_columns:
                self._ensure_account_token_schema(conn, dialect, table_columns["accounts"])
            for table, columns in additions.items():
                existing = table_columns.get(table)
                if not existing:
                    continue
                for column, column_type in columns.items():
                    if column in existing:
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
            if "accounts" in table_columns:
                try:
                    rows = conn.execute(text("SELECT id, data FROM accounts WHERE image_quota_unknown IS NULL")).fetchall()
                    for row_id, raw_data in rows:
                        conn.execute(
                            text("UPDATE accounts SET image_quota_unknown = :value WHERE id = :id"),
                            {
                                "value": bool(_as_dict(raw_data).get("image_quota_unknown")),
                                "id": row_id,
                            },
                        )
                except Exception:
                    pass

            index_specs = [
                ("idx_accounts_status", "accounts", "status"),
                ("idx_accounts_source_type", "accounts", "source_type"),
                ("idx_accounts_type", "accounts", "type"),
                ("idx_images_date_created_at", "images", "date, created_at"),
                ("idx_logs_type_time", "logs", "type, time"),
            ]
            for index_name, table, columns in index_specs:
                if table in table_columns:
                    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({columns})"))

    def _ensure_account_token_schema(self, conn, dialect: str, columns: set[str]) -> None:
        if dialect == "postgresql":
            conn.execute(text("DROP INDEX IF EXISTS ix_accounts_access_token"))
            conn.execute(text("DROP INDEX IF EXISTS idx_accounts_access_token"))
            conn.execute(text("ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_access_token_key"))
            conn.execute(text("ALTER TABLE accounts ALTER COLUMN access_token TYPE TEXT"))
        if "access_token_hash" not in columns:
            conn.execute(text("ALTER TABLE accounts ADD COLUMN access_token_hash VARCHAR(64)"))
            columns.add("access_token_hash")
        rows = conn.execute(text("SELECT id, access_token FROM accounts WHERE access_token_hash IS NULL OR access_token_hash = ''")).fetchall()
        for row_id, token in rows:
            conn.execute(
                text("UPDATE accounts SET access_token_hash = :token_hash WHERE id = :id"),
                {"token_hash": _token_hash(token), "id": row_id},
            )
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_access_token_hash ON accounts (access_token_hash)"))
        except Exception:
            pass
