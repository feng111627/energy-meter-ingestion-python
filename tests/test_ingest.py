"""上报业务测试：回绕、倒序、重复、读数变小。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import IngestStatus, RejectReason
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


class TestIngest:
    """读数上报与异常规则"""

    def test_first_reading_is_baseline(self, svc):
        """首条读数作为累计基线，增量不定义、不计入用电量"""
        r = svc.ingest("m", t(0), 100.0)
        assert r.status is IngestStatus.ACCEPTED_NEW
        assert r.delta is None

    def test_normal_increment(self, svc):
        """正常递增读数的增量为相邻读数之差"""
        svc.ingest("m", t(0), 100.0)
        r = svc.ingest("m", t(5), 108.5)
        assert r.status is IngestStatus.ACCEPTED_NEW
        assert r.delta == pytest.approx(8.5)

    def test_duplicate_idempotent_not_counted(self, svc):
        """重复上报同值读数幂等接受，不重复计入增量"""
        svc.ingest("m", t(0), 100.0)
        svc.ingest("m", t(5), 108.5)
        dup = svc.ingest("m", t(5), 108.5)
        assert dup.status is IngestStatus.ACCEPTED_DUPLICATE
        assert dup.delta is None
        # 再来一条新读数，增量仍以 108.5 为前值，未被重复上报影响
        nxt = svc.ingest("m", t(10), 112.0)
        assert nxt.delta == pytest.approx(3.5)

    def test_conflict_same_timestamp_rejected(self, svc):
        """同一时间点不同读数值相互冲突，拒绝并说明原因"""
        svc.ingest("m", t(0), 100.0)
        r = svc.ingest("m", t(0), 120.0)
        assert r.status is IngestStatus.REJECTED
        assert r.reason is RejectReason.CONFLICT
        assert "冲突" in r.detail

    def test_out_of_order_rejected(self, svc):
        """时间戳早于最新读数的上报按时间倒序拒绝"""
        svc.ingest("m", t(0), 100.0)
        svc.ingest("m", t(10), 110.0)
        r = svc.ingest("m", t(5), 105.0)
        assert r.status is IngestStatus.REJECTED
        assert r.reason is RejectReason.OUT_OF_ORDER
        assert "倒序" in r.detail
        # 被拒绝的读数没有入库，统计与最新读数不受影响
        assert len(svc.store.list("m")) == 2

    def test_counter_rollover_accepted(self, svc):
        """计数器到顶回绕：99800 -> 200 按量程 100000 计算跨绕增量 400"""
        svc.ingest("m", t(0), 100.0)
        svc.ingest("m", t(1), 99800.0)
        r = svc.ingest("m", t(2), 200.0)
        assert r.status is IngestStatus.ACCEPTED_NEW
        assert r.delta == pytest.approx(400.0)
        assert "回绕" in r.detail

    def test_rollover_then_growth_continues(self, svc):
        """回绕之后读数继续正常递增，增量计算连续正确"""
        svc.ingest("m", t(0), 99900.0)
        svc.ingest("m", t(1), 50.0)       # 回绕，增量 150
        r = svc.ingest("m", t(2), 120.0)  # 回绕后正常增长
        assert r.delta == pytest.approx(70.0)

    def test_reset_like_decrease_rejected(self, svc):
        """读数下降但不构成回绕（疑似重启复位/预置值），拒绝"""
        svc.ingest("m", t(0), 100.0)
        svc.ingest("m", t(1), 500.0)
        r = svc.ingest("m", t(2), 10.0)
        assert r.status is IngestStatus.REJECTED
        assert r.reason is RejectReason.VALUE_DECREASE
        assert "复位" in r.detail or "重启" in r.detail

    def test_drop_from_mid_value_rejected(self, svc):
        """旧值未接近量程顶部时即使新值接近零也不算回绕"""
        svc.ingest("m", t(0), 100.0)
        svc.ingest("m", t(1), 5000.0)
        r = svc.ingest("m", t(2), 10.0)
        assert r.status is IngestStatus.REJECTED
        assert r.reason is RejectReason.VALUE_DECREASE

    def test_custom_rollover_limit(self):
        """可配置计数器量程：量程 1000 时 990 -> 20 为合法回绕"""
        store = ReadingStore()
        store.connect()
        svc = MeterService(store, rollover=1000.0)
        svc.ingest("m", t(0), 10.0)
        svc.ingest("m", t(1), 990.0)
        r = svc.ingest("m", t(2), 20.0)
        assert r.status is IngestStatus.ACCEPTED_NEW
        assert r.delta == pytest.approx(30.0)
        store.close()

    def test_multiple_devices_independent(self, svc):
        """多台设备的读数序列与增量各自独立"""
        svc.ingest("a", t(0), 0.0)
        svc.ingest("b", t(0), 50.0)
        ra = svc.ingest("a", t(5), 5.0)
        rb = svc.ingest("b", t(5), 60.0)
        assert ra.delta == pytest.approx(5.0)
        assert rb.delta == pytest.approx(10.0)

    def test_naive_timestamp_treated_as_utc(self, svc):
        """无时区时间按 UTC 归一化，等价时间戳仍能识别为重复上报"""
        naive = datetime(2026, 10, 7, 10, 0)
        svc.ingest("m", naive, 100.0)
        r = svc.ingest("m", t(0), 100.0)
        assert r.status is IngestStatus.ACCEPTED_DUPLICATE
