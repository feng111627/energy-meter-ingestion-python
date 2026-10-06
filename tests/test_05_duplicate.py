"""业务⑤重复上报幂等：同一(设备,时刻,读数)重复提交不产生第二份增量。"""

from __future__ import annotations

from app.models import IngestStatus

W = ("2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")


def test_exact_duplicate_is_idempotent(service):
    """用例5.1：同一条读数重复上报 -> DUPLICATE，返回原 delta，电量不翻倍。"""
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    first = service.ingest("dev", "2026-10-07T11:00:00Z", 130.0)
    dup = service.ingest("dev", "2026-10-07T11:00:00Z", 130.0)
    assert first.status == IngestStatus.ACCEPTED and first.delta == 30.0
    assert dup.status == IngestStatus.DUPLICATE and dup.delta == 30.0
    assert len(service.readings("dev")) == 2
    st = service.statistics("dev", *W)
    assert st.energy == 30.0 and st.segment_count == 1


def test_duplicate_first_reading(service):
    """用例5.2：首条读数的重复提交同样幂等（delta 仍为 null）。"""
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    dup = service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    assert dup.status == IngestStatus.DUPLICATE and dup.delta is None
    assert len(service.readings("dev")) == 1


def test_repeated_queries_stable(service):
    """用例5.3：重复上报后多次统计，结果始终稳定一致。"""
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    service.ingest("dev", "2026-10-07T11:00:00Z", 130.0)
    service.ingest("dev", "2026-10-07T12:00:00Z", 160.0)
    for _ in range(5):
        service.ingest("dev", "2026-10-07T11:00:00Z", 130.0)
        service.ingest("dev", "2026-10-07T12:00:00Z", 160.0)
    energies = [service.statistics("dev", *W).energy for _ in range(5)]
    assert energies == [60.0] * 5
    assert len(service.readings("dev")) == 3


def test_duplicate_via_http_is_200_not_error(client):
    """用例5.4：HTTP 层重复上报返回 200 + DUPLICATE，而不是 4xx 报错。"""
    client.post("/devices", json={"device_id": "dev", "capacity": 1000.0,
                                  "max_delta": 100.0})
    body = {"device_id": "dev", "timestamp": "2026-10-07T10:00:00Z", "value": 100.0}
    assert client.post("/readings", json=body).json()["status"] == "ACCEPTED"
    r2 = client.post("/readings", json=body)
    assert r2.status_code == 200 and r2.json()["status"] == "DUPLICATE"
