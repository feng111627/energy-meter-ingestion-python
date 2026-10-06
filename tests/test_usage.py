"""统计查询测试：空档、重复上报、稳定性。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.service import MeterService
from app.store import ReadingStore

BASE = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def t(minute: int) -> datetime:
    return BASE + timedelta(minutes=minute)


@pytest.fixture()
def svc():
    store = ReadingStore()
    store.connect()
    yield MeterService(store)
    store.close()


def feed(svc, seq):
    for minute, value in seq:
        svc.ingest("m", t(minute), value)


class TestUsage:
    """时间窗口用电量统计"""

    def test_total_over_window(self, svc):
        """窗口覆盖全部读数时，总用量等于所有相邻增量之和"""
        feed(svc, [(0, 100.0), (5, 108.5), (10, 112.0)])
        stats = svc.usage("m", t(0), t(10))
        assert stats.total_usage == pytest.approx(12.0)
        assert [p.delta for p in stats.points] == [8.5, 3.5]
        assert stats.reading_count == 3

    def test_offline_gap_generates_no_usage(self, svc):
        """设备离线造成的时间空档不凭空产生用电量，也不摊派"""
        feed(svc, [(0, 100.0), (5, 105.0), (60, 105.0), (65, 109.0)])
        stats = svc.usage("m", t(0), t(65))
        # 空档两侧读数相同：空档区间增量为 0，总量只有 5 + 0 + 4
        assert stats.total_usage == pytest.approx(9.0)
        # 没有任何读数落在空档里：查询空档窗口本身得到 0
        gap = svc.usage("m", t(10), t(50))
        assert gap.total_usage == 0.0
        assert gap.points == ()

    def test_gap_with_consumption_not_attributed_to_window(self, svc):
        """跨越窗口起点的增量不计入窗口（避免把窗外用电算进窗内）"""
        feed(svc, [(0, 100.0), (10, 130.0), (20, 140.0)])
        # 从第 10 分钟开始查：100->130 这段跨越窗口起点，不计入
        stats = svc.usage("m", t(10), t(20))
        assert stats.total_usage == pytest.approx(10.0)
        assert [p.delta for p in stats.points] == [10.0]

    def test_duplicate_reports_not_double_counted(self, svc):
        """重复上报不产生第二行，统计结果与只报一次完全一致"""
        feed(svc, [(0, 100.0), (5, 105.0), (10, 108.0)])
        before = svc.usage("m", t(0), t(10))
        # 把已有读数重复上报若干次
        for _ in range(3):
            svc.ingest("m", t(5), 105.0)
            svc.ingest("m", t(10), 108.0)
        after = svc.usage("m", t(0), t(10))
        assert after.total_usage == before.total_usage == 8.0
        assert after.reading_count == before.reading_count == 3
        assert len(after.points) == 2

    def test_rollover_usage_in_window(self, svc):
        """回绕产生的跨绕增量被正确计入窗口统计"""
        feed(svc, [(0, 99800.0), (5, 200.0), (10, 260.0)])
        stats = svc.usage("m", t(0), t(10))
        assert stats.total_usage == pytest.approx(400.0 + 60.0)
        assert stats.points[0].delta == pytest.approx(400.0)

    def test_empty_window_and_unknown_device(self, svc):
        """空窗口与未知设备返回零用量，不报错"""
        feed(svc, [(0, 100.0), (5, 105.0)])
        assert svc.usage("m", t(30), t(40)).total_usage == 0.0
        unknown = svc.usage("ghost", t(0), t(60))
        assert unknown.total_usage == 0.0
        assert unknown.reading_count == 0

    def test_repeated_queries_are_stable(self, svc):
        """同样输入反复查询得到完全一致（确定、稳定）的结果"""
        feed(svc, [(0, 100.0), (5, 105.0), (10, 105.0), (15, 107.0)])
        first = svc.usage("m", t(0), t(15))
        serialized = [
            (p.window_start.isoformat(), p.window_end.isoformat(), p.delta)
            for p in first.points
        ]
        for _ in range(5):
            again = svc.usage("m", t(0), t(15))
            again_serialized = [
                (p.window_start.isoformat(), p.window_end.isoformat(), p.delta)
                for p in again.points
            ]
            assert again_serialized == serialized
            assert again.total_usage == first.total_usage

    def test_points_have_time_bounds(self, svc):
        """每个增量片段都带有起止时间，且端点都落在查询窗口内"""
        feed(svc, [(0, 100.0), (5, 105.0), (10, 109.0)])
        stats = svc.usage("m", t(0), t(10))
        for p in stats.points:
            assert stats.window_start <= p.window_start < p.window_end <= stats.window_end
