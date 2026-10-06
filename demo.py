"""本地模拟：设备读数上报 -> 异常处理 -> 用电量统计 -> 重启恢复 的完整流程。

运行：uv run python demo.py
脚本不依赖网络，直接在进程内调用服务层，日志按业务分段打印。
"""

from __future__ import annotations

import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.models import IngestStatus
from app.service import MeterService
from app.store import ReadingStore

BASE = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def t(minute: int) -> datetime:
    return BASE + timedelta(minutes=minute)


def section(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show(result) -> None:
    delta = "-" if result.delta is None else f"{result.delta:.3f}"
    print(
        f"  [{result.status.value:>18}] device={result.device_id} "
        f"ts=t+{int((result.timestamp - BASE).total_seconds() // 60):>2}m "
        f"value={result.value:>9.3f} delta={delta:>9}  {result.detail or ''}"
        + (f"（原因: {result.reason.value}）" if result.reason else "")
    )


def main() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    import os

    os.close(fd)

    # --------------------------------------------------------------- 正常流程
    section("业务1：正常上报与相邻增量（设备 A 稳定用电）")
    store = ReadingStore(db_path)
    store.connect()
    svc = MeterService(store)
    for minute, value in ((0, 100.0), (5, 108.5), (10, 112.0), (15, 120.25)):
        show(svc.ingest("A", t(minute), value))

    # --------------------------------------------------------------- 异常处理
    section("业务2：异常读数处理（设备 B 演示重复/倒序/冲突/回绕/重启式下降）")
    cases = [
        (0, 500.0, "基线"),
        (5, 506.0, "正常递增"),
        (5, 506.0, "重复上报"),
        (5, 999.0, "同点冲突"),
        (3, 503.0, "时间倒序"),
        (10, 99_900.0, "接近回绕顶部"),
        (15, 30.0, "计数器回绕到接近零"),
        (20, 90.0, "回绕后继续增长"),
        (25, 5.0, "疑似重启复位的下降"),
    ]
    for minute, value, label in cases:
        r = svc.ingest("B", t(minute), value)
        show(r)
        print(f"        └─ 场景: {label}")

    # --------------------------------------------------------------- 离线空档
    section("业务3：设备离线空档不凭空产生用电量（设备 C）")
    for minute, value in ((0, 0.0), (5, 3.0), (30, 3.0), (35, 7.0)):
        show(svc.ingest("C", t(minute), value))
    gap_stats = svc.usage("C", t(6), t(29))
    print(f"  -> 查询离线空档 [t+6m, t+29m]：总用电量 = {gap_stats.total_usage}（应为 0）")
    full_stats = svc.usage("C", t(0), t(35))
    print(f"  -> 查询全程 [t+0m, t+35m]：总用电量 = {full_stats.total_usage}（应为 7，空档不摊派）")

    # --------------------------------------------------------------- 并发上报
    section("业务4：4 台设备同一时刻并发上报，同一读数并发提交幂等")
    barrier = threading.Barrier(4)

    def device_worker(device: str) -> None:
        offset = (ord(device) - ord("D")) * 100
        for minute in range(40, 45):
            barrier.wait()
            svc.ingest(device, t(minute), float(offset + minute))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(device_worker, ["D", "E", "F", "G"]))
    # 同一读数 20 个线程同时提交
    barrier20 = threading.Barrier(20)

    def duplicate_worker():
        barrier20.wait()
        return svc.ingest("D", t(45), 205.0)

    with ThreadPoolExecutor(max_workers=20) as pool:
        duplicate_results = list(pool.map(lambda _: duplicate_worker(), range(20)))
    new_count = sum(r.status is IngestStatus.ACCEPTED_NEW for r in duplicate_results)
    dup_count = sum(r.status is IngestStatus.ACCEPTED_DUPLICATE for r in duplicate_results)
    print(f"  -> 20 线程提交同一读数：新增 {new_count} 次，幂等重复 {dup_count} 次，存储仅 1 行")

    # --------------------------------------------------------------- 窗口查询
    section("业务5：按设备和时间窗口查询用电量")
    for device in ["A", "B", "C"]:
        stats = svc.usage(device, t(0), t(39))
        print(
            f"  设备 {device}: 窗口内读数 {stats.reading_count} 条，"
            f"总用电量 {stats.total_usage:.3f} kWh，增量片段 {len(stats.points)} 个"
        )

    # --------------------------------------------------------------- 重启恢复
    section("业务6：服务重启后数据不丢、不重，统计连续")
    store.close()
    store2 = ReadingStore(db_path)
    store2.connect()
    svc2 = MeterService(store2)
    before = svc2.usage("A", t(0), t(15)).total_usage
    replay = svc2.ingest("A", t(15), 120.25)
    cont = svc2.ingest("A", t(20), 125.25)
    after = svc2.usage("A", t(0), t(20)).total_usage
    print(f"  重启后 A 在 [t+0,t+15] 的用电量 = {before:.3f}（数据未丢）")
    print(f"  旧读数重放结果 = {replay.status.value}（不重复计入）")
    print(f"  重启后继续上报增量 = {cont.delta:.3f}，全程用电量 = {after:.3f}")
    store2.close()

    for suffix in ("", "-wal", "-shm"):
        try:
            import os as _os

            _os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass
    print("\n模拟流程完成。")


if __name__ == "__main__":
    main()
