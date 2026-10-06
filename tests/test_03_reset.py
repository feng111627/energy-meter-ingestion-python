"""业务③设备重启/计数器清零：接受读数作为新基准，但增量不可知、不产生电量。"""

from __future__ import annotations

import pytest

from app.models import DeltaFlag


def test_reset_accepted_but_no_delta(service):
    """用例3.1：大幅变小且回绕不成立 -> RESET，delta=null 且不计入电量。"""
    service.register_device("dev", capacity=1000.0, max_delta=100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 500.0)
    r = service.ingest("dev", "2026-10-07T11:00:00Z", 2.0)  # wrap=502 > 100
    assert r.delta_flag == DeltaFlag.RESET and r.delta is None
    st = service.statistics("dev", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert st.energy == 0.0 and st.reset_gaps == 1


def test_baseline_resumes_after_reset(service):
    """用例3.2：重启后的读数以新基准继续计量，历史数据不丢。"""
    service.register_device("dev", capacity=1000.0, max_delta=100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 500.0)
    service.ingest("dev", "2026-10-07T11:00:00Z", 2.0)     # RESET
    r = service.ingest("dev", "2026-10-07T12:00:00Z", 40.0)  # 40-2=38
    assert r.delta_flag == DeltaFlag.NORMAL
    assert r.delta == pytest.approx(38.0)
    st = service.statistics("dev", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert st.energy == pytest.approx(38.0) and st.reset_gaps == 1
    # 重启前那条读数仍可查
    rows = service.readings("dev")
    assert [row.value for row in rows] == [500.0, 2.0, 40.0]


def test_multiple_resets(service):
    """用例3.3：多次重启各自只产生一个不计电量的空档段，绝不出现负电量。"""
    service.register_device("dev", capacity=1000.0, max_delta=100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 500.0)
    service.ingest("dev", "2026-10-07T11:00:00Z", 10.0)   # RESET
    service.ingest("dev", "2026-10-07T12:00:00Z", 60.0)   # +50
    service.ingest("dev", "2026-10-07T13:00:00Z", 1.0)    # RESET (wrap=941)
    service.ingest("dev", "2026-10-07T14:00:00Z", 20.0)   # +19
    st = service.statistics("dev", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert st.energy == pytest.approx(69.0)
    assert st.reset_gaps == 2 and st.segment_count == 2
