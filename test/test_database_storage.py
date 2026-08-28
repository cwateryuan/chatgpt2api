from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import event

from services.storage.database_storage import AccountModel, DatabaseStorageBackend


class DatabaseStorageTests(unittest.TestCase):
    def test_accounts_support_access_tokens_longer_than_2048_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            storage = DatabaseStorageBackend(f"sqlite:///{db_path}")
            long_token = "token-" + ("x" * 4096)

            storage.upsert_account({
                "access_token": long_token,
                "status": "正常",
                "source_type": "web",
                "type": "free",
                "quota": 1,
            })

            account = storage.get_account(long_token)
            self.assertIsNotNone(account)
            self.assertEqual(account["access_token"], long_token)
            self.assertEqual(account["quota"], 1)
            self.assertEqual(storage.delete_account_tokens([long_token]), 1)
            self.assertIsNone(storage.get_account(long_token))
            storage.engine.dispose()

    def test_list_image_candidate_accounts_uses_lightweight_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            storage = DatabaseStorageBackend(f"sqlite:///{db_path}")
            storage.upsert_account({
                "access_token": "token-a",
                "status": "正常",
                "source_type": "web",
                "type": "Plus",
                "quota": 3,
                "image_quota_unknown": True,
                "fp": "x" * 10000,
            })

            statements: list[str] = []

            def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
                statements.append(str(statement))

            event.listen(storage.engine, "before_cursor_execute", before_cursor_execute)
            try:
                candidates = storage.list_image_candidate_accounts()
            finally:
                event.remove(storage.engine, "before_cursor_execute", before_cursor_execute)
                storage.engine.dispose()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["access_token"], "token-a")
        self.assertTrue(candidates[0]["image_quota_unknown"])
        select_sql = "\n".join(statements).lower()
        self.assertIn("accounts.access_token", select_sql)
        self.assertNotIn("accounts.data", select_sql)

    def test_list_image_candidate_tokens_filters_in_sql_and_projects_only_candidate_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            storage = DatabaseStorageBackend(f"sqlite:///{db_path}")
            for account in (
                {"access_token": "plus-ready", "status": "正常", "source_type": "web", "type": "Plus", "quota": 2},
                {"access_token": "plus-unknown", "status": "正常", "source_type": "web", "type": "Plus", "quota": 0, "image_quota_unknown": True},
                {"access_token": "plus-zero", "status": "正常", "source_type": "web", "type": "Plus", "quota": 0},
                {"access_token": "plus-limited", "status": "限流", "source_type": "web", "type": "Plus", "quota": 2},
                {"access_token": "codex-ready", "status": "正常", "source_type": "codex", "type": "Plus", "quota": 2},
                {"access_token": "pro-ready", "status": "正常", "source_type": "web", "type": "Pro", "quota": 2},
            ):
                storage.upsert_account(account)

            statements: list[str] = []

            def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
                statements.append(str(statement))

            event.listen(storage.engine, "before_cursor_execute", before_cursor_execute)
            try:
                candidates = storage.list_image_candidate_tokens(plan_type="plus", source_type="web")
            finally:
                event.remove(storage.engine, "before_cursor_execute", before_cursor_execute)
                storage.engine.dispose()

        self.assertEqual({item["access_token"] for item in candidates}, {"plus-ready", "plus-unknown"})
        self.assertEqual(
            set(candidates[0]),
            {"access_token", "status", "source_type", "type", "quota", "image_quota_unknown"},
        )
        select_sql = "\n".join(statements).lower()
        self.assertNotIn("accounts.data", select_sql)
        self.assertIn("accounts.image_quota_unknown", select_sql)

    def test_account_mutation_serializes_counter_updates_across_storage_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            root = DatabaseStorageBackend(f"sqlite:///{db_path}")
            root.upsert_account({"access_token": "token-a", "status": "正常", "quota": 100, "success": 0})
            stores = [DatabaseStorageBackend(f"sqlite:///{db_path}") for _ in range(4)]

            def increment(storage: DatabaseStorageBackend) -> None:
                def mutate(account):
                    assert account is not None
                    return {**account, "success": int(account.get("success") or 0) + 1}

                storage.mutate_account("token-a", mutate)

            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(increment, [stores[index % len(stores)] for index in range(24)]))
                self.assertEqual(root.get_account("token-a")["success"], 24)
            finally:
                for storage in stores:
                    storage.engine.dispose()
                root.engine.dispose()

    def test_legacy_schema_backfills_missing_status_from_account_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounts.db"
            storage = DatabaseStorageBackend(f"sqlite:///{db_path}")
            storage.upsert_account({
                "access_token": "limited-token",
                "status": "限流",
                "quota": 0,
                "image_quota_unknown": True,
            })
            session = storage.Session()
            try:
                row = session.query(AccountModel).filter(AccountModel.access_token == "limited-token").one()
                row.status = None
                session.commit()
            finally:
                session.close()
                storage.engine.dispose()

            reopened = DatabaseStorageBackend(f"sqlite:///{db_path}")
            try:
                candidate = reopened.list_image_candidate_accounts()[0]
                self.assertEqual(candidate["status"], "限流")
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
