"""Offline scheduling benchmark. No upstream requests or real account files.

Run: python -m scripts.benchmark_image_cooldown --output build/image-cooldown-benchmark.json
Optional psutil reports resident memory; Redis Lua is covered by the unit suite.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "offline-benchmark")

from sqlalchemy import event, insert, update
from services.account_service import AccountService
from services.config import config
from services.image_cooldown import ImageSchedulingUnavailable
from services.runtime_state import RuntimeState
from services.storage.database_storage import AccountModel, DatabaseStorageBackend


def rss():
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="build/image-cooldown-benchmark.json")
    args = parser.parse_args()
    threading.stack_size(1024 * 1024)
    report = {"accounts": 20000, "storage": "temporary SQLite", "runtime": "single process / memory",
              "upstream": "none; scheduling only", "results": []}
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"REDIS_URL": "", "UVICORN_WORKERS": "1"}), patch.dict(config.data, {"log_levels": [], "image_account_concurrency": 3}):
        storage = DatabaseStorageBackend(f"sqlite:///{Path(tmp) / 'benchmark.db'}")
        try:
            rows = []
            for i in range(20000):
                token = f"offline-account-{i}"
                item = {"access_token": token, "status": "正常", "type": "free", "source_type": "web", "quota": 25, "image_quota_unknown": False}
                rows.append({**item, "access_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                             "data": json.dumps(item), "image_cooldown_until": 0})
            with storage.engine.begin() as conn:
                conn.execute(insert(AccountModel), rows)
            del rows
            for minutes, cooling_fraction in ((0, 0), (60, 0), (60, 0.9), (60, 1)):
                with storage.engine.begin() as conn:
                    conn.execute(update(AccountModel).values(image_cooldown_until=0))
                    conn.execute(update(AccountModel).where(AccountModel.id <= int(20000 * cooling_fraction)).values(image_cooldown_until=time.time() + 3600))
                config.data["image_account_cooldown_minutes"] = minutes
                for concurrency in (100, 200, 400):
                    runtime = RuntimeState()
                    service = AccountService(storage)
                    if minutes == 0:
                        service._list_ready_candidate_tokens()  # Warm the established legacy cache.
                    gc.collect()
                    before_rss = rss()
                    counters = {"selects": 0, "all_account_reads": 0}
                    counter_lock = threading.Lock()
                    def on_sql(_conn, _cursor, statement, _parameters, _context, _many):
                        if statement.lstrip().upper().startswith("SELECT"):
                            with counter_lock:
                                counters["selects"] += 1
                                if "accounts.data" in statement and "WHERE" not in statement:
                                    counters["all_account_reads"] += 1
                    barrier = threading.Barrier(concurrency)
                    def claim(_):
                        barrier.wait(timeout=60)
                        start = time.perf_counter()
                        try:
                            token = service.get_available_access_token()
                            return token, (time.perf_counter() - start) * 1000
                        except ImageSchedulingUnavailable:
                            return None, (time.perf_counter() - start) * 1000
                    event.listen(storage.engine, "before_cursor_execute", on_sql)
                    cpu_start, wall_start = time.process_time(), time.perf_counter()
                    with patch("services.account_service.runtime_state", runtime), ThreadPoolExecutor(max_workers=concurrency) as executor:
                        results = list(executor.map(claim, range(concurrency)))
                    event.remove(storage.engine, "before_cursor_execute", on_sql)
                    tokens = [token for token, _ in results if token]
                    durations = sorted(duration for _, duration in results)
                    after_rss = rss()
                    record = {"cooldown_minutes": minutes, "cooling_fraction": cooling_fraction,
                              "concurrency": concurrency, "claimed": len(tokens), "unique": len(set(tokens)),
                              "wall_seconds": round(time.perf_counter() - wall_start, 3),
                              "cpu_seconds": round(time.process_time() - cpu_start, 3),
                              "selection_p50_ms": round(statistics.median(durations), 2),
                              "selection_p95_ms": round(durations[int(len(durations) * .95) - 1], 2),
                              "rss_delta_mib": round((after_rss - before_rss) / 1048576, 2) if before_rss and after_rss else None,
                              **counters}
                    report["results"].append(record)
                    print(json.dumps(record), flush=True)
                    assert record["all_account_reads"] == 0
                    assert len(tokens) == (0 if cooling_fraction == 1 else concurrency)
                    assert len(set(tokens)) == len(tokens)
                    del service, runtime
            captured = []
            def capture(_conn, _cursor, statement, parameters, _context, _many):
                captured.append((statement, parameters))
            event.listen(storage.engine, "before_cursor_execute", capture)
            storage.list_image_cooldown_candidates(now=time.time(), after=(0, "8" * 64))
            event.remove(storage.engine, "before_cursor_execute", capture)
            with storage.engine.connect() as conn:
                statement, parameters = captured[0]
                report["query_plan"] = [list(row) for row in conn.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters)]
        finally:
            storage.engine.dispose()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
