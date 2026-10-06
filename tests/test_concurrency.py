"""并发与重启幂等性测试。

说明：业务规则要求拒绝时间倒序的读数，因此"不同时刻的读数无序并发"
天然会有一部分被拒绝（那是正确语义）。为让并发测试聚焦于幂等性而非
乱序，这里用 Barrier 按时间轮次同步，模拟真实设备"同一时刻并发上报、
时刻之间保持递增"的场景。
"""

from __future__ import annotations

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.models import IngestStatus
from app.service import MeterService
from app.store import ReadingStore

BASE = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def t(minute: int) -> datetime:
    return BASE + timedelta(minutes=minute)


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


def make_service(db_path: str) -> MeterService:
    store = ReadingStore(db_path)
    store.connect()
    return MeterService(store)


class TestConcurrency:
    """并发与重启幂等"""

    def test_same_reading_concurrent_submit_once(self):
        """同一读数被 32 个线程并发提交：恰好一次 accepted_new，其余幂等重复"""
        store = ReadingStore()
        store.connect()
        svc = MeterService(store)
        barrier = threading.Barrier(32)
        outcomes: list[str] = []
        lock = threading.Lock()

        def submit() -> None:
            barrier.wait()  # 尽量让所有线程在同一时刻发起
            r = svc.ingest("m", t(0), 100.0)
            with lock:
                outcomes.append(r.status.value)

        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(lambda _: submit(), range(32)))

        assert outcomes.count("accepted_new") == 1
        assert outcomes.count("accepted_duplicate") == 31
        assert len(store.list("m")) == 1
        store.close()

    def test_concurrent_rounds_new_and_duplicate(self):
        """按时间轮次并发：每轮两个线程提交同一新读数，恰好一个新增一个幂等"""
        store = ReadingStore()
        store.connect()
        svc = MeterService(store)
        rounds = 30
        barrier = threading.Barrier(2)
        results: list = []
        lock = threading.Lock()

        def worker() -> None:
            for minute in range(rounds):
                barrier.wait()  # 两个线程在同一时刻提交同一读数
                r = svc.ingest("m", t(minute), 100.0 + minute)
                with lock:
                    results.append(r.status)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: worker(), range(2)))

        new_count = sum(s is IngestStatus.ACCEPTED_NEW for s in results)
        dup_count = sum(s is IngestStatus.ACCEPTED_DUPLICATE for s in results)
        assert new_count == rounds
        assert dup_count == rounds
        assert len(store.list("m")) == rounds
        stats = svc.usage("m", t(0), t(rounds - 1))
        assert stats.total_usage == pytest.approx(float(rounds - 1))
        store.close()

    def test_concurrent_multi_devices(self):
        """多台设备同一时刻并发上报互不干扰，各自统计正确"""
        store = ReadingStore()
        store.connect()
        svc = MeterService(store)
        device_count, rounds = 8, 20
        barrier = threading.Barrier(device_count)

        def device_worker(device: int) -> None:
            for minute in range(rounds):
                barrier.wait()  # 所有设备在同一时刻并发上报各自读数
                svc.ingest(f"dev-{device}", t(minute), float(device * 1000 + minute))

        with ThreadPoolExecutor(max_workers=device_count) as pool:
            list(pool.map(device_worker, range(device_count)))

        assert sorted(store.devices()) == [f"dev-{d}" for d in range(device_count)]
        for d in range(device_count):
            stats = svc.usage(f"dev-{d}", t(0), t(rounds - 1))
            assert stats.reading_count == rounds
            assert stats.total_usage == pytest.approx(float(rounds - 1))
        store.close()

    def test_concurrent_duplicates_and_new_mix(self):
        """并发混合：每个新读数到达的同一刻，旧读数被 4 个线程重复重放"""
        store = ReadingStore()
        store.connect()
        svc = MeterService(store)
        svc.ingest("m", t(0), 0.0)
        barrier = threading.Barrier(5)
        results: list = []
        lock = threading.Lock()

        def round_worker(role: int, minute: int) -> None:
            barrier.wait()
            if role == 0:
                r = svc.ingest("m", t(minute), float(minute))       # 新读数
            else:
                r = svc.ingest("m", t(minute - 1), float(minute - 1))  # 旧读数重放
            with lock:
                results.append(r.status)

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = []
            for minute in range(1, 11):
                for role in range(5):
                    futures.append(pool.submit(round_worker, role, minute))
            for f in futures:
                f.result()

        accepted_new = [s for s in results if s is IngestStatus.ACCEPTED_NEW]
        assert len(accepted_new) == 10
        assert len(store.list("m")) == 11  # 含基线
        stats = svc.usage("m", t(0), t(10))
        assert stats.total_usage == pytest.approx(10.0)
        store.close()

    def test_restart_keeps_data_and_continues_stats(self, tmp_db):
        """服务重启后已计入数据不丢、不重，且可在其基础上继续累计统计"""
        svc = make_service(tmp_db)
        svc.ingest("m", t(0), 100.0)
        svc.ingest("m", t(5), 105.0)
        svc.store.close()

        # —— 模拟服务重启 ——
        svc2 = make_service(tmp_db)
        assert len(svc2.store.list("m")) == 2  # 数据未丢
        assert svc2.usage("m", t(0), t(5)).total_usage == pytest.approx(5.0)

        # 旧读数重放：不重复计入
        replay = svc2.ingest("m", t(5), 105.0)
        assert replay.status is IngestStatus.ACCEPTED_DUPLICATE
        assert len(svc2.store.list("m")) == 2

        # 重启后继续上报，统计与重启前连续衔接
        cont = svc2.ingest("m", t(10), 109.0)
        assert cont.status is IngestStatus.ACCEPTED_NEW
        assert cont.delta == pytest.approx(4.0)
        assert svc2.usage("m", t(0), t(10)).total_usage == pytest.approx(9.0)
        svc2.store.close()
