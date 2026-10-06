"""业务④异常拒绝：时间倒序、同时刻冲突、超上限、负数、非法时间戳。"""

from __future__ import annotations

import pytest

from app.errors import DeviceNotFoundError, ReadingRejectedError


def _ingest_ok(service, ts, v):
    return service.ingest("dev", f"2026-10-07T{ts}", v)


def test_out_of_order_rejected(service):
    """用例4.1：早于最新读数的上报被拒绝，返回 OUT_OF_ORDER 与可读原因。"""
    service.register_device("dev", 1000.0, 100.0)
    _ingest_ok(service, "10:00:00Z", 100.0)
    _ingest_ok(service, "12:00:00Z", 140.0)
    with pytest.raises(ReadingRejectedError) as ei:
        _ingest_ok(service, "11:00:00Z", 120.0)
    assert ei.value.code == "OUT_OF_ORDER"
    assert "时间倒序" in ei.value.message
    # 被拒绝后数据不被污染：统计仍只有 100->140 这一段
    st = service.statistics("dev", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert st.energy == 40.0 and st.segment_count == 1
    assert len(service.readings("dev")) == 2


def test_same_timestamp_conflict(service):
    """用例4.2：同一时刻两个不同读数 -> CONFLICT_SAME_TS，只保留第一个。"""
    service.register_device("dev", 1000.0, 100.0)
    _ingest_ok(service, "10:00:00Z", 100.0)
    with pytest.raises(ReadingRejectedError) as ei:
        _ingest_ok(service, "10:00:00Z", 105.0)
    assert ei.value.code == "CONFLICT_SAME_TS"
    assert service.readings("dev")[0].value == 100.0


def test_value_too_large_jump_rejected(service):
    """用例4.3：单调上涨但超过设备单段物理上限 -> VALUE_TOO_LARGE 拒绝。"""
    service.register_device("dev", 1000.0, 100.0)
    _ingest_ok(service, "10:00:00Z", 100.0)
    with pytest.raises(ReadingRejectedError) as ei:
        _ingest_ok(service, "11:00:00Z", 500.0)  # +400 > 100
    assert ei.value.code == "VALUE_TOO_LARGE"
    assert "物理上限" in ei.value.message


def test_negative_value_rejected(service):
    """用例4.4：负数读数直接拒绝（累计值不可能为负）。"""
    service.register_device("dev", 1000.0, 100.0)
    with pytest.raises(ReadingRejectedError) as ei:
        service.ingest("dev", "2026-10-07T10:00:00Z", -0.1)
    assert ei.value.code == "NEGATIVE_OR_INVALID"


def test_bad_timestamp_rejected(service):
    """用例4.5：无法解析的时间戳抛 ValueError（API 层转为 422）。"""
    service.register_device("dev", 1000.0, 100.0)
    with pytest.raises(ValueError):
        service.ingest("dev", "not-a-time", 1.0)


def test_unknown_device_rejected(service):
    """用例4.6：未注册设备上报 -> DEVICE_NOT_FOUND。"""
    with pytest.raises(DeviceNotFoundError):
        service.ingest("ghost", "2026-10-07T10:00:00Z", 1.0)


def test_inverted_window_rejected(service):
    """用例4.7：统计窗口 start 晚于 end 被拒绝。"""
    service.register_device("dev", 1000.0, 100.0)
    with pytest.raises(ReadingRejectedError) as ei:
        service.statistics("dev", "2026-10-08T00:00:00Z", "2026-10-07T00:00:00Z")
    assert ei.value.code == "INVALID_WINDOW"


def test_rejected_report_is_audited(storage, service):
    """用例4.8：被拒绝的上报在 rejected_reports 留痕，可解释"为什么没计入"。"""
    service.register_device("dev", 1000.0, 100.0)
    _ingest_ok(service, "12:00:00Z", 100.0)
    try:
        _ingest_ok(service, "11:00:00Z", 90.0)
    except ReadingRejectedError:
        pass
    with storage._db() as conn:
        rows = conn.execute("SELECT reason, detail FROM rejected_reports").fetchall()
    assert len(rows) == 1 and rows[0]["reason"] == "OUT_OF_ORDER"
    assert rows[0]["detail"]
