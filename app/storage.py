"""SQLite 持久化层。

- readings：已接受（计入或作为基准）的读数及相邻增量，主键 (device_id, ts)
  从数据库层面保证同一设备同一时刻只能有一条读数；
- rejected_reports：被拒绝的上报留痕，便于审计"为什么没计入"。

WAL 模式 + 单一连接串行访问，配合业务层的"按设备锁"实现并发安全。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.errors import DeviceAlreadyExistsError
from app.timeutil import now_utc_iso


class MeterStorage:
    def __init__(self, db_path: str = "data/meter.db"):
        self.db_path = db_path
        self._device_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._db_lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ---- 连接与建表 ----

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")  # 提交即落盘，重启不丢
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                capacity  REAL NOT NULL,
                max_delta REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS readings (
                device_id   TEXT NOT NULL,
                ts          TEXT NOT NULL,
                value       REAL NOT NULL,
                delta       REAL,
                delta_flag  TEXT NOT NULL,
                ingest_status TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                PRIMARY KEY (device_id, ts)
            );
            CREATE INDEX IF NOT EXISTS idx_readings_dev_ts
                ON readings(device_id, ts);
            CREATE TABLE IF NOT EXISTS rejected_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                ts        TEXT NOT NULL,
                value     REAL NOT NULL,
                reason    TEXT NOT NULL,
                detail    TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        self._conn = conn

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.init_db()
        assert self._conn is not None
        return self._conn

    # ---- 设备 ----

    def register_device(self, device_id: str, capacity: float, max_delta: float) -> None:
        with self._db_lock:
            try:
                self._db().execute(
                    "INSERT INTO devices(device_id, capacity, max_delta, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (device_id, float(capacity), float(max_delta), now_utc_iso()),
                )
                self._db().commit()
            except sqlite3.IntegrityError as exc:
                raise DeviceAlreadyExistsError(device_id) from exc

    def get_device(self, device_id: str) -> tuple[str, float, float] | None:
        with self._db_lock:
            row = self._db().execute(
                "SELECT device_id, capacity, max_delta FROM devices WHERE device_id=?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["device_id"]), float(row["capacity"]), float(row["max_delta"])

    def list_devices(self) -> list[tuple[str, float, float]]:
        with self._db_lock:
            rows = self._db().execute(
                "SELECT device_id, capacity, max_delta FROM devices ORDER BY device_id"
            ).fetchall()
        return [(r["device_id"], float(r["capacity"]), float(r["max_delta"])) for r in rows]

    # ---- 并发控制：同一设备的写入串行化，跨设备可并行 ----

    def lock_for(self, device_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._device_locks.get(device_id)
            if lock is None:
                lock = threading.Lock()
                self._device_locks[device_id] = lock
            return lock

    # ---- 读数 ----

    def latest_reading(self, device_id: str) -> dict | None:
        with self._db_lock:
            row = self._db().execute(
                "SELECT device_id, ts, value, delta, delta_flag, ingest_status, ingested_at "
                "FROM readings WHERE device_id=? ORDER BY ts DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def reading_at(self, device_id: str, ts_iso: str) -> dict | None:
        with self._db_lock:
            row = self._db().execute(
                "SELECT device_id, ts, value, delta, delta_flag, ingest_status, ingested_at "
                "FROM readings WHERE device_id=? AND ts=?",
                (device_id, ts_iso),
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_reading(self, row: dict) -> None:
        with self._db_lock:
            self._db().execute(
                "INSERT INTO readings(device_id, ts, value, delta, delta_flag, "
                "ingest_status, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["device_id"],
                    row["ts"],
                    float(row["value"]),
                    None if row["delta"] is None else float(row["delta"]),
                    row["delta_flag"],
                    row["ingest_status"],
                    now_utc_iso(),
                ),
            )
            self._db().commit()

    def insert_rejected(self, device_id: str, ts_iso: str, value: float,
                        reason: str, detail: str) -> None:
        with self._db_lock:
            self._db().execute(
                "INSERT INTO rejected_reports(device_id, ts, value, reason, detail, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (device_id, ts_iso, float(value), reason, detail, now_utc_iso()),
            )
            self._db().commit()

    def list_readings(self, device_id: str, start_iso: str | None = None,
                      end_iso: str | None = None) -> list[dict]:
        sql = ("SELECT device_id, ts, value, delta, delta_flag, ingest_status, ingested_at "
               "FROM readings WHERE device_id=?")
        params: list = [device_id]
        if start_iso is not None:
            sql += " AND ts>=?"
            params.append(start_iso)
        if end_iso is not None:
            sql += " AND ts<=?"
            params.append(end_iso)
        sql += " ORDER BY ts ASC"
        with self._db_lock:
            rows = self._db().execute(sql, params).fetchall()
        return [dict(r) for r in rows]
