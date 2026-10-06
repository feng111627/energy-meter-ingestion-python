"""业务⑥并发：多设备同时上报 + 同一读数并发提交的幂等与一致性。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from app.errors import ReadingRejectedError

W = ("2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")


def test_concurrent_identical_submission(service):
    """用例6.1：同一条读数被 20 个线程同时提交。

    结果必须恰好 1 个 ACCEPTED、19 个 DUPLICATE、无异常抛出；
    库里只有 1 行；不产生重复电量。
    """
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)

    results: list = []
    errors: list = []

    def submit():
        try:
            results.append(
                service.ingest("dev", "2026-10-07T11:00:00Z", 130.0).status.value
            )
        except Exception as e:  # noqa: BLE001 - 并发下不允许任何异常逃逸
            errors.append(e)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(submit) for _ in range(20)]
        for f in as_completed(futures):
            f.result()

    assert errors == []
    assert results.count("ACCEPTED") == 1
    assert results.count("DUPLICATE") == 19
    assert len(service.readings("dev")) == 2
    assert service.statistics("dev", *W).energy == 30.0


def test_concurrent_distinct_timestamps_consistency(service):
    """用例6.2：同一设备并发提交 10 个不同时刻读数。

    契约要求按时间递增，因此乱序到达的请求被拒 OUT_OF_ORDER 是正确行为。
    无论如何调度都必须保证：
    - 每个时刻最多 1 行、无重复行；已存行按时间严格递增；
    - 每个已接受行的增量与它在已存序列中的前值一致；
    - 统计电量恰好等于各行增量之和（确定、稳定，不因并发产生脏数据）。
    """
    service.register_device("dev", 1000.0, 100.0)
    barrier = threading.Barrier(10)
    rejections: list[str] = []
    lock = threading.Lock()

    def submit(i: int):
        barrier.wait()  # 尽量放大竞争
        try:
            service.ingest(
                "dev", f"2026-10-07T10:{i:02d}:00Z", float(100 + 10 * (i + 1))
            )
        except ReadingRejectedError as e:
            with lock:
                rejections.append(e.code)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(submit, range(10)))

    rows = service.readings("dev")
    # 无重复、时间严格递增
    tss = [r.timestamp for r in rows]
    assert len(tss) == len(set(tss))
    assert tss == sorted(tss)
    # 每条 NORMAL 行的增量都与前一已存行一致（并发没有把基准算错）
    for prev, cur in zip(rows, rows[1:]):
        assert cur.delta_flag.value == "NORMAL"
        assert cur.delta == pytest.approx(cur.value - prev.value)
    # 未被接受的请求全部是 OUT_OF_ORDER（而不是别的异常）
    assert set(rejections) <= {"OUT_OF_ORDER"}
    # 统计与行数据严格一致（跳过首条 FIRST 的 null 增量）
    st = service.statistics("dev", *W)
    assert st.energy == pytest.approx(
        sum(r.delta for r in rows if r.delta is not None)
    )
    assert st.segment_count == max(len(rows) - 1, 0)


def test_concurrent_multi_device(service):
    """用例6.3：多台设备在同一时间并发上报，设备间真正并行、互不串数据。

    每个设备一个上报线程（设备内部按时间顺序提交，模拟同一台设备的序列），
    8 台设备同时进行；各设备统计彼此独立且正确。
    """
    n_devices = 8
    for d in range(n_devices):
        service.register_device(f"dev{d}", 1000.0, 100.0)

    def device_job(d: int):
        for i in range(5):
            service.ingest(f"dev{d}", f"2026-10-07T1{i}:00:00Z",
                           float(10 * (i + 1)))

    with ThreadPoolExecutor(max_workers=n_devices) as pool:
        for f in as_completed([pool.submit(device_job, d)
                               for d in range(n_devices)]):
            f.result()

    for d in range(n_devices):
        st = service.statistics(f"dev{d}", *W)
        # 首条 FIRST 不计，其余 4 段每段 10
        assert st.energy == pytest.approx(40.0), d
        assert st.segment_count == 4
        assert len(service.readings(f"dev{d}")) == 5


def test_concurrent_duplicate_vs_conflict(service):
    """用例6.4：同一时刻并发提交：同值全部幂等；混有不同值时不同值被拒绝。

    15 个同值请求 => 1 ACCEPTED + 14 DUPLICATE；5 个异值请求 => 全部 CONFLICT。
    """
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T09:00:00Z", 0.0)
    outcomes: list[str] = []
    lock = threading.Lock()

    def submit(value: float):
        try:
            r = service.ingest("dev", "2026-10-07T10:00:00Z", value)
            with lock:
                outcomes.append(r.status.value)
        except ReadingRejectedError as e:
            with lock:
                outcomes.append(e.code)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(submit, 50.0) for _ in range(15)]
        futs += [pool.submit(submit, 99.0) for _ in range(5)]
        for f in as_completed(futs):
            f.result()

    assert outcomes.count("ACCEPTED") == 1
    assert outcomes.count("DUPLICATE") == 14
    assert outcomes.count("CONFLICT_SAME_TS") == 5
    assert len(service.readings("dev")) == 2
    assert service.statistics("dev", *W).energy == 50.0
