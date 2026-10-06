"""业务⑦持久化：服务重启后数据不丢、不重复，统计保持一致。"""

from __future__ import annotations

from pathlib import Path

from app.models import IngestStatus
from app.service import MeterService
from app.storage import MeterStorage

W = ("2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z")


def _fresh_service(db_path: str) -> MeterService:
    """模拟一次"服务重启"：新建存储/服务实例并重新打开同一个数据库文件。"""
    storage = MeterStorage(db_path)
    storage.init_db()
    return MeterService(storage)


def test_data_survives_restart(tmp_path):
    """用例7.1：重启后读数、增量、统计都还在，结果与重启前一致。"""
    db = str(tmp_path / "meter.db")
    svc = _fresh_service(db)
    svc.register_device("dev", 1000.0, 100.0)
    svc.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    svc.ingest("dev", "2026-10-07T11:00:00Z", 130.0)
    before = svc.statistics("dev", *W)
    assert before.energy == 30.0

    svc2 = _fresh_service(db)  # 重启
    rows = svc2.readings("dev")
    assert [(r.value, r.delta) for r in rows] == [(100.0, None), (130.0, 30.0)]
    after = svc2.statistics("dev", *W)
    assert after.energy == before.energy == 30.0
    assert after.segment_count == 1


def test_duplicate_after_restart_not_double_counted(tmp_path):
    """用例7.2：重启后重放同一条上报 -> DUPLICATE，不产生第二份电量。"""
    db = str(tmp_path / "meter.db")
    svc = _fresh_service(db)
    svc.register_device("dev", 1000.0, 100.0)
    svc.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    svc.ingest("dev", "2026-10-07T11:00:00Z", 130.0)

    svc2 = _fresh_service(db)
    r = svc2.ingest("dev", "2026-10-07T11:00:00Z", 130.0)
    assert r.status == IngestStatus.DUPLICATE and r.delta == 30.0
    assert len(svc2.readings("dev")) == 2
    assert svc2.statistics("dev", *W).energy == 30.0


def test_continue_after_restart_with_correct_baseline(tmp_path):
    """用例7.3：重启后继续上报，基准延续最后一条读数，增量计算正确。"""
    db = str(tmp_path / "meter.db")
    svc = _fresh_service(db)
    svc.register_device("dev", 1000.0, 100.0)
    svc.ingest("dev", "2026-10-07T10:00:00Z", 100.0)
    svc.ingest("dev", "2026-10-07T11:00:00Z", 130.0)

    svc2 = _fresh_service(db)
    r = svc2.ingest("dev", "2026-10-07T12:00:00Z", 155.0)
    assert r.delta == 25.0
    assert svc2.statistics("dev", *W).energy == 55.0


def test_sqlite_files_created(tmp_path):
    """用例7.4：数据库文件真实落盘（WAL 模式下主文件存在）。"""
    db = tmp_path / "meter.db"
    _fresh_service(str(db))
    assert Path(db).exists()
