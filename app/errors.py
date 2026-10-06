"""服务层异常类型。"""

from __future__ import annotations


class MeterServiceError(Exception):
    """业务错误基类。"""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class DeviceNotFoundError(MeterServiceError):
    def __init__(self, device_id: str):
        super().__init__("DEVICE_NOT_FOUND", f"设备不存在: {device_id}", 404)


class DeviceAlreadyExistsError(MeterServiceError):
    def __init__(self, device_id: str):
        super().__init__("DEVICE_ALREADY_EXISTS", f"设备已注册: {device_id}", 409)


class ReadingRejectedError(MeterServiceError):
    """读数被拒绝：调用方应收到 422，并带明确原因。"""

    def __init__(self, reason: str, message: str):
        super().__init__(reason, message, 422)
