"""HTTP 端到端测试：上报、拒绝、查询、重启恢复。"""

from __future__ import annotations

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.api import create_app

    app = create_app(":memory:")
    with TestClient(app) as c:
        yield c


def reading(device, minute, value):
    return {
        "device_id": device,
        "timestamp": f"2026-10-07T10:{minute:02d}:00Z",
        "value": value,
    }


class TestApi:
    """HTTP 端到端流程"""

    def test_health(self, client):
        """健康检查返回 ok 与设备概览"""
        assert client.get("/health").json()["status"] == "ok"

    def test_post_and_query_usage(self, client):
        """完整流程：上报两条读数 -> 查询窗口用电量为相邻增量"""
        assert client.post("/api/readings", json=reading("m1", 0, 100.0)).status_code == 200
        r = client.post("/api/readings", json=reading("m1", 5, 108.5))
        assert r.json()["delta"] == 8.5
        u = client.get(
            "/api/devices/m1/usage",
            params={"start": "2026-10-07T10:00:00Z", "end": "2026-10-07T10:05:00Z"},
        ).json()
        assert u["total_usage"] == 8.5
        assert u["points"][0]["delta"] == 8.5

    def test_duplicate_http_idempotent(self, client):
        """HTTP 重复上报返回 200 幂等结果，统计不重复计入"""
        client.post("/api/readings", json=reading("m1", 0, 100.0))
        r1 = client.post("/api/readings", json=reading("m1", 5, 105.0))
        r2 = client.post("/api/readings", json=reading("m1", 5, 105.0))
        assert r1.status_code == r2.status_code == 200
        assert r2.json()["status"] == "accepted_duplicate"
        listed = client.get("/api/devices/m1/readings").json()["readings"]
        assert len(listed) == 2

    def test_conflict_and_out_of_order_http_422(self, client):
        """冲突读数与倒序读数通过 HTTP 返回 422 并带中文原因"""
        client.post("/api/readings", json=reading("m1", 0, 100.0))
        client.post("/api/readings", json=reading("m1", 10, 110.0))

        conflict = client.post("/api/readings", json=reading("m1", 0, 200.0))
        assert conflict.status_code == 422
        assert conflict.json()["detail"]["reason"] == "conflict"

        out_of_order = client.post("/api/readings", json=reading("m1", 5, 105.0))
        assert out_of_order.status_code == 422
        assert out_of_order.json()["detail"]["reason"] == "out_of_order"

        decrease = client.post("/api/readings", json=reading("m1", 11, 5.0))
        assert decrease.status_code == 422
        assert decrease.json()["detail"]["reason"] == "value_decrease"

    def test_rollover_end_to_end(self, client):
        """端到端：回绕上报成功，窗口统计包含跨绕增量 400"""
        client.post("/api/readings", json=reading("m1", 0, 99800.0))
        r = client.post("/api/readings", json=reading("m1", 5, 200.0))
        assert r.status_code == 200
        assert r.json()["delta"] == 400.0
        u = client.get(
            "/api/devices/m1/usage",
            params={"start": "2026-10-07T10:00:00Z", "end": "2026-10-07T10:05:00Z"},
        ).json()
        assert u["total_usage"] == 400.0

    def test_concurrent_http_posts(self):
        """多设备通过 HTTP 线程池并发上报，服务端结果与单线程一致"""
        from app.api import create_app

        app = create_app(":memory:")
        with TestClient(app) as client:
            def post(args):
                device, minute = args
                return client.post(
                    "/api/readings", json=reading(device, minute, float(minute))
                )

            # 按分钟轮次并发：同一轮 4 台设备同时上报，轮次之间严格递增
            for minute in range(10):
                jobs = [(f"dev-{d}", minute) for d in range(4)]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    responses = list(pool.map(post, jobs))
                assert all(r.status_code == 200 for r in responses)
            for d in range(4):
                u = client.get(
                    f"/api/devices/dev-{d}/usage",
                    params={
                        "start": "2026-10-07T10:00:00Z",
                        "end": "2026-10-07T10:09:00Z",
                    },
                ).json()
                assert u["reading_count"] == 10
                assert u["total_usage"] == 9.0

    def test_persistence_across_http_restart(self):
        """HTTP 服务重启后数据仍在，旧读数重放保持幂等"""
        from app.api import create_app

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            app1 = create_app(path)
            with TestClient(app1) as c1:
                c1.post("/api/readings", json=reading("m1", 0, 100.0))
                c1.post("/api/readings", json=reading("m1", 5, 105.0))
            app1.state.store.close()

            app2 = create_app(path)
            with TestClient(app2) as c2:
                replay = c2.post("/api/readings", json=reading("m1", 5, 105.0))
                assert replay.json()["status"] == "accepted_duplicate"
                u = c2.get(
                    "/api/devices/m1/usage",
                    params={
                        "start": "2026-10-07T10:00:00Z",
                        "end": "2026-10-07T10:05:00Z",
                    },
                ).json()
                assert u["total_usage"] == 5.0
            app2.state.store.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass
