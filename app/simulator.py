"""模拟设备读数上报，跑通：注册设备 -> 上报（含各类异常）-> 统计 -> 查询。

设备 meter-A（capacity=1000, max_delta=900）轨迹：
  10:00 100 (FIRST) → 11:00 130 → 12:00 130(重复提交一次) → 14:00 300（12~14 离线空档）
  → 15:00 990 → 16:00 20（回绕）→ 17:00 5（重启清零，RESET，不计增量）
  → 18:00 40；另发一条倒序读数演示拒绝。
设备 meter-B：用于演示多设备并发；并对同一读数做并发重复提交验证幂等。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _post(base_url: str, path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(base_url: str, path: str) -> tuple[int, dict | list]:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def run_demo(base_url: str = "http://127.0.0.1:8000") -> dict:
    log: list[str] = []

    def say(line: str) -> None:
        log.append(line)
        print(line)

    say("=== 1. 注册设备 ===")
    for dev, cap, md in (("meter-A", 1000.0, 900.0), ("meter-B", 1000.0, 900.0)):
        code, body = _post(base_url, "/devices",
                           {"device_id": dev, "capacity": cap, "max_delta": md})
        say(f"注册 {dev}: HTTP {code} -> {body}")

    say("\n=== 2. meter-A 上报（正常/重复/离线空档/回绕/重启）===")
    a_track = [
        ("2026-10-07T10:00:00Z", 100.0),
        ("2026-10-07T11:00:00Z", 130.0),
        ("2026-10-07T12:00:00Z", 130.0),  # 值与上点相同，delta=0
        ("2026-10-07T14:00:00Z", 300.0),  # 12:00~14:00 离线，只有一条读数
        ("2026-10-07T15:00:00Z", 990.0),
        ("2026-10-07T16:00:00Z", 20.0),   # 回绕：(1000-990)+20=30
        ("2026-10-07T17:00:00Z", 5.0),    # 重启清零，RESET，增量不可知
        ("2026-10-07T18:00:00Z", 40.0),
    ]
    for ts, v in a_track:
        code, body = _post(base_url, "/readings",
                           {"device_id": "meter-A", "timestamp": ts, "value": v})
        say(f"上报 {ts} value={v:g}: HTTP {code} -> "
            f"status={body.get('status')} flag={body.get('delta_flag')} "
            f"delta={body.get('delta')} reason={body.get('reason')}")

    # 完全重复的上报（幂等）
    code, body = _post(base_url, "/readings",
                       {"device_id": "meter-A", "timestamp": "2026-10-07T11:00:00Z",
                        "value": 130.0})
    say(f"重复上报 11:00/130: HTTP {code} -> status={body.get('status')} "
        f"delta={body.get('delta')}（幂等，不重复计入）")

    # 倒序上报 -> 拒绝
    code, body = _post(base_url, "/readings",
                       {"device_id": "meter-A", "timestamp": "2026-10-07T09:00:00Z",
                        "value": 90.0})
    say(f"倒序上报 09:00/90: HTTP {code} -> {body.get('error', {}).get('code')}："
        f"{body.get('error', {}).get('message')}")

    say("\n=== 3. meter-B 正常上报 ===")
    for ts, v in [("2026-10-07T10:00:00Z", 0.0),
                  ("2026-10-07T11:00:00Z", 25.0),
                  ("2026-10-07T12:00:00Z", 65.0)]:
        code, body = _post(base_url, "/readings",
                           {"device_id": "meter-B", "timestamp": ts, "value": v})
        say(f"上报 {ts} value={v:g}: status={body.get('status')} "
            f"flag={body.get('delta_flag')} delta={body.get('delta')}")

    say("\n=== 4. 统计查询（整天窗口）===")
    win = "start=2026-10-07T00:00:00Z&end=2026-10-08T00:00:00Z"
    for dev in ("meter-A", "meter-B"):
        code, st = _get(base_url, f"/devices/{dev}/statistics?{win}")
        say(f"{dev} 统计: HTTP {code} -> 电量={st['energy']} "
            f"计入段数={st['segment_count']} RESET空档={st['reset_gaps']} "
            f"区间=[{st['first_reading_ts']} .. {st['last_reading_ts']}]")

    say("\n=== 5. 子窗口查询（离线空档不产生电量；跨 RESET 边界正确）===")
    code, st = _get(base_url, "/devices/meter-A/statistics"
                              "?start=2026-10-07T12:30:00Z&end=2026-10-07T13:30:00Z")
    say(f"离线空档窗口 12:30~13:30: 电量={st['energy']}（无读数=0）")

    code, rows = _get(base_url, "/devices/meter-A/readings")
    say(f"meter-A 读数明细共 {len(rows)} 条（重复上报不会多出行）")

    return {"log": log}
