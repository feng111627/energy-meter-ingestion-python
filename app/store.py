"""存储层：基于 SQLite 的读数持久化，线程安全。

设计要点：
- readings 表以 (device_id, timestamp) 为联合主键，重复上报在数据库层面
  即无法产生第二行，保证"同一读数并发提交"的幂等性；
- 时间戳统一转换为 UTC ISO-8601 字符串存储，同一格式下字符串排序与时间
  排序一致，查询结果稳定；
- 单连接 + RLock 串行化进程内的写入（FastAPI 同步端点运行在线程池中），
  服务重启只需重新打开同一个数据库文件，已计入数据不丢、不重。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from .models import Reading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    device_id TEXT NOT NULL,
    ts        TEXT NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (device_id, ts)
)
"""


def _to_ts(dt: datetime) -> str:
    """把时间归一化为带时区的 UTC ISO-8601 字符串。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


class ReadingStore:
    """读数存储。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # -- 生命周期 -----------------------------------------------------------
    def connect(self) -> None:
        """建立连接并初始化表结构。"""
        with self._lock:
            if self._conn is not None:
                return
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.commit()
            self._conn = conn

    def close(self) -> None:
        """关闭连接。"""
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    @property
    def lock(self) -> threading.RLock:
        """对外暴露的写锁，业务层可用它把"检查+写入"做成原子操作。"""
        return self._lock

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store 尚未 connect()")
        return self._conn

    # -- 写入 ---------------------------------------------------------------
    def insert(self, reading: Reading) -> bool:
        """插入一条读数。

        返回 True 表示新插入；False 表示 (device_id, timestamp) 已存在
        （无论值是否相同，冲突判定交给业务层）。
        """
        with self._lock:
            cur = self._require_conn().execute(
                "INSERT OR IGNORE INTO readings(device_id, ts, value) VALUES (?, ?, ?)",
                (reading.device_id, _to_ts(reading.timestamp), float(reading.value)),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # -- 查询 ---------------------------------------------------------------
    def _hydrate(self, row: sqlite3.Row) -> Reading:
        return Reading(
            device_id=row["device_id"],
            timestamp=_from_ts(row["ts"]),
            value=row["value"],
        )

    def list(self, device_id: str) -> list[Reading]:
        """按时间升序列出某设备的全部读数。"""
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM readings WHERE device_id = ? ORDER BY ts ASC",
                (device_id,),
            ).fetchall()
        return [self._hydrate(r) for r in rows]

    def latest(self, device_id: str) -> Reading | None:
        """获取某设备最新一条读数。"""
        with self._lock:
            row = self._require_conn().execute(
                "SELECT * FROM readings WHERE device_id = ? ORDER BY ts DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        return self._hydrate(row) if row is not None else None

    def get(self, device_id: str, timestamp: datetime) -> Reading | None:
        """按精确时间点获取读数。"""
        with self._lock:
            row = self._require_conn().execute(
                "SELECT * FROM readings WHERE device_id = ? AND ts = ?",
                (device_id, _to_ts(timestamp)),
            ).fetchone()
        return self._hydrate(row) if row is not None else None

    def range(
        self, device_id: str, start: datetime, end: datetime
    ) -> list[Reading]:
        """按闭区间 [start, end] 升序返回读数。"""
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT * FROM readings WHERE device_id = ? AND ts BETWEEN ? AND ?"
                " ORDER BY ts ASC",
                (device_id, _to_ts(start), _to_ts(end)),
            ).fetchall()
        return [self._hydrate(r) for r in rows]

    def devices(self) -> list[str]:
        """列出所有已知设备。"""
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT DISTINCT device_id FROM readings ORDER BY device_id ASC"
            ).fetchall()
        return [r["device_id"] for r in rows]
