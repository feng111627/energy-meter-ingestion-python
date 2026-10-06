"""存储层测试：持久化、唯一约束、重启恢复。"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime, timezone

import pytest

from app.models import Reading
from app.store import ReadingStore


def ts(h: int = 10, m: int = 0) -> datetime:
    return datetime(2026, 10, 7, h, m, tzinfo=timezone.utc)


@pytest.fixture()
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


class TestStore:
    """存储与持久化"""

    def test_insert_and_ordered_list(self):
        """读数按时间升序返回，乱序写入后查询结果稳定有序"""
        store = ReadingStore()
        store.connect()
        store.insert(Reading("d", ts(10, 5), 3.0))
        store.insert(Reading("d", ts(10, 0), 1.0))
        store.insert(Reading("d", ts(10, 9), 2.0))
        rows = store.list("d")
        assert [r.value for r in rows] == [1.0, 3.0, 2.0]
        assert [r.timestamp for r in rows] == sorted(r.timestamp for r in rows)
        store.close()

    def test_primary_key_idempotent(self):
        """同一设备同一时间点重复插入只能写入一行（数据库级幂等）"""
        store = ReadingStore()
        store.connect()
        assert store.insert(Reading("d", ts(), 1.0)) is True
        assert store.insert(Reading("d", ts(), 1.0)) is False
        assert store.insert(Reading("d", ts(), 9.0)) is False
        assert len(store.list("d")) == 1
        assert store.list("d")[0].value == 1.0
        store.close()

    def test_get_latest_range(self):
        """精确查询、最新一条、闭区间查询行为正确"""
        store = ReadingStore()
        store.connect()
        for m, v in ((0, 1.0), (5, 6.0), (9, 10.0)):
            store.insert(Reading("d", ts(10, m), v))
        assert store.get("d", ts(10, 5)).value == 6.0
        assert store.get("d", ts(10, 6)) is None
        assert store.latest("d").value == 10.0
        windowed = store.range("d", ts(10, 4), ts(10, 9))
        assert [r.value for r in windowed] == [6.0, 10.0]
        store.close()

    def test_devices_isolated(self):
        """多设备数据互相隔离"""
        store = ReadingStore()
        store.connect()
        store.insert(Reading("a", ts(), 1.0))
        store.insert(Reading("b", ts(), 2.0))
        assert store.devices() == ["a", "b"]
        assert store.latest("a").value == 1.0
        assert store.latest("b").value == 2.0
        store.close()

    def test_persistence_across_restart(self, tmp_db):
        """服务重启（重新打开同一数据库）后已计入数据不丢、不重"""
        store = ReadingStore(tmp_db)
        store.connect()
        store.insert(Reading("d", ts(10, 0), 100.0))
        store.insert(Reading("d", ts(10, 5), 103.0))
        store.close()

        reopened = ReadingStore(tmp_db)
        reopened.connect()
        assert [r.value for r in reopened.list("d")] == [100.0, 103.0]
        # 重启后重复上报旧读数不能产生第二行
        assert reopened.insert(Reading("d", ts(10, 0), 100.0)) is False
        assert len(reopened.list("d")) == 2
        reopened.close()

    def test_concurrent_inserts_threadsafe(self, tmp_db):
        """多线程并发写入不损坏数据，唯一约束始终生效"""
        store = ReadingStore(tmp_db)
        store.connect()

        def worker(base: int) -> None:
            for m in range(base, base + 20):
                store.insert(Reading("d", ts(10, m % 30), float(m)))

        threads = [threading.Thread(target=worker, args=(i * 5,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = store.list("d")
        timestamps = [r.timestamp for r in rows]
        assert timestamps == sorted(timestamps)
        assert len(timestamps) == len(set(timestamps))
        store.close()
