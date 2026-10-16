"""Evident —— 投资事件证据时间线 API 服务。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .adapters import DATA_MODE, fetch_market_context, fetch_records
from .pipeline import build_dashboard

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Evident", version="1.1.0", description="投资事件情报与证据时间线")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "data_mode_env": DATA_MODE,
        "has_api_key": bool(os.getenv("FUYAO_API_KEY")),
    }


@app.get("/api/dashboard")
def dashboard(watch: str = Query("", description="逗号分隔的关注标的，如 600519.SH,300033.SZ")) -> JSONResponse:
    """主链路：取数 -> 原子化 -> 事件归并 -> 冲突/状态仲裁 -> 面板数据。

    数据缺失/失败/降级时，health 中会显式标注，绝不静默生成"正常"结论。
    """
    watch_list = [w.strip() for w in watch.split(",") if w.strip()]

    records, health_obj = fetch_records(watch_list)
    payload = build_dashboard(records, health_obj)

    effective_watch = health_obj.watchlist or watch_list
    if effective_watch:
        for ev in payload["events"]:
            ev["watched"] = bool(set(ev["instruments"]) & set(effective_watch))
        payload["events"].sort(key=lambda e: (not e.get("watched"),))

    # 行情/估值上下文（仅作标的关联展示，不构成涨跌预测或建议）
    ctx = fetch_market_context(health_obj.watchlist) if health_obj.mode == "real" else {"quotes": {}, "valuations": {}, "errors": []}
    payload["market"] = ctx
    if ctx.get("errors"):
        for e in ctx["errors"]:
            payload["health"]["warnings"].append(e)
        payload["health"]["degraded"] = True

    return JSONResponse(payload)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
