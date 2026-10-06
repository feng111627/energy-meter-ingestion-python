"""业务逻辑层：读数上报校验、增量计算、用量统计。

读数语义
--------
每条读数是设备累计电能计数器（单位 kWh）在某一时刻的快照。设备重启后
计数器可能归零、也可能被配置为某个预置值；计数器到达量程上限会回绕到
接近零的值。因此"后一条读数比前一条小"并不总是异常，需要按可区分的
规则分别处理，详见 ingest() 与 README。

并发与幂等
----------
"查重/查最新 + 写入" 在 store.lock 内原子完成；底层 (device_id, ts)
为主键，同一读数被并发提交只有一个请求真正写入，结果幂等。
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import (
    IngestResult,
    IngestStatus,
    Reading,
    RejectReason,
    UsagePoint,
    UsageStats,
)
from .store import ReadingStore

# 默认计数器回绕量程（kWh）。累计值到达该值后回绕到接近 0。
DEFAULT_ROLLOVER = 100_000.0
# 回绕判定：新值必须小于上一值的该比例，才可能被认定为"回绕到接近零"。
_ROLLOVER_LOW_RATIO = 0.05


def _ensure_utc(dt: datetime) -> datetime:
    """时间归一化：无时区按 UTC 处理，有时区换算为 UTC，保证排序稳定。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class MeterService:
    """电表读数采集与统计服务。"""

    def __init__(self, store: ReadingStore, rollover: float = DEFAULT_ROLLOVER) -> None:
        self.store = store
        self.rollover = float(rollover)

    # ---------------------------------------------------------------- 上报
    def ingest(
        self,
        device_id: str,
        timestamp: datetime,
        value: float,
    ) -> IngestResult:
        """受理一条读数上报。

        返回 ACCEPTED_NEW（新计入，含 delta）、ACCEPTED_DUPLICATE（重复，
        delta=None 表示不重复计入）或 REJECTED（带 reason/detail）。
        计数器量程为服务级配置，上报与统计使用同一口径。
        """
        ts = _ensure_utc(timestamp)
        value = float(value)
        limit = self.rollover

        with self.store.lock:
            existing = self.store.get(device_id, ts)
            # 规则 1/2：同一时间点已经有读数
            if existing is not None:
                if existing.value == value:
                    return IngestResult(
                        status=IngestStatus.ACCEPTED_DUPLICATE,
                        device_id=device_id,
                        timestamp=ts,
                        value=value,
                        delta=None,
                        detail="重复上报：同一设备同一时间点读数相同，幂等接受且不重复计入",
                    )
                return IngestResult(
                    status=IngestStatus.REJECTED,
                    device_id=device_id,
                    timestamp=ts,
                    value=value,
                    reason=RejectReason.CONFLICT,
                    detail=(
                        f"同一时间点 {ts.isoformat()} 已存在读数 {existing.value}，"
                        f"本次上报 {value} 与之冲突"
                    ),
                )

            latest = self.store.latest(device_id)

            # 规则 3：时间倒序（严格早于已有最新读数；等于已在上面处理）
            if latest is not None and ts < latest.timestamp:
                return IngestResult(
                    status=IngestStatus.REJECTED,
                    device_id=device_id,
                    timestamp=ts,
                    value=value,
                    reason=RejectReason.OUT_OF_ORDER,
                    detail=(
                        f"读数时间 {ts.isoformat()} 早于已有最新读数时间 "
                        f"{latest.timestamp.isoformat()}，按时间倒序拒绝"
                    ),
                )

            # 计算与上一条读数之间的增量；第一条读数没有前驱，增量不定义
            delta: float | None
            if latest is None:
                delta = None
                detail = "该设备首条读数，作为累计基线，增量不定义（不计入用电量）"
            elif value >= latest.value:
                # 规则 4：正常递增
                delta = value - latest.value
                detail = "正常递增"
            elif self._is_rollover(latest.value, value, limit):
                # 规则 5：计数器回绕：旧值已接近量程上限，新值回到接近零
                delta = (limit - latest.value) + value
                detail = (
                    f"识别为计数器回绕（量程 {limit:g} kWh），"
                    f"增量跨回绕计算为 {delta:g}"
                )
            else:
                # 规则 6：变小但不构成合法回绕 —— 疑似重启归零/预置，拒绝
                return IngestResult(
                    status=IngestStatus.REJECTED,
                    device_id=device_id,
                    timestamp=ts,
                    value=value,
                    reason=RejectReason.VALUE_DECREASE,
                    detail=(
                        f"读数由 {latest.value} 下降为 {value}，但不满足回绕条件"
                        f"（新值需小于旧值的 {_ROLLOVER_LOW_RATIO:.0%} 且旧值接近"
                        f"量程 {limit:g}），疑似设备重启/计数器复位，拒绝以免产生不合理增量"
                    ),
                )

            inserted = self.store.insert(Reading(device_id, ts, value))
            # 锁内刚查过不存在；并发下来到这里理论上不会失败，失败则按重复处理
            if not inserted:
                return IngestResult(
                    status=IngestStatus.ACCEPTED_DUPLICATE,
                    device_id=device_id,
                    timestamp=ts,
                    value=value,
                    delta=None,
                    detail="并发重复提交，幂等处理",
                )

            return IngestResult(
                status=IngestStatus.ACCEPTED_NEW,
                device_id=device_id,
                timestamp=ts,
                value=value,
                delta=delta,
                detail=detail,
            )

    @staticmethod
    def _is_rollover(prev_value: float, new_value: float, limit: float) -> bool:
        """判断一次下降是否为合法的计数器回绕。

        条件（同时满足）：
        1. 回绕前的旧值已处在量程顶部 5% 区间；
        2. 回绕后的新值落在量程底部 5% 区间（计数器回到接近零）。
        两个条件把"回绕"与"重启归零/预置到非零值"区分开。
        """
        if limit <= 0:
            return False
        near_top = prev_value >= limit * (1.0 - _ROLLOVER_LOW_RATIO)
        near_bottom = 0.0 <= new_value <= limit * _ROLLOVER_LOW_RATIO
        return near_top and near_bottom

    # ---------------------------------------------------------------- 查询
    def usage(
        self,
        device_id: str,
        start: datetime,
        end: datetime,
    ) -> UsageStats:
        """计算设备在闭区间 [start, end] 内的用电量。

        只统计"起点与终点都落在窗口内"的相邻增量：查询窗口比已有读数
        更宽时（含设备离线造成的空档），不会凭空产生或摊派任何用电量；
        重复读数在存储层就只有一行，天然不会被重复计入。
        """
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        readings = self.store.range(device_id, start, end)

        points: list[UsagePoint] = []
        for prev, cur in zip(readings, readings[1:]):
            point_delta = self._delta_between(prev.value, cur.value)
            points.append(
                UsagePoint(
                    window_start=prev.timestamp,
                    window_end=cur.timestamp,
                    delta=point_delta,
                )
            )

        total = round(sum(p.delta for p in points), 9)
        return UsageStats(
            device_id=device_id,
            window_start=start,
            window_end=end,
            total_usage=total,
            points=tuple(points),
            reading_count=len(readings),
        )

    def _delta_between(self, prev_value: float, cur_value: float) -> float:
        """计算两条相邻读数之间的增量（含回绕）。

        存储中的读数均已通过上报校验，相邻下降只能是合法回绕。
        """
        if cur_value >= prev_value:
            return round(cur_value - prev_value, 9)
        if self._is_rollover(prev_value, cur_value, self.rollover):
            return round((self.rollover - prev_value) + cur_value, 9)
        # 理论不可达：任何非回绕下降都在 ingest 阶段被拒绝
        return 0.0

    # ------------------------------------------------------------- 概览
    def stats_snapshot(self) -> dict:
        """返回当前所有设备的读数条数概览（演示/健康检查用）。"""
        return {
            device_id: {"reading_count": len(self.store.list(device_id))}
            for device_id in self.store.devices()
        }
