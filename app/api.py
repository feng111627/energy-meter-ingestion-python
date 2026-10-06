"""HTTP 接口层：上报读数、查询用电量。"""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .models import IngestStatus
from .service import MeterService
from .store import ReadingStore


class ReadingIn(BaseModel):
    device_id: str = Field(min_length=1)
    timestamp: datetime
    value: float = Field(ge=0.0)


class UsagePointOut(BaseModel):
    window_start: datetime
    window_end: datetime
    delta: float


class UsageOut(BaseModel):
    device_id: str
    window_start: datetime
    window_end: datetime
    total_usage: float
    points: list[UsagePointOut]
    reading_count: int


def create_app(db_path: str | None = None) -> FastAPI:
    """创建并初始化 FastAPI 应用。

    db_path 未指定时读取环境变量 METER_DB，默认落到本地文件
    data/energy.db，使服务重启后已计入数据不丢失；测试可传 ":memory:"。
    """
    app = FastAPI(title="energy-meter-ingestion", version="0.1.0")
    resolved = db_path or os.environ.get("METER_DB", "data/energy.db")
    if resolved not in (":memory:",) and os.path.dirname(resolved):
        os.makedirs(os.path.dirname(resolved), exist_ok=True)

    store = ReadingStore(resolved)
    store.connect()
    app.state.store = store
    app.state.service = MeterService(store)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "devices": app.state.service.stats_snapshot()}

    @app.post("/api/readings")
    def post_reading(payload: ReadingIn) -> dict:
        result = app.state.service.ingest(
            payload.device_id,
            payload.timestamp,
            payload.value,
        )
        body = {
            "status": result.status.value,
            "device_id": result.device_id,
            "timestamp": result.timestamp.isoformat(),
            "value": result.value,
            "delta": result.delta,
            "detail": result.detail,
        }
        if result.status is IngestStatus.REJECTED:
            body["reason"] = result.reason.value if result.reason else None
            raise HTTPException(status_code=422, detail=body)
        return body

    @app.get("/api/devices")
    def list_devices() -> dict:
        return {"devices": app.state.store.devices()}

    @app.get("/api/devices/{device_id}/readings")
    def get_readings(device_id: str) -> dict:
        readings = app.state.store.list(device_id)
        return {
            "device_id": device_id,
            "readings": [
                {"timestamp": r.timestamp.isoformat(), "value": r.value}
                for r in readings
            ],
        }

    @app.get("/api/devices/{device_id}/usage", response_model=UsageOut)
    def get_usage(device_id: str, start: datetime, end: datetime) -> UsageOut:
        stats = app.state.service.usage(device_id, start, end)
        return UsageOut(
            device_id=stats.device_id,
            window_start=stats.window_start,
            window_end=stats.window_end,
            total_usage=stats.total_usage,
            points=[
                UsagePointOut(
                    window_start=p.window_start, window_end=p.window_end, delta=p.delta
                )
                for p in stats.points
            ],
            reading_count=stats.reading_count,
        )

    return app
