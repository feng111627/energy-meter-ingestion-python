"""数据模型：读数、上报结果、统计结果等类型定义。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class IngestStatus(str, Enum):
    """上报受理状态。"""

    ACCEPTED_NEW = "accepted_new"          # 新读数已接受并计入
    ACCEPTED_DUPLICATE = "accepted_duplicate"  # 重复上报，幂等接受，不重复计入
    REJECTED = "rejected"                    # 读数被拒绝


class RejectReason(str, Enum):
    """拒绝原因（与业务规则一一对应）。"""

    OUT_OF_ORDER = "out_of_order"            # 时间倒序：存在更晚的读数
    CONFLICT = "conflict"                    # 同一时间点读数值不一致
    VALUE_DECREASE = "value_decrease"        # 读数变小且不构成合法回绕


@dataclass(frozen=True)
class Reading:
    """一台设备在某个时刻的累计电表读数。"""

    device_id: str
    timestamp: datetime
    value: float  # 累计读数，单位 kWh，单调不减（回绕除外）


@dataclass(frozen=True)
class IngestResult:
    """上报处理结果。"""

    status: IngestStatus
    device_id: str
    timestamp: datetime
    value: float
    delta: float | None = None
    reason: RejectReason | None = None
    detail: str | None = None


@dataclass(frozen=True)
class UsagePoint:
    """相邻两条读数之间的一个用电增量片段。"""

    window_start: datetime
    window_end: datetime
    delta: float


@dataclass(frozen=True)
class UsageStats:
    """按设备与时间窗口的用电量统计。"""

    device_id: str
    window_start: datetime
    window_end: datetime
    total_usage: float
    points: tuple[UsagePoint, ...]
    reading_count: int
