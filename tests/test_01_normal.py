"""业务①正常计量：累计读数相邻增量、零增量、窗口统计、结果稳定。"""

from __future__ import annotations

from app.models import DeltaFlag, IngestStatus

DAY_START = "2026-10-07T00:00:00Z"
DAY_END = "2026-10-08T00:00:00Z"


def test_first_reading_has_no_delta(service):
    """用例1.1：设备首条读数没有前值，delta 为 null（FIRST），不计电量。"""
    service.register_device("dev", 1000.0, 100.0)
    r = service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    assert r.status == IngestStatus.ACCEPTED
    assert r.delta_flag == DeltaFlag.FIRST
    assert r.delta is None


def test_adjacent_increment_and_zero_delta(service):
    """用例1.2：相邻读数增量=后值-前值；读数不变时增量为 0 且是合法 NORMAL。"""
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    assert service.ingest("dev", "2026-10-07T11:00:00Z", 130.0).delta == 30.0
    r = service.ingest("dev", "2026-10-07T12:00:00Z", 130.0)
    assert r.delta_flag == DeltaFlag.NORMAL and r.delta == 0.0
    assert service.ingest("dev", "2026-10-07T13:00:00Z", 200.0).delta == 70.0


def test_window_statistics_and_determinism(service):
    """用例1.3：按窗口求和正确；重复查询同一输入得到稳定一致的结果。"""
    service.register_device("dev", 1000.0, 100.0)
    points = [("10:00", 100.0), ("11:00", 130.0), ("12:00", 180.0), ("13:00", 190.0)]
    for hm, v in points:
        service.ingest("dev", f"2026-10-07T{hm}:00Z", v)
    s1 = service.statistics("dev", DAY_START, DAY_END)
    s2 = service.statistics("dev", DAY_START, DAY_END)
    assert s1.energy == 90.0          # 30 + 50 + 10
    assert s1.segment_count == 3
    assert s1.energy == s2.energy and s1.segment_count == s2.segment_count
    assert s1.first_reading_ts.endswith("10:00:00.000000Z")
    assert s1.last_reading_ts.endswith("13:00:00.000000Z")


def test_subwindow_only_counts_segments_inside(service):
    """用例1.4：子窗口只统计窗口内读数行携带的增量段。"""
    service.register_device("dev", 1000.0, 100.0)
    for hm, v in [("10:00", 0.0), ("11:00", 20.0), ("12:00", 50.0), ("13:00", 90.0)]:
        service.ingest("dev", f"2026-10-07T{hm}:00Z", v)
    # 11:00 行带 20（10->11 段），12:00 行带 30
    st = service.statistics("dev", "2026-10-07T11:00:00Z", "2026-10-07T12:00:00Z")
    assert st.energy == 50.0 and st.segment_count == 2
    # 部分重叠窗口：只含 12:00 行 -> 30
    st = service.statistics("dev", "2026-10-07T11:30:00Z", "2026-10-07T12:30:00Z")
    assert st.energy == 30.0


def test_timezone_equivalence(service):
    """用例1.5：Z 与 +08:00 等等价时间写法视为同一时刻。"""
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    service.ingest("dev", "2026-10-07T11:00:00Z", 130.0)
    # 19:00+08:00 == 11:00Z，同值 -> 幂等重复
    r = service.ingest("dev", "2026-10-07T19:00:00+08:00", 130.0)
    assert r.status == IngestStatus.DUPLICATE and r.delta == 30.0
    # 18:00+08:00 == 10:00Z，同值 -> 同样命中首条
    r2 = service.ingest("dev", "2026-10-07T18:00:00+08:00", 100.0)
    assert r2.status == IngestStatus.DUPLICATE and r2.delta is None
