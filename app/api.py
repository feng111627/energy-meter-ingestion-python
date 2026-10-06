"""FastAPI 路由层。

接口：
- POST /devices                        注册设备（capacity/max_delta）
- GET  /devices                        设备列表
- POST /readings                       上报一条累计读数
- GET  /devices/{id}/readings          查询读数明细（可带 start/end）
- GET  /devices/{id}/statistics        查询时间窗口用电量统计
- GET  /health                         健康检查
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import MeterServiceError
from app.models import (
    DeviceInfo,
    ReadingRequest,
    ReadingView,
    RegisterDeviceRequest,
    StatItem,
)
from app.service import MeterService
from app.storage import MeterStorage
from app.timeutil import now_utc_iso


def create_app(db_path: str = "data/meter.db") -> FastAPI:
    storage = MeterStorage(db_path)
    storage.init_db()
    service = MeterService(storage)

    app = FastAPI(title="本地电表读数采集与用电量统计服务", version="0.1.0")
    app.state.service = service

    @app.exception_handler(MeterServiceError)
    async def _on_service_error(_: Request, exc: MeterServiceError) -> JSONResponse:
        # 拒绝/冲突等错误带明确 code 与中文原因，方便上报方定位
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "time": now_utc_iso()}

    @app.post("/devices", response_model=DeviceInfo, status_code=201)
    def register_device(req: RegisterDeviceRequest) -> DeviceInfo:
        service.register_device(req.device_id, req.capacity, req.max_delta)
        return DeviceInfo(device_id=req.device_id, capacity=req.capacity,
                          max_delta=req.max_delta)

    @app.get("/devices", response_model=list[DeviceInfo])
    def list_devices() -> list[DeviceInfo]:
        return [
            DeviceInfo(device_id=d, capacity=c, max_delta=m)
            for d, c, m in storage.list_devices()
        ]

    @app.post("/readings")
    def post_reading(req: ReadingRequest) -> dict:
        result = service.ingest(req.device_id, req.timestamp, req.value)
        # 重复上报幂等：HTTP 200，status=DUPLICATE，不产生新数据
        return result.model_dump(mode="json")

    @app.get("/devices/{device_id}/readings", response_model=list[ReadingView])
    def get_readings(device_id: str, start: str | None = None,
                     end: str | None = None) -> list[ReadingView]:
        return service.readings(device_id, start, end)

    @app.get("/devices/{device_id}/statistics", response_model=StatItem)
    def get_statistics(device_id: str, start: str, end: str) -> StatItem:
        return service.statistics(device_id, start, end)

    return app
