from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import event

from services.storage.database_storage import DatabaseStorageBackend


class DatabaseStorageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
