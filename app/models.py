"""领域模型与状态枚举（纯数据定义，不含业务逻辑）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class IngestStatus(str, Enum):
    """单次读数上报的处理结果。"""

    ACCEPTED = "ACCEPTED"          # 正常接受，已计入增量
    DUPLICATE = "DUPLICATE"        # 完全重复上报，幂等返回，不重复计入
    REJECTED = "REJECTED"          # 非法读数，被拒绝且未计入


class RejectReason(str, Enum):
    """读数被拒绝（或增量不可计）的具体原因。"""

    OUT_OF_ORDER = "OUT_OF_ORDER"          # 时间倒序（严格早于已存最新读数）
    CONFLICT_SAME_TS = "CONFLICT_SAME_TS"  # 同一时间戳但读数不同
    VALUE_TOO_LARGE = "VALUE_TOO_LARGE"    # 单段增量超过设备物理上限
    NEGATIVE_OR_INVALID = "NEGATIVE_OR_INVALID"  # 读数非法（为负/非数字）


class DeltaFlag(str, Enum):
    """相邻读数之间这段增量的性质。"""

    NORMAL = "NORMAL"        # 正常单调递增
    WRAPAROUND = "WRAPAROUND"  # 计数器回绕，按 (capacity-prev)+cur 计算
    RESET = "RESET"          # 设备重启/计数器清零，增量不可知，不计入
    FIRST = "FIRST"          # 该设备首条读数，没有前值可比


class RegisterDeviceRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    capacity: float = Field(gt=0, description="计数器满度（回绕阈值），如 999999.9")
    max_delta: float = Field(gt=0, description="两次上报间物理上可能的最大增量")


class DeviceInfo(BaseModel):
    device_id: str
    capacity: float
    max_delta: float


class ReadingRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    timestamp: str = Field(description="ISO8601，如 2026-10-07T10:00:00Z")
    value: float = Field(description="累计电表读数（非负）")


class IngestResult(BaseModel):
    device_id: str
    timestamp: str
    value: float
    status: IngestStatus
    delta_flag: DeltaFlag | None = None
    delta: float | None = Field(None, description="本段用电增量；RESET/FIRST/拒绝时为 null")
    reason: str | None = None


class StatItem(BaseModel):
    device_id: str
    window_start: str
    window_end: str
    energy: float = Field(description="窗口内可计入的用电总量")
    segment_count: int = Field(description="计入统计的增量段数")
    first_reading_ts: str | None = None
    last_reading_ts: str | None = None
    reset_gaps: int = Field(0, description="窗口内 RESET 空档段数（不产生电量）")


class ReadingView(BaseModel):
    timestamp: str
    value: float
    delta_flag: DeltaFlag
    delta: float | None
    status: IngestStatus
