"""核心业务规则层。

读数语义（累计计数器）：
- 每条读数是设备计数器在某一时刻的"累计值"，只增不减；
- 一段用电量 = 本段后读数 - 本段前读数；
- 回绕（计数器到达满度 capacity 后归零）：delta = (capacity - prev) + cur；
- 重启/计数器清零：前值失效，本段增量不可知，delta 记为 null（RESET），
  该读数成为新基准，绝不凭空产生用电量；
- 第一条读数没有前值，delta 记为 null（FIRST）。

判别规则（cur=本次读数, prev=上一条读数, M=设备单段最大合理增量, K=满度）：
1. (设备, 时间戳) 已存在且 value 相同 -> DUPLICATE，幂等返回原结果；
2. (设备, 时间戳) 已存在但 value 不同 -> 拒绝 CONFLICT_SAME_TS；
3. 时间戳早于已存最新读数 -> 拒绝 OUT_OF_ORDER（要求严格按时间递增上报）；
4. value < 0 或非有限数字 -> 拒绝 NEGATIVE_OR_INVALID；
5. cur > prev：delta=cur-prev；delta>M -> 拒绝 VALUE_TOO_LARGE，否则 NORMAL；
6. cur == prev：delta=0，NORMAL；
7. cur < prev：wrap=(K-prev)+cur；若 0<=wrap<=M -> WRAPAROUND（按回绕计入）；
   否则视为设备重启 RESET：接受读数但 delta=null，不产生电量。
注：cur>=prev 时绝不可能是回绕/重启，超上限只能拒绝。
"""

from __future__ import annotations

import math

from app.errors import DeviceNotFoundError, ReadingRejectedError
from app.models import (
    DeltaFlag,
    IngestResult,
    IngestStatus,
    ReadingView,
    RejectReason,
    StatItem,
)
from app.storage import MeterStorage
from app.timeutil import normalize_ts


class MeterService:
    def __init__(self, storage: MeterStorage):
        self.storage = storage

    # ---- 设备注册 ----

    def register_device(self, device_id: str, capacity: float, max_delta: float):
        return self.storage.register_device(device_id, capacity, max_delta)

    def _require_device(self, device_id: str) -> tuple[str, float, float]:
        dev = self.storage.get_device(device_id)
        if dev is None:
            raise DeviceNotFoundError(device_id)
        return dev

    # ---- 上报 ----

    def ingest(self, device_id: str, timestamp: str, value: float) -> IngestResult:
        ts = normalize_ts(timestamp)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ReadingRejectedError(
                RejectReason.NEGATIVE_OR_INVALID.value, "读数必须是有限数字"
            )
        value = float(value)
        if value < 0:
            raise ReadingRejectedError(
                RejectReason.NEGATIVE_OR_INVALID.value, f"累计读数不能为负: {value}"
            )

        _, capacity, max_delta = self._require_device(device_id)

        # 同一设备的"查最新/查重/写入"在同一把锁内完成，保证并发提交串行化
        with self.storage.lock_for(device_id):
            existing = self.storage.reading_at(device_id, ts)
            if existing is not None:
                if existing["value"] == value:
                    # 完全相同的重复上报：幂等返回，不重复计入
                    return IngestResult(
                        device_id=device_id, timestamp=ts, value=value,
                        status=IngestStatus.DUPLICATE,
                        delta_flag=DeltaFlag(existing["delta_flag"]),
                        delta=existing["delta"],
                        reason="同一读数重复上报，已幂等处理，不重复计入",
                    )
                self._reject(
                    device_id, ts, value,
                    RejectReason.CONFLICT_SAME_TS,
                    f"时间戳 {ts} 已存在读数 {existing['value']}，"
                    f"与本次 {value} 冲突；同一时刻只允许一个读数",
                )

            latest = self.storage.latest_reading(device_id)
            if latest is not None and ts < latest["ts"]:
                self._reject(
                    device_id, ts, value,
                    RejectReason.OUT_OF_ORDER,
                    f"时间倒序：{ts} 早于已存最新读数时间 {latest['ts']}，"
                    f"请按时间递增上报（如需补录请走管理员流程）",
                )

            flag: DeltaFlag
            delta: float | None
            if latest is None:
                flag, delta = DeltaFlag.FIRST, None
            else:
                prev = float(latest["value"])
                if value >= prev:
                    step = value - prev
                    if step > max_delta:
                        self._reject(
                            device_id, ts, value,
                            RejectReason.VALUE_TOO_LARGE,
                            f"增量 {step:g} 超过设备单段物理上限 {max_delta:g}"
                            f"（前值 {prev:g} -> {value:g}），疑似异常跳变，已拒绝",
                        )
                    flag, delta = DeltaFlag.NORMAL, round(step, 9)
                elif cur_is_wrap(value, prev, capacity, max_delta):
                    # 变小且按回绕算出的增量在物理上限内 -> 计数器回绕
                    flag, delta = DeltaFlag.WRAPAROUND, round(
                        (capacity - prev) + value, 9
                    )
                else:
                    # 变小但按回绕计算不成立 -> 设备重启/计数器清零
                    flag, delta = DeltaFlag.RESET, None

            self.storage.insert_reading(
                {
                    "device_id": device_id, "ts": ts, "value": value,
                    "delta": delta, "delta_flag": flag.value,
                    "ingest_status": IngestStatus.ACCEPTED.value,
                }
            )
            return IngestResult(
                device_id=device_id, timestamp=ts, value=value,
                status=IngestStatus.ACCEPTED, delta_flag=flag, delta=delta,
            )

    def _reject(self, device_id, ts, value, reason: RejectReason, detail: str):
        self.storage.insert_rejected(device_id, ts, value, reason.value, detail)
        raise ReadingRejectedError(reason.value, detail)

    # ---- 查询 ----

    def readings(self, device_id: str, start: str | None = None,
                 end: str | None = None) -> list[ReadingView]:
        self._require_device(device_id)
        s = normalize_ts(start) if start else None
        e = normalize_ts(end) if end else None
        rows = self.storage.list_readings(device_id, s, e)
        return [
            ReadingView(
                timestamp=r["ts"], value=r["value"],
                delta_flag=DeltaFlag(r["delta_flag"]), delta=r["delta"],
                status=IngestStatus(r["ingest_status"]),
            )
            for r in rows
        ]

    def statistics(self, device_id: str, start: str, end: str) -> StatItem:
        self._require_device(device_id)
        s, e = normalize_ts(start), normalize_ts(end)
        if s > e:
            raise ReadingRejectedError(
                "INVALID_WINDOW", f"时间窗口非法：start {s} 晚于 end {e}"
            )
        rows = self.storage.list_readings(device_id, s, e)

        # 每一行携带的 delta 是"从上一条已接受读数到本行"这段的增量；
        # 只累加窗口内的 NORMAL/WRAPAROUND 段。
        # RESET 段 delta 为 null（空档不产生电量）；离线空档本身就没有行，
        # 同样不产生电量。重复上报不会产生第二行，故不会重复计入。
        total = 0.0
        segment_count = 0
        reset_gaps = 0
        for r in rows:
            if r["delta_flag"] in (DeltaFlag.NORMAL.value, DeltaFlag.WRAPAROUND.value):
                if r["delta"] is not None:
                    total += float(r["delta"])
                    segment_count += 1
            elif r["delta_flag"] == DeltaFlag.RESET.value:
                reset_gaps += 1

        return StatItem(
            device_id=device_id, window_start=s, window_end=e,
            energy=round(total, 9), segment_count=segment_count,
            first_reading_ts=rows[0]["ts"] if rows else None,
            last_reading_ts=rows[-1]["ts"] if rows else None,
            reset_gaps=reset_gaps,
        )


def cur_is_wrap(cur: float, prev: float, capacity: float, max_delta: float) -> bool:
    """仅当变小且按回绕算出的增量为非负且不超过物理上限时，才认定为回绕。"""
    if cur >= prev:
        return False
    wrap = (capacity - prev) + cur
    return 0.0 <= wrap <= max_delta
