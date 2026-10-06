"""pytest 配置：让单测日志体现"业务分组 -> 用例 -> 结果"的覆盖情况。"""

from __future__ import annotations

import time

import pytest

# 业务分组：测试类名前缀 -> 业务说明
BUSINESS_GROUPS = {
    "TestStore": "存储与持久化",
    "TestIngest": "读数上报与异常规则",
    "TestUsage": "时间窗口用电量统计",
    "TestConcurrency": "并发与重启幂等",
    "TestApi": "HTTP 端到端流程",
}

_results: list[tuple[str, str, str]] = []


def _business_of(node) -> str:
    return BUSINESS_GROUPS.get(node.cls.__name__, node.cls.__name__) if node.cls else "其他"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        _results.append((_business_of(item), item.name, report.outcome))
        mark = {"passed": "PASS", "failed": "FAIL"}.get(report.outcome, report.outcome.upper())
        doc = (item.function.__doc__ or "").strip().splitlines()[0]
        report.sections.append(
            ("用例结果", f"[{_business_of(item)}] {item.name} - {doc} => {mark}")
        )


def pytest_sessionfinish(session, exitstatus):
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr is None:
        return
    tr.write_sep("=", "业务覆盖与用例结果汇总")
    by_group: dict[str, list] = {}
    for business, name, outcome in _results:
        by_group.setdefault(business, []).append((name, outcome))
    for business, cases in by_group.items():
        passed = sum(1 for _, o in cases if o == "passed")
        tr.write_line(f"业务: {business}（{passed}/{len(cases)} 通过）")
        for name, outcome in cases:
            mark = "✓" if outcome == "passed" else "✗"
            tr.write_line(f"    {mark} {name} [{outcome}]")
    tr.write_sep("=", f"合计 {len(_results)} 个用例")
