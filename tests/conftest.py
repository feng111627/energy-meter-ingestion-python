"""测试公共夹具与日志增强。

日志增强：每个用例开始时打印其所属业务（模块 docstring 首行）
与用例说明（函数 docstring 首行），配合 pytest -v 的 PASSED/FAILED，
让一次运行即可看清覆盖了哪些业务、各业务有哪些用例及结果。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.service import MeterService
from app.storage import MeterStorage

_CURRENT_MODULE: dict[str, str] = {}


@pytest.fixture
def storage(tmp_path):
    s = MeterStorage(str(tmp_path / "test.db"))
    s.init_db()
    return s


@pytest.fixture
def service(storage):
    return MeterService(storage)


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "http.db"))
    with TestClient(app) as c:
        yield c


def _module_business(item: pytest.Item) -> str:
    mod = item.module  # type: ignore[attr-defined]
    doc = (mod.__doc__ or "").strip().splitlines()[0] if mod.__doc__ else mod.__name__
    return doc


def pytest_runtest_setup(item: pytest.Item) -> None:
    business = _module_business(item)
    if _CURRENT_MODULE.get("name") != item.module.__name__:
        _CURRENT_MODULE["name"] = item.module.__name__
        print(f"\n────────── {business} ──────────")
    case_doc = ""
    if item.obj.__doc__:  # type: ignore[attr-defined]
        case_doc = item.obj.__doc__.strip().splitlines()[0]  # type: ignore[attr-defined]
    print(f"\n▶ 用例 {item.name}：{case_doc}")
