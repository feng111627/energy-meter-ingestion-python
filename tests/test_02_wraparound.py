"""业务②计数器回绕：到达满度后归零，按 (capacity-prev)+cur 计算增量。"""

from __future__ import annotations

import pytest

from app.errors import ReadingRejectedError
from app.models import DeltaFlag


def test_wraparound_basic(service):
    """用例2.1：990 -> 20（满度1000、单段上限100）=> (1000-990)+20=30。"""
    service.register_device("dev", capacity=1000.0, max_delta=100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 990.0)
    r = service.ingest("dev", "2026-10-07T11:00:00Z", 20.0)
    assert r.delta_flag == DeltaFlag.WRAPAROUND
    assert r.delta == pytest.approx(30.0)


def test_wraparound_exact_boundary(service):
    """用例2.2：正好从满度归零：1000 -> 0 => (1000-1000)+0=0。"""
    service.register_device("dev", capacity=1000.0, max_delta=100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 1000.0)
    r = service.ingest("dev", "2026-10-07T11:00:00Z", 0.0)
    assert r.delta_flag == DeltaFlag.WRAPAROUND and r.delta == 0.0
    # 回绕后继续正常计量
    r2 = service.ingest("dev", "2026-10-07T12:00:00Z", 45.0)
    assert r2.delta_flag == DeltaFlag.NORMAL and r2.delta == 45.0


def test_wraparound_counted_in_statistics(service):
    """用例2.3：回绕段作为真实用电计入窗口统计。"""
    service.register_device("dev", capacity=1000.0, max_delta=100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 950.0)
    service.ingest("dev", "2026-10-07T11:00:00Z", 20.0)   # wrap 70
    service.ingest("dev", "2026-10-07T12:00:00Z", 30.0)   # normal 10
    st = service.statistics("dev", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert st.energy == pytest.approx(80.0) and st.segment_count == 2


def test_smaller_value_but_wrap_too_big_is_reset(service):
    """用例2.4：读数变小但按回绕算出的增量超物理上限 -> 不是回绕，是重启 RESET。

    900 -> 5，wrap=(1000-900)+5=105 > max_delta=100，不能凭空计 105 度。
    """
    service.register_device("dev", capacity=1000.0, max_delta=100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 900.0)
    r = service.ingest("dev", "2026-10-07T11:00:00Z", 5.0)
    assert r.delta_flag == DeltaFlag.RESET and r.delta is None
    st = service.statistics("dev", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert st.energy == 0.0 and st.reset_gaps == 1
