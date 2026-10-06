"""时间戳解析与规范化。

对外接受 ISO8601（含 Z 或显式偏移量）；内部统一转成 UTC，
以 "YYYY-MM-DDTHH:MM:SS.ffffffZ" 形式存储与比较，保证等价写法
（如 2026-10-07T10:00:00Z 与 2026-10-07T18:00:00+08:00）被视为同一时刻。
"""

from __future__ import annotations

from datetime import datetime, timezone


def normalize_ts(raw: str) -> str:
    """把 ISO8601 字符串规范化为 UTC 规范字符串；无法解析时抛 ValueError。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("时间戳为空")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # 不带时区的时间戳一律按 UTC 处理，规则明确、不依赖部署机器时区
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
