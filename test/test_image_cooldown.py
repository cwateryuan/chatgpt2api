from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Barrier
from threading import Thread
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.account_service import AccountService
from services.config import ConfigStore, config
from services.image_cooldown import ImageSchedulingUnavailable, MemoryImageCandidateIndex
from services.runtime_state import RuntimeState
from services.storage.database_storage import AccountModel, DatabaseStorageBackend
from services.storage.json_storage import JSONStorageBackend


def account(token, quota=25, **extra):
    return {"access_token": token, "status": "正常", "source_type": "web", "type": "free",
            "quota": quota, "image_quota_unknown": False, **extra}


def _process_claims(args):
    database_url, redis_url, count = args
    with patch.dict(os.environ, {"REDIS_URL": redis_url, "UVICORN_WORKERS": "2"}), patch.dict(config.data, {"image_account_cooldown_minutes": 60, "log_levels": []}):
        runtime = RuntimeState()
        storage = DatabaseStorageBackend(database_url)
        try:
            service = AccountService(storage)
            with patch("services.account_service.runtime_state", runtime):
                return [service.get_available_access_token() for _ in range(count)]
        finally:
            storage.engine.dispose()
            runtime._redis.close()


class ImageCooldownTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"REDIS_URL": "", "UVICORN_WORKERS": "1"}))
        self.runtime = RuntimeState()
        self.stack.enter_context(patch("services.account_service.runtime_state", self.runtime))
        self.stack.enter_context(patch.dict(config.data, {"image_account_cooldown_minutes": 60,
                                                       "auto_remove_rate_limited_accounts": False,
                                                       "log_levels": []}))
        self.stack.enter_context(patch("services.account_service.log_service.add"))

    def build(self, accounts, database=True, name="accounts"):
        storage = (DatabaseStorageBackend(f"sqlite:///{self.root / (name + '.db')}") if database
                   else JSONStorageBackend(self.root / (name + '.json')))
        if database:
            self.addCleanup(storage.engine.dispose)
        storage.save_accounts(accounts)
        return AccountService(storage)

    def test_success_keeps_normal_status_and_quota_but_blocks_until_boundary(self):
        for database in (True, False):
            with self.subTest(database=database):
                service = self.build([account("a", 25), account("b", 5)], database, str(database))
                with patch("time.time", return_value=10000):
                    token = service.get_available_access_token()
                    before = service.get_account(token)["quota"]
                    result = service.mark_image_result(token, True)
                    self.assertEqual(result["quota"], before - 1)
                    self.assertEqual(result["status"], "正常")
                    self.assertEqual(result["image_cooldown_until"], 13600)
                    self.assertEqual(service.get_image_pool_metrics()["current_available"], 2)
                    self.assertTrue(all("image_cooldown_until" not in a for a in service.list_accounts()))
                    with self.assertRaises(ImageSchedulingUnavailable):
                        service.get_available_access_token(excluded_tokens={"b" if token == "a" else "a"})
                with patch("time.time", return_value=13600):
                    selected = service.get_available_access_token(excluded_tokens={"b" if token == "a" else "a"})
                    self.assertEqual(selected, token)
                    service.release_image_slot(selected)

    def test_failure_and_last_quota(self):
        service = self.build([account("a", 1)])
        token = service.get_available_access_token()
        failed = service.mark_image_result(token, False)
        self.assertEqual(failed["quota"], 1)
        self.assertEqual(failed.get("image_cooldown_until", 0), 0)
        token = service.get_available_access_token()
        service.mark_image_result(token, True)
        metrics = service.get_image_cooldown_metrics()
        self.assertEqual(metrics["cooling_accounts"], 0)
        self.assertEqual(service.get_account(token)["status"], "限流")

    def test_source_plan_and_excluded_filters_and_deleted_accounts(self):
        service = self.build([account("web-plus", type="Plus"), account("codex-pro", type="Pro", source_type="codex"),
                              account("web-free"), account("deleted")])
        service.delete_accounts(["deleted"])
        token = service.get_available_access_token(source_type="codex", plan_types=("plus", "team", "pro"))
        self.assertEqual(token, "codex-pro")
        service.mark_image_result(token, True)
        with self.assertRaises(ImageSchedulingUnavailable):
            service.get_available_access_token(plan_type="plus", excluded_tokens={"web-plus"})
        token = service.get_available_access_token(plan_type="plus")
        self.assertEqual(token, "web-plus")
        service.mark_image_result(token, True)
        service.update_account(token, {"status": "禁用"}, quiet=True)
        service._cooldown_metrics_cache = None
        self.assertEqual(service.get_image_cooldown_metrics()["cooling_accounts"], 1)

    def test_pool_exhaustion_is_http_503_with_retry_after_for_single_and_multiple_images(self):
        from services.protocol import conversation
        from services.image_failure import ImageGenerationError
        from services.log_service import _image_error_response
        service = self.build([account("a", image_cooldown_until=time.time() + 1800)])
        for count in (1, 3):
            request = conversation.ConversationRequest(model="gpt-image-2", prompt="test", n=count)
            with patch.object(conversation, "account_service", service), patch.object(conversation.proxy_settings, "next_upstream_proxy", return_value=""), patch.object(conversation, "OpenAIBackendAPI") as backend:
                with self.assertRaises(ImageGenerationError) as error:
                    list(conversation.stream_image_outputs_with_pool(request))
                backend.assert_not_called()
            response = _image_error_response(error.exception)
            self.assertEqual(response.status_code, 503)
            self.assertGreater(int(response.headers["Retry-After"]), 1700)
            self.assertEqual(json.loads(response.body)["error"]["code"], "image_pool_unavailable")

    def test_dashboard_adds_counts_without_changing_health_json(self):
        from api import system
        service = self.build([account("a", image_cooldown_until=time.time() + 1800)])
        app = FastAPI()
        app.include_router(system.create_router("test"))
        with TestClient(app) as client, patch("services.account_service.account_service", service), patch.object(config, "get_storage_backend", return_value=service.storage), patch.object(system.proxy_settings, "get_runtime_status", return_value={}):
            payload = client.get("/health?format=json").json()
            self.assertNotIn("cooling_accounts", payload["accounts"])
            html = client.get("/health").text
            self.assertIn("未来 1 小时内冷却结束", html)
            self.assertIn("document.hidden", html)

    def test_success_is_persisted_before_release_and_survives_restart(self):
        service = self.build([account("a")])
        token = service.get_available_access_token()
        original = service.storage.mutate_account

        def checked(*args, **kwargs):
            self.assertEqual(self.runtime.get_image_inflight(token), 1)
            return original(*args, **kwargs)

        with patch.object(service.storage, "mutate_account", side_effect=checked):
            service.mark_image_result(token, True)
        self.assertEqual(self.runtime.get_image_inflight(token), 0)
        restarted = AccountService(service.storage)
        with self.assertRaises(ImageSchedulingUnavailable) as error:
            restarted.get_available_access_token()
        self.assertGreater(error.exception.retry_after, 3500)

    def test_success_storage_error_retains_slot_without_retrying_generation(self):
        service = self.build([account("a")])
        token = service.get_available_access_token()
        with patch.object(service.storage, "mutate_account", side_effect=OSError("disk unavailable")):
            self.assertIsNone(service.mark_image_result(token, True))
        self.assertEqual(self.runtime.get_image_inflight(token), 1)
        with self.assertRaises(ImageSchedulingUnavailable):
            service.get_available_access_token()

    def test_stale_batch_is_rechecked_against_database(self):
        service = self.build([account("a"), account("b")])
        worker = AccountService(service.storage)
        worker._next_cooldown_candidate()  # Pre-fill this worker's independent batch.
        for token in ("a", "b"):
            service.mark_image_result(token, True)
        with self.assertRaises(ImageSchedulingUnavailable):
            worker.get_available_access_token()

    def test_storage_error_never_falls_back_to_cached_eligible_account(self):
        service = self.build([account("a")])
        with patch.object(service.storage, "get_account", side_effect=OSError("unavailable")):
            with self.assertRaises(ImageSchedulingUnavailable):
                service.get_available_access_token()
        self.assertEqual(self.runtime.get_image_inflight("a"), 0)

    def test_configuration_change_does_not_rewrite_existing_deadlines(self):
        service = self.build([account("a")])
        result = service.mark_image_result("a", True)
        until = result["image_cooldown_until"]
        config.data["image_account_cooldown_minutes"] = 5
        with self.assertRaises(ImageSchedulingUnavailable):
            service.get_available_access_token()
        config.data["image_account_cooldown_minutes"] = 0
        token = service.get_available_access_token()
        service.mark_image_result(token, True)
        self.assertEqual(service.get_account(token)["image_cooldown_until"], until)
        self.assertEqual(service.get_image_cooldown_metrics()["cooling_accounts"], 0)
        config.data["image_account_cooldown_minutes"] = 60
        with self.assertRaises(ImageSchedulingUnavailable):
            service.get_available_access_token()

    def test_metrics_boundaries_and_account_eligibility(self):
        now = time.time()
        items = [account("due", image_cooldown_until=now),
                 account("soon", image_cooldown_until=now + 1),
                 account("hour", image_cooldown_until=now + 3600),
                 account("later", image_cooldown_until=now + 3601),
                 account("empty", 0, image_cooldown_until=now + 50),
                 account("unknown", 0, image_quota_unknown=True, image_cooldown_until=now + 100),
                 account("disabled", status="禁用", image_cooldown_until=now + 50)]
        for database in (True, False):
            service = self.build(items, database, str(database))
            with patch("time.time", return_value=now):
                metrics = service.get_image_cooldown_metrics()
            self.assertEqual(metrics, {"cooling_accounts": 4, "thawing_within_hour": 3,
                                       "next_thaw_at": now + 1, "as_of": now})

    def test_token_rotation_keeps_cooldown_and_removes_old_memory_reference(self):
        for database in (True, False):
            service = self.build([account("old")], database, str(database))
            until = service.mark_image_result("old", True)["image_cooldown_until"]
            new = service._apply_refreshed_tokens("old", {"access_token": "new"}, "test")
            self.assertEqual(new, "new")
            self.assertEqual(service.get_account(new)["image_cooldown_until"], until)
            self.assertEqual(service.get_image_cooldown_metrics()["cooling_accounts"], 1)
            with self.assertRaises(ImageSchedulingUnavailable):
                service.get_available_access_token()

    def test_four_hundred_concurrent_claims_cross_batch_boundary_without_duplicates(self):
        service = self.build([account(f"token-{i}") for i in range(500)])
        barrier = Barrier(100)
        def claim(_):
            barrier.wait(timeout=30)
            return service.get_available_access_token()
        with ThreadPoolExecutor(max_workers=100) as executor:
            tokens = list(executor.map(claim, range(400)))
        self.assertEqual(len(set(tokens)), 400)
        for token in tokens:
            service.release_image_slot(token)

    def test_memory_index_does_not_accumulate_versions(self):
        index = MemoryImageCandidateIndex()
        for turn in range(10000):
            index.update(account("a", image_cooldown_until=turn), True)
        self.assertEqual(len(index._entries), 1)
        self.assertEqual(sum(map(len, index._groups.values())), 1)
        index.remove("a")
        self.assertEqual(index._groups, {})

    def test_memory_candidates_keep_plan_alias_support(self):
        service = self.build([account("business", type="business"), account("lite", type="pro_lite")], database=False)
        self.assertEqual(service.get_available_access_token(plan_type="team"), "business")
        self.assertEqual(service.get_available_access_token(plan_type="prolite"), "lite")

    def test_legacy_schema_migrates_to_zero_cooldown(self):
        service = self.build([account("a")])
        with service.storage.engine.begin() as conn:
            conn.exec_driver_sql("DROP INDEX idx_accounts_image_cooldown")
            conn.exec_driver_sql("ALTER TABLE accounts DROP COLUMN image_cooldown_until")
        reopened = DatabaseStorageBackend(service.storage.database_url)
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(reopened.get_account("a")["image_cooldown_until"], 0)
        self.assertEqual(len(reopened.list_image_cooldown_candidates(now=time.time(), after=(-1, ""))), 1)

    def test_actual_candidate_query_uses_ordered_index_without_sorting_pool(self):
        from sqlalchemy import event
        service = self.build([account("a")])
        captured = []
        def capture(_conn, _cursor, sql, args, _context, _many):
            captured.append((sql, args))
        event.listen(service.storage.engine, "before_cursor_execute", capture)
        try:
            service.storage.list_image_cooldown_candidates(now=time.time(), after=(0, "8" * 64))
        finally:
            event.remove(service.storage.engine, "before_cursor_execute", capture)
        sql, args = captured[0]
        with service.storage.engine.connect() as conn:
            plan = str(conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args).all())
        self.assertIn("idx_accounts_image_cooldown", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_stale_metadata_write_cannot_shorten_persisted_cooldown(self):
        service = self.build([account("a")])
        stale = service.storage.get_account("a")
        result = service.mark_image_result("a", True)
        service.storage.upsert_account({**stale, "proxy": "http://proxy.example"})
        self.assertEqual(service.storage.get_account("a")["image_cooldown_until"], result["image_cooldown_until"])

    def test_metrics_api_authentication_and_legacy_pool_contract(self):
        from api import accounts
        service = self.build([account("a")])
        before = service.get_image_pool_metrics()
        service.mark_image_result("a", True)
        app = FastAPI()
        app.include_router(accounts.create_router())
        with TestClient(app) as client, patch.object(accounts, "account_service", service), patch.object(accounts, "require_admin") as auth:
            response = client.get("/api/accounts/image-cooldown-metrics", headers={"Authorization": "Bearer test"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["cooling_accounts"], 1)
            auth.assert_called_once_with("Bearer test")
            legacy = client.get("/api/accounts/image-pool-metrics").json()
            self.assertEqual(set(legacy), set(before))
            self.assertEqual(legacy["current_available"], before["current_available"])
            items = client.get("/api/accounts").json()["items"]
            self.assertEqual(items[0]["status"], "正常")
            self.assertNotIn("image_cooldown_until", items[0])

    def test_config_validation_default_and_stale_worker_update(self):
        path = self.root / "config.json"
        path.write_text("{}", encoding="utf-8")
        a, b = ConfigStore(path), ConfigStore(path)
        self.assertEqual(a.image_account_cooldown_minutes, 60)
        a.update({"image_account_cooldown_minutes": 30})
        b.update({"image_account_cooldown_minutes": 60, "base_url": "https://example.com"})
        self.assertEqual(json.loads(path.read_text())["image_account_cooldown_minutes"], 30)
        for value in (-1, 1.5, True, None, "abc"):
            with self.assertRaises(ValueError):
                a.update({"image_account_cooldown_minutes": value})
        a.update({"image_account_cooldown_minutes": 0})
        self.assertEqual(a.image_account_cooldown_minutes, 0)

    def test_redis_failure_and_missing_shared_state_fail_closed(self):
        with patch.dict(os.environ, {"UVICORN_WORKERS": "6"}):
            with self.assertRaises(ImageSchedulingUnavailable):
                self.runtime.acquire_image_slot(["a"], 1, 3600, strict=True)
        with patch.object(self.runtime, "_redis") as redis:
            redis.eval.side_effect = ConnectionError("unavailable")
            with self.assertRaises(ImageSchedulingUnavailable):
                self.runtime.acquire_image_slot(["a"], 1, 3600, strict=True)
        self.assertEqual(self.runtime._memory_inflight, {})
        service = self.build([account("a")], database=False)
        with patch.dict(os.environ, {"UVICORN_WORKERS": "6"}):
            with self.assertRaisesRegex(ImageSchedulingUnavailable, "database storage"):
                service.get_available_access_token()

    def test_redis_lua_coordinates_workers_and_caches_metrics(self):
        try:
            import fakeredis
        except ImportError:
            self.skipTest("Install fakeredis[lua] to exercise Redis scripts")
        server = fakeredis.FakeServer()
        other = RuntimeState()
        self.runtime._redis = fakeredis.FakeRedis(server=server, decode_responses=True)
        other._redis = fakeredis.FakeRedis(server=server, decode_responses=True)
        self.assertEqual(self.runtime.acquire_image_slot(["a"], 1, 4000, strict=True), "a")
        self.assertEqual(other.acquire_image_slot(["a"], 1, 4000, strict=True), "")
        self.assertEqual(self.runtime._redis.keys("account:image:lease:*"), [])
        self.runtime.transfer_image_slot("a", "b", 200)
        self.assertGreater(other._redis.ttl("account:image:inflight:b"), 3900)
        other.release_image_slot("b")
        self.assertEqual(self.runtime.get_image_inflight("b"), 0)
        for _ in range(100):
            self.runtime.acquire_image_slot(["repeat"], 1, 4000, strict=True)
            other.release_image_slot("repeat")
        self.assertEqual(self.runtime._redis.keys("account:image:inflight:*"), [])
        self.assertEqual(self.runtime._redis.keys("account:image:lease:*"), [])
        service = self.build([account("a", image_cooldown_until=time.time() + 1800)])
        worker = AccountService(service.storage)
        with patch.object(service.storage, "get_image_cooldown_metrics", wraps=service.storage.get_image_cooldown_metrics) as query:
            self.assertEqual(service.get_image_cooldown_metrics()["cooling_accounts"], 1)
            self.assertEqual(worker.get_image_cooldown_metrics()["cooling_accounts"], 1)
            self.assertEqual(query.call_count, 1)

    def test_two_processes_coordinate_over_shared_redis_protocol(self):
        try:
            from fakeredis import TcpFakeServer
        except ImportError:
            self.skipTest("Install fakeredis[lua] to exercise the Redis protocol")
        import multiprocessing
        service = self.build([account(f"process-{i}") for i in range(150)])
        server = TcpFakeServer(("127.0.0.1", 0))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            redis_url = f"redis://127.0.0.1:{server.server_address[1]}/0"
            args = (service.storage.database_url, redis_url, 50)
            with multiprocessing.get_context("spawn").Pool(2) as workers:
                results = workers.map(_process_claims, [args, args])
            tokens = results[0] + results[1]
            self.assertEqual(len(set(tokens)), 100)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
