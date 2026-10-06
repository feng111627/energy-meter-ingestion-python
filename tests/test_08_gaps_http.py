"""业务⑧离线空档处理 与 HTTP 端到端流程。"""

from __future__ import annotations


def test_offline_gap_produces_no_energy(service):
    """用例8.1：10:00 后设备离线，直到 14:00 才有下一条读数。

    空档窗口（11:00~13:00）没有任何读数 => 电量 0，绝不凭空产生；
    而 10:00->14:00 这一段真实增量只计一次（30）。
    """
    service.register_device("dev", 1000.0, 500.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    service.ingest("dev", "2026-10-07T14:00:00Z", 130.0)

    gap = service.statistics("dev",
                             "2026-10-07T11:00:00Z", "2026-10-07T13:00:00Z")
    assert gap.energy == 0.0 and gap.segment_count == 0
    assert gap.first_reading_ts is None

    full = service.statistics("dev",
                              "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert full.energy == 30.0 and full.segment_count == 1
    # 重复查询稳定
    assert service.statistics(
        "dev", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z").energy == 30.0


def test_empty_window_before_first_reading(service):
    """用例8.2：设备上线前的窗口电量为 0。"""
    service.register_device("dev", 1000.0, 100.0)
    service.ingest("dev", "2026-10-07T12:00:00Z", 10.0)
    st = service.statistics("dev",
                            "2026-10-07T00:00:00Z", "2026-10-07T11:00:00Z")
    assert st.energy == 0.0 and st.segment_count == 0


def test_gap_plus_duplicate_together(service):
    """用例8.3：离线空档 + 重复上报同时存在，统计仍只算真实增量。"""
    service.register_device("dev", 1000.0, 500.0)
    service.ingest("dev", "2026-10-07T10:00:00Z", 0.0)
    service.ingest("dev", "2026-10-07T15:00:00Z", 42.0)   # 离线空档后的一条
    for _ in range(3):  # 重复重发，不重复计入
        service.ingest("dev", "2026-10-07T15:00:00Z", 42.0)
    st = service.statistics("dev",
                            "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")
    assert st.energy == 42.0 and st.segment_count == 1
    assert len(service.readings("dev")) == 2


# ---------------- HTTP 端到端 ----------------

def test_http_full_flow(client):
    """用例8.4：HTTP 跑通 注册->上报(含回绕/重复/倒序)->统计->明细查询。"""
    r = client.post("/devices", json={
        "device_id": "m1", "capacity": 1000.0, "max_delta": 900.0})
    assert r.status_code == 201

    def post(ts, v):
        return client.post("/readings", json={
            "device_id": "m1", "timestamp": ts, "value": v})

    assert post("2026-10-07T10:00:00Z", 950.0).json()["delta_flag"] == "FIRST"
    assert post("2026-10-07T11:00:00Z", 20.0).json()["delta_flag"] == "WRAPAROUND"
    dup = post("2026-10-07T11:00:00Z", 20.0).json()
    assert dup["status"] == "DUPLICATE" and dup["delta"] == 70.0
    bad = post("2026-10-07T09:00:00Z", 1.0)
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "OUT_OF_ORDER"

    r = client.get("/devices/m1/statistics", params={
        "start": "2026-10-07T00:00:00Z", "end": "2026-10-08T00:00:00Z"})
    st = r.json()
    assert st["energy"] == 70.0 and st["segment_count"] == 1

    rows = client.get("/devices/m1/readings").json()
    assert len(rows) == 2  # 重复上报不增加行


def test_http_validation_errors(client):
    """用例8.5：请求体校验（负数/缺字段/未知设备）返回 4xx 且带错误信息。"""
    client.post("/devices", json={
        "device_id": "m1", "capacity": 1000.0, "max_delta": 100.0})
    # Pydantic 层：负数 -> 422
    r = client.post("/readings", json={
        "device_id": "m1", "timestamp": "2026-10-07T10:00:00Z", "value": -5})
    assert r.status_code == 422
    # 业务层：超上限 -> 422 + 明确 code
    client.post("/readings", json={
        "device_id": "m1", "timestamp": "2026-10-07T10:00:00Z", "value": 10})
    r = client.post("/readings", json={
        "device_id": "m1", "timestamp": "2026-10-07T11:00:00Z", "value": 900})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALUE_TOO_LARGE"
    # 未知设备 -> 404
    r = client.post("/readings", json={
        "device_id": "nope", "timestamp": "2026-10-07T10:00:00Z", "value": 1})
    assert r.status_code == 404 and r.json()["error"]["code"] == "DEVICE_NOT_FOUND"


def test_http_multi_device_isolation(client):
    """用例8.6：多设备 HTTP 上报与统计相互隔离。"""
    for d in ("a", "b"):
        client.post("/devices", json={
            "device_id": d, "capacity": 1000.0, "max_delta": 100.0})
    client.post("/readings", json={"device_id": "a",
                "timestamp": "2026-10-07T10:00:00Z", "value": 10})
    client.post("/readings", json={"device_id": "a",
                "timestamp": "2026-10-07T11:00:00Z", "value": 40})
    client.post("/readings", json={"device_id": "b",
                "timestamp": "2026-10-07T10:00:00Z", "value": 0})
    client.post("/readings", json={"device_id": "b",
                "timestamp": "2026-10-07T11:00:00Z", "value": 25})
    params = {"start": "2026-10-07T00:00:00Z", "end": "2026-10-08T00:00:00Z"}
    assert client.get("/devices/a/statistics", params=params).json()["energy"] == 30.0
    assert client.get("/devices/b/statistics", params=params).json()["energy"] == 25.0
