from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from services.account_service import account_service
from services.config import config
from services.image_service import _cleanup_parent_dirs, _safe_relative_path, _thumbnail_path
from services.image_storage_service import image_storage_service
from services.image_tags_service import remove_many_tags
from services.maintenance_activity import maintenance_activity
from services.memory import trim_memory
from services.runtime_state import runtime_state
from utils.helper import anonymize_token
from utils.log import logger


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_float_ms(name: str, default_ms: int, minimum_ms: int = 0) -> float:
    try:
        value = int(str(os.getenv(name) or "").strip() or default_ms)
    except (TypeError, ValueError):
        value = default_ms
    return max(minimum_ms, value) / 1000.0


class BulkJobService:
    IMAGE_DELETE_KIND = "bulk_image_delete"
    ACCOUNT_IMPORT_KIND = "bulk_account_import"
    _GLOBAL_LOCK_KEY = "lock:bulk:maintenance:global"

    def __init__(self) -> None:
        self._local_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    @property
    def ttl_seconds(self) -> int:
        return _env_int("APP_BULK_JOB_TTL_SECS", 86400, 300)

    @property
    def lock_ttl_seconds(self) -> int:
        return max(60, min(600, self.ttl_seconds))

    def require_redis(self) -> None:
        if not runtime_state.redis_enabled:
            raise HTTPException(
                status_code=503,
                detail={"error": "bulk job requires Redis; current runtime state backend is not Redis"},
            )

    def get_job(self, kind: str, job_id: str) -> dict[str, Any] | None:
        return runtime_state.get_progress(kind, job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise HTTPException(status_code=404, detail={"error": "job not found"})
        for kind in (self.IMAGE_DELETE_KIND, self.ACCOUNT_IMPORT_KIND):
            progress = runtime_state.get_progress(kind, job_id)
            if not progress:
                continue
            now = time.time()

            def updater(current: dict[str, Any]) -> dict[str, Any]:
                current["cancel_requested"] = True
                current["updated_at"] = now
                if current.get("done"):
                    return current
                current["status"] = "cancelling"
                return current

            updated = runtime_state.update_progress(kind, job_id, updater, ttl_seconds=self.ttl_seconds)
            return updated or progress
        raise HTTPException(status_code=404, detail={"error": "job not found"})

    def submit_image_delete(self, *, paths: list[str] | None = None, start_date: str = "", end_date: str = "", all_matching: bool = False) -> str:
        self.require_redis()
        job_id = uuid.uuid4().hex
        now = time.time()
        runtime_state.set_progress(
            self.IMAGE_DELETE_KIND,
            job_id,
            {
                "job_id": job_id,
                "kind": self.IMAGE_DELETE_KIND,
                "status": "queued",
                "total": 0,
                "processed": 0,
                "removed": 0,
                "failed": 0,
                "errors": [],
                "done": False,
                "error": None,
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            },
            ttl_seconds=self.ttl_seconds,
        )
        self._start_thread(
            job_id,
            self.IMAGE_DELETE_KIND,
            lambda: self._run_image_delete(job_id, paths or [], start_date, end_date, all_matching),
        )
        return job_id

    def submit_account_import(self, *, tokens: list[str], accounts: list[dict[str, Any]]) -> str:
        self.require_redis()
        job_id = uuid.uuid4().hex
        now = time.time()
        total = len(self._build_account_payloads(tokens, accounts))
        runtime_state.set_progress(
            self.ACCOUNT_IMPORT_KIND,
            job_id,
            {
                "job_id": job_id,
                "kind": self.ACCOUNT_IMPORT_KIND,
                "status": "queued",
                "phase": "queued",
                "total": total,
                "processed": 0,
                "imported": 0,
                "skipped": 0,
                "refreshed": 0,
                "failed": 0,
                "errors": [],
                "status_counts": {"正常": 0, "限流": 0, "异常": 0, "禁用": 0},
                "total_quota": 0,
                "done": False,
                "error": None,
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            },
            ttl_seconds=self.ttl_seconds,
        )
        self._start_thread(
            job_id,
            self.ACCOUNT_IMPORT_KIND,
            lambda: self._run_account_import(job_id, tokens, accounts),
        )
        return job_id

    def _start_thread(self, job_id: str, kind: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, daemon=True, name=f"bulk-{kind}-{job_id[:12]}")
        with self._local_lock:
            self._threads[job_id] = thread
        thread.start()

    def _run_with_global_lock(self, kind: str, job_id: str, runner: Callable[[str], None]) -> None:
        owner = runtime_state.acquire_lock(self._GLOBAL_LOCK_KEY, ttl_seconds=self.lock_ttl_seconds)
        if not owner:
            self._mark_done(kind, job_id, status="error", error="another bulk maintenance job is already running")
            with self._local_lock:
                self._threads.pop(job_id, None)
            return
        renew_stop = threading.Event()
        renew_thread = threading.Thread(
            target=self._renew_lock_worker,
            args=(renew_stop, owner),
            daemon=True,
            name=f"bulk-lock-renew-{job_id[:12]}",
        )
        renew_thread.start()
        try:
            with maintenance_activity.track(kind):
                runner(owner)
        except Exception as exc:
            self._mark_done(kind, job_id, status="error", error=str(exc))
            logger.error({"event": "bulk_job_error", "kind": kind, "job_id": job_id, "error": str(exc)})
        finally:
            renew_stop.set()
            renew_thread.join(timeout=1)
            runtime_state.release_lock(self._GLOBAL_LOCK_KEY, owner)
            with self._local_lock:
                self._threads.pop(job_id, None)
            trim_memory(f"{kind}_finished", force=True)

    def _renew_lock_worker(self, stop_event: threading.Event, owner: str) -> None:
        interval = max(5.0, self.lock_ttl_seconds / 3.0)
        while not stop_event.wait(interval):
            if not runtime_state.extend_lock(self._GLOBAL_LOCK_KEY, owner, ttl_seconds=self.lock_ttl_seconds):
                return

    def _lock_still_owned(self, owner: str) -> bool:
        return runtime_state.extend_lock(self._GLOBAL_LOCK_KEY, owner, ttl_seconds=self.lock_ttl_seconds)

    def _run_image_delete(self, job_id: str, paths: list[str], start_date: str, end_date: str, all_matching: bool) -> None:
        self._run_with_global_lock(
            self.IMAGE_DELETE_KIND,
            job_id,
            lambda owner: self._execute_image_delete(job_id, paths, start_date, end_date, all_matching, owner),
        )

    def _execute_image_delete(self, job_id: str, paths: list[str], start_date: str, end_date: str, all_matching: bool, lock_owner: str) -> None:
        batch_size = _env_int("APP_BULK_IMAGE_DELETE_BATCH_SIZE", 200, 1)
        batch_sleep = _env_float_ms("APP_BULK_IMAGE_DELETE_BATCH_SLEEP_MS", 100, 0)
        targets = self._resolve_image_targets(paths, start_date, end_date, all_matching)
        self._update(self.IMAGE_DELETE_KIND, job_id, lambda p: {**p, "status": "running", "total": len(targets), "updated_at": time.time()})
        removed = 0
        failed = 0
        for batch in self._chunks(targets, batch_size):
            if not self._lock_still_owned(lock_owner):
                self._mark_done(self.IMAGE_DELETE_KIND, job_id, status="error", error="bulk maintenance lock lost")
                return
            if self._cancel_requested(self.IMAGE_DELETE_KIND, job_id):
                self._mark_done(self.IMAGE_DELETE_KIND, job_id, status="cancelled")
                return
            try:
                result = self._delete_image_batch(batch)
                batch_removed = int(result.get("removed") or 0)
                removed += batch_removed
            except Exception as exc:
                batch_removed = 0
                failed += len(batch)
                self._append_error(self.IMAGE_DELETE_KIND, job_id, {"error": str(exc), "count": len(batch)})
            self._update(
                self.IMAGE_DELETE_KIND,
                job_id,
                lambda p, batch_count=len(batch), batch_removed=batch_removed, failed_count=failed: {
                    **p,
                    "processed": min(int(p.get("processed") or 0) + batch_count, len(targets)),
                    "removed": int(p.get("removed") or 0) + batch_removed,
                    "failed": failed_count,
                    "updated_at": time.time(),
                },
            )
            if batch_sleep > 0:
                time.sleep(batch_sleep)
        self._mark_done(self.IMAGE_DELETE_KIND, job_id, status="success")

    def _resolve_image_targets(self, paths: list[str], start_date: str, end_date: str, all_matching: bool) -> list[str]:
        raw_targets = [
            str(item["path"])
            for item in image_storage_service.list_items("", start_date=start_date, end_date=end_date)
        ] if all_matching else paths
        targets: list[str] = []
        seen: set[str] = set()
        root = config.images_dir.resolve()
        for item in raw_targets:
            try:
                rel = _safe_relative_path(item)
                path = (root / rel).resolve()
                path.relative_to(root)
            except Exception:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            targets.append(rel)
        return targets

    def _delete_image_batch(self, targets: list[str]) -> dict[str, int]:
        root = config.images_dir.resolve()
        thumbnails_root = config.image_thumbnails_dir.resolve()
        removed_items = image_storage_service.delete_many(targets)
        touched_image_paths: set[Path] = set()
        touched_thumbnail_paths: set[Path] = set()
        for item in removed_items:
            touched_image_paths.add((root / item).resolve())
            thumbnail_paths = (
                _thumbnail_path(item),
                config.image_thumbnails_dir / _safe_relative_path(item),
            )
            for thumbnail in thumbnail_paths:
                try:
                    thumbnail = thumbnail.resolve()
                    thumbnail.relative_to(thumbnails_root)
                except Exception:
                    continue
                touched_thumbnail_paths.add(thumbnail)
                if thumbnail.is_file():
                    thumbnail.unlink()
        remove_many_tags(removed_items)
        _cleanup_parent_dirs(touched_image_paths, root)
        _cleanup_parent_dirs(touched_thumbnail_paths, thumbnails_root)
        return {"removed": len(removed_items)}

    def _run_account_import(self, job_id: str, tokens: list[str], accounts: list[dict[str, Any]]) -> None:
        self._run_with_global_lock(
            self.ACCOUNT_IMPORT_KIND,
            job_id,
            lambda owner: self._execute_account_import(job_id, tokens, accounts, owner),
        )

    def _execute_account_import(self, job_id: str, tokens: list[str], accounts: list[dict[str, Any]], lock_owner: str) -> None:
        import_batch_size = _env_int("APP_BULK_ACCOUNT_IMPORT_BATCH_SIZE", 100, 1)
        refresh_workers = _env_int("APP_BULK_ACCOUNT_REFRESH_WORKERS", 2, 1)
        payloads = self._build_account_payloads(tokens, accounts)
        total = len(payloads)
        self._update(
            self.ACCOUNT_IMPORT_KIND,
            job_id,
            lambda p: {**p, "status": "running", "phase": "importing", "total": total, "updated_at": time.time()},
        )
        imported_tokens: list[str] = []
        for batch in self._chunks(payloads, import_batch_size):
            if not self._lock_still_owned(lock_owner):
                self._mark_done(self.ACCOUNT_IMPORT_KIND, job_id, status="error", error="bulk maintenance lock lost")
                return
            if self._cancel_requested(self.ACCOUNT_IMPORT_KIND, job_id):
                self._mark_done(self.ACCOUNT_IMPORT_KIND, job_id, status="cancelled")
                return
            result = self._import_account_batch(batch)
            imported_tokens.extend(result["tokens"])
            self._update(
                self.ACCOUNT_IMPORT_KIND,
                job_id,
                lambda p, added=result["added"], skipped=result["skipped"], count=len(batch): {
                    **p,
                    "processed": min(int(p.get("processed") or 0) + count, total),
                    "imported": int(p.get("imported") or 0) + added,
                    "skipped": int(p.get("skipped") or 0) + skipped,
                    "updated_at": time.time(),
                },
            )
        if self._cancel_requested(self.ACCOUNT_IMPORT_KIND, job_id):
            self._mark_done(self.ACCOUNT_IMPORT_KIND, job_id, status="cancelled")
            return
        self._update(
            self.ACCOUNT_IMPORT_KIND,
            job_id,
            lambda p: {**p, "phase": "refreshing", "refresh_total": len(imported_tokens), "refresh_processed": 0, "updated_at": time.time()},
        )
        refresh_status = self._refresh_imported_accounts(job_id, imported_tokens, refresh_workers, lock_owner)
        if refresh_status == "cancelled":
            self._mark_done(self.ACCOUNT_IMPORT_KIND, job_id, status="cancelled")
            return
        if refresh_status == "lock_lost":
            self._mark_done(self.ACCOUNT_IMPORT_KIND, job_id, status="error", error="bulk maintenance lock lost")
            return
        self._mark_done(self.ACCOUNT_IMPORT_KIND, job_id, status="success")

    def _build_account_payloads(self, tokens: list[str], accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for item in accounts:
            payload = account_service._prepare_account_payload(item)
            if payload is None:
                continue
            token = account_service._account_payload_token(payload)
            deduped[token] = {**deduped.get(token, {}), **payload, "access_token": token}
        for token in tokens:
            cleaned = str(token or "").strip()
            if not cleaned:
                continue
            deduped[cleaned] = {
                **deduped.get(cleaned, {}),
                "access_token": cleaned,
                "source_type": account_service._normalize_source_type(deduped.get(cleaned, {}).get("source_type") or "web"),
            }
        return list(deduped.values())

    def _import_account_batch(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        if account_service._database_features_enabled():
            return self._import_account_batch_database(payloads)
        added = 0
        skipped = 0
        tokens: list[str] = []
        with account_service._lock:
            account_service._reload_accounts_locked(force=True)
            changed = False
            for payload in payloads:
                token = account_service._account_payload_token(payload)
                if not token:
                    continue
                current = account_service._accounts.get(token)
                if current is None:
                    added += 1
                    account_service._cumulative_total += 1
                    account_service._save_cumulative_total()
                    current = {"created_at": account_service._now()}
                else:
                    skipped += 1
                incoming = dict(payload)
                if not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                account = account_service._normalize_account(
                    {
                        **current,
                        **incoming,
                        "access_token": token,
                        "type": str(incoming.get("type") or current.get("type") or "free"),
                    }
                )
                if account is None:
                    continue
                account_service._accounts[token] = account
                changed = True
                tokens.append(token)
            if changed:
                account_service._save_accounts()
        return {"added": added, "skipped": skipped, "tokens": tokens}

    def _import_account_batch_database(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        added = 0
        skipped = 0
        tokens: list[str] = []
        for payload in payloads:
            token = account_service._account_payload_token(payload)
            if not token:
                continue
            try:
                current = account_service.storage.get_account(token)
            except Exception:
                current = None
            normalized_current = account_service._normalize_account(current) if isinstance(current, dict) else None
            if normalized_current is None:
                added += 1
                normalized_current = {"created_at": account_service._now()}
            else:
                skipped += 1
            incoming = dict(payload)
            if not incoming.get("created_at"):
                incoming.pop("created_at", None)
            account = account_service._normalize_account(
                {
                    **normalized_current,
                    **incoming,
                    "access_token": token,
                    "type": str(incoming.get("type") or normalized_current.get("type") or "free"),
                }
            )
            if account is None:
                continue
            account_service.storage.upsert_account(account)
            with account_service._lock:
                if current is None:
                    account_service._cumulative_total += 1
                    account_service._save_cumulative_total()
                account_service._accounts[token] = account
            tokens.append(token)
        return {"added": added, "skipped": skipped, "tokens": tokens}

    def _refresh_imported_accounts(self, job_id: str, tokens: list[str], max_workers: int, lock_owner: str) -> str:
        if not tokens:
            return "success"
        workers = min(max_workers, len(tokens))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bulk-account-refresh") as executor:
            pending: dict[Any, str] = {}
            iterator = iter(tokens)

            def submit_next() -> bool:
                if not self._lock_still_owned(lock_owner):
                    return False
                if self._cancel_requested(self.ACCOUNT_IMPORT_KIND, job_id):
                    return False
                try:
                    token = next(iterator)
                except StopIteration:
                    return False
                future = executor.submit(account_service.fetch_remote_info, token, "bulk_account_import", False)
                pending[future] = token
                return True

            for _ in range(workers):
                submit_next()
            while pending:
                if not self._lock_still_owned(lock_owner):
                    executor.shutdown(wait=False, cancel_futures=True)
                    return "lock_lost"
                done, _pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    token = pending.pop(future)
                    try:
                        account = future.result()
                    except Exception as exc:
                        self._update_account_refresh_progress(job_id, token, None, str(exc))
                    else:
                        self._update_account_refresh_progress(job_id, token, account, None)
                    if self._cancel_requested(self.ACCOUNT_IMPORT_KIND, job_id):
                        executor.shutdown(wait=False, cancel_futures=True)
                        return "cancelled"
                    submit_next()
        return "success"

    def _update_account_refresh_progress(self, job_id: str, token: str, account: dict[str, Any] | None, error: str | None) -> None:
        if error:
            self._append_error(self.ACCOUNT_IMPORT_KIND, job_id, {"token": anonymize_token(token), "error": error})
        status = str((account or {}).get("status") or "正常").strip() or "正常"
        quota = max(0, int((account or {}).get("quota") or 0))

        def updater(progress: dict[str, Any]) -> dict[str, Any]:
            progress["refresh_processed"] = int(progress.get("refresh_processed") or 0) + 1
            if error:
                progress["failed"] = int(progress.get("failed") or 0) + 1
            elif account is not None:
                progress["refreshed"] = int(progress.get("refreshed") or 0) + 1
                counts = dict(progress.get("status_counts") or {})
                counts[status] = int(counts.get(status) or 0) + 1
                progress["status_counts"] = counts
                progress["total_quota"] = int(progress.get("total_quota") or 0) + quota
            progress["updated_at"] = time.time()
            return progress

        self._update(self.ACCOUNT_IMPORT_KIND, job_id, updater)

    def _cancel_requested(self, kind: str, job_id: str) -> bool:
        progress = runtime_state.get_progress(kind, job_id) or {}
        return bool(progress.get("cancel_requested"))

    def _append_error(self, kind: str, job_id: str, error: dict[str, Any]) -> None:
        def updater(progress: dict[str, Any]) -> dict[str, Any]:
            errors = list(progress.get("errors") or [])
            if len(errors) < 20:
                errors.append(error)
            progress["errors"] = errors
            progress["updated_at"] = time.time()
            return progress

        self._update(kind, job_id, updater)

    def _mark_done(self, kind: str, job_id: str, *, status: str, error: str | None = None) -> None:
        now = time.time()

        def updater(progress: dict[str, Any]) -> dict[str, Any]:
            progress["status"] = status
            progress["done"] = True
            progress["updated_at"] = now
            if error:
                progress["error"] = error
            return progress

        self._update(kind, job_id, updater)

    def _update(self, kind: str, job_id: str, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any] | None:
        return runtime_state.update_progress(kind, job_id, updater, ttl_seconds=self.ttl_seconds)

    @staticmethod
    def _chunks(items: list[Any], size: int) -> list[list[Any]]:
        return [items[index:index + size] for index in range(0, len(items), size)]


bulk_job_service = BulkJobService()
