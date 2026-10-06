"""服务入口：uv run python main.py  或  uvicorn main:app"""

import os

import uvicorn

from app.api import create_app

DB_PATH = os.environ.get("METER_DB", "data/meter.db")

app = create_app(DB_PATH)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
