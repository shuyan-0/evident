"""
数据源适配器：real(扶摇 REST) / mock(构造数据) / auto(失败自动降级)。

硬约束（来自扶摇官方文档原文）：
> "空列表也可能表示服务降级，不能据此断言期间没有异动。"
因此所有适配器都必须显式返回 DataHealth，绝不允许把"取不到"伪装成"没有"。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import requests

BASE_URL = os.getenv("FUYAO_BASE_URL", "https://fuyao.aicubes.cn")
API_KEY = os.getenv("FUYAO_API_KEY", "")
DATA_MODE = os.getenv("EVIDENT_DATA_MODE", "auto")  # real | mock | auto
TIMEOUT = float(os.getenv("FUYAO_TIMEOUT", "8"))

MOCK_PATH = Path(__file__).resolve().parent.parent / "data" / "mock_records.json"


@dataclass
class DataHealth:
    """数据健康度——题目要求「调用失败时不得静默生成正常结论」。"""

    mode: str = "mock"
    ok: bool = True
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    sources: dict[str, str] = field(default_factory=dict)  # 接口 -> ok/empty/failed
    fetched_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        self.degraded = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _call(path: str, params: dict[str, Any]) -> tuple[Optional[dict], Optional[str]]:
    """调用扶摇 REST。返回 (data, error)。所有业务错误经 code 分发。"""
    try:
        r = requests.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"X-api-key": API_KEY},
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            return None, "触发限流(HTTP 429)"
        payload = r.json()
        code = payload.get("code")
        if code != 0:
            return None, f"接口返回 code={code}: {payload.get('message')}"
        return payload.get("data"), None
    except requests.Timeout:
        return None, "请求超时"
    except requests.RequestException as e:
        return None, f"网络异常: {type(e).__name__}"
    except ValueError:
        return None, "响应非合法 JSON"


def load_mock() -> list[dict]:
    """加载构造数据（严格遵循官方字段契约）。"""
    with open(MOCK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_real(health: DataHealth) -> list[dict]:
    """从扶摇真实接口拉取快讯/个股分时新闻/异动解读。"""
    records: list[dict] = []
    now_ms = int(time.time() * 1000)

    # 1) 个股分时新闻（已知标的路径）
    for thscode in ("300033.SZ", "600519.SH", "002594.SZ"):
        data, err = _call("/api/news/stock/timeline-list", {"thscode": thscode, "offset": "first", "size": 30})
        if err:
            health.sources[f"stock_timeline:{thscode}"] = "failed"
            health.warn(f"个股分时新闻({thscode})拉取失败：{err}")
            continue
        items = (data or {}).get("items") or []
        health.sources[f"stock_timeline:{thscode}"] = "ok" if items else "empty"
        if not items:
            health.warn(f"个股分时新闻({thscode})返回空列表——可能是服务降级，不代表期间无新闻")
        for it in items:
            records.append({
                "raw_id": f"timeline:{it.get('id')}",
                "channel": "stock_timeline",
                "title": it.get("title") or "",
                "content": it.get("summary") or it.get("title") or "",
                "source_name": it.get("source") or it.get("author") or "同花顺资讯",
                "source_url": it.get("jump_url") or "",
                "publish_time_ms": it.get("publish_time_ms"),
                "update_time_ms": it.get("publish_time_ms"),
                "instruments": [thscode],
                "fetched_at_ms": now_ms,
            })

    # 2) 异动解读（发现路径，含 first_publish_time / tag_code_update_time）
    today = time.strftime("%Y-%m-%d")
    begin = time.strftime("%Y-%m-%d", time.localtime(time.time() - 10 * 86400))
    data, err = _call("/api/news/anomaly/analysis", {"begin_date": begin, "end_date": today})
    if err:
        health.sources["anomaly_analysis"] = "failed"
        health.warn(f"异动解读拉取失败：{err}")
    else:
        items = (data or {}).get("items") or []
        health.sources["anomaly_analysis"] = "ok" if items else "empty"
        if (data or {}).get("was_truncated"):
            health.truncated = True
            health.warn("异动解读响应被上游截断(was_truncated=true)，结果不完整")
        if not items:
            health.warn("异动解读返回空列表——可能是服务降级，不能据此断言期间无异动")
        for it in items:
            records.append({
                "raw_id": f"anomaly:{it.get('analysis_id')}",
                "channel": "anomaly",
                "title": it.get("tag_name") or "异动解读",
                "content": it.get("analysis_content") or "",
                "source_name": "同花顺异动解读",
                "source_url": it.get("link_url") or "",
                "publish_time_ms": it.get("first_publish_time"),
                "update_time_ms": it.get("tag_code_update_time"),
                "instruments": [it.get("tag_code")] if it.get("tag_code") else [],
                "fetched_at_ms": now_ms,
                "anomaly_type": it.get("anomaly_type"),
                "keywords": it.get("key_word_list") or [],
            })

    if not records:
        health.warn("真实数据源未返回任何可用记录")
    return records


def fetch_records() -> tuple[list[dict], DataHealth]:
    """统一入口：按 DATA_MODE 决定取数路径，auto 模式失败自动降级。"""
    mode = DATA_MODE
    health = DataHealth(mode=mode)

    if mode == "mock":
        health.mode = "mock"
        return load_mock(), health

    if mode in ("real", "auto"):
        if not API_KEY:
            if mode == "real":
                health.ok = False
                health.warn("EVIDENT_DATA_MODE=real 但未配置 FUYAO_API_KEY，无法取数")
                return [], health
            health.mode = "mock"
            health.warn("未配置 FUYAO_API_KEY，已自动降级为构造数据模式（mock）")
            return load_mock(), health

        records = fetch_real(health)
        if records:
            health.mode = "real"
            health.ok = True
            return records, health

        # 真实源无可信数据 -> 降级
        if mode == "auto":
            health.mode = "mock"
            health.warn("真实数据源不可用，已自动降级为构造数据模式（mock）")
            return load_mock(), health

        health.ok = False
        return [], health

    health.ok = False
    health.warn(f"未知 EVIDENT_DATA_MODE={mode}")
    return [], health
