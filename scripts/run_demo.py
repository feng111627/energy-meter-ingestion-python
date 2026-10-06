"""一键演示：在临时端口启动服务 -> 用模拟设备跑通 上报/统计/查询 全流程。

用法：uv run python scripts/run_demo.py
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import create_app  # noqa: E402
from app.simulator import run_demo  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    db_file = os.path.join(tempfile.mkdtemp(prefix="meter-demo-"), "demo.db")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(create_app(db_file), host="127.0.0.1",
                            port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import urllib.request
    for _ in range(100):
        try:
            urllib.request.urlopen(base_url + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)

    try:
        run_demo(base_url)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
