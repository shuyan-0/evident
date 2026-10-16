"""
数据源适配器：real(扶摇 REST) / mock(构造数据) / auto(失败自动降级)。

硬约束（来自扶摇官方文档原文）：
> "空列表也可能表示服务降级，不能据此断言期间没有异动。"
因此所有适配器都必须显式返回 DataHealth，绝不允许把"取不到"伪装成"没有"。

真实数据字段已实测核实（2026-10）：
- 个股分时新闻 /api/news/stock/timeline-list → id/title/source/summary/publish_time_ms/jump_url
- 快讯          /api/news/flash/list        → create_time_ms/update_time_ms（双时间戳，可判定被修改）
- 异动解读      /api/news/anomaly/analysis  → first_publish_time/create_time/tag_code_update_time（毫秒）
- 热股榜        /api/a-share/special-data/hot-stock-list → thscode/name/rank/heat
- 行情快照      /api/a-share/prices/snapshot → last_price/price_change_ratio_pct
- 估值          /api/a-share/valuations/snapshot → pe_ttm/pb_mrq
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
TIMEOUT = float(os.getenv("FUYAO_TIMEOUT", "15"))

# 默认关注标的（用户未指定时使用）
DEFAULT_WATCH = os.getenv("EVIDENT_DEFAULT_WATCH", "600519.SH,300033.SZ,002594.SZ")
MAX_WATCH = int(os.getenv("EVIDENT_MAX_WATCH", "6"))

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
    real_record_count: int = 0
    watchlist: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        self.degraded = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _call(path: str, params: dict[str, Any], retries: int = 2) -> tuple[Optional[Any], Optional[str]]:
    """调用扶摇 REST。返回 (data, error)。所有业务错误经 code 分发。

    带有限重试：实测上游偶发瞬时失败（首次请求/网络抖动），
    重试可显著降低误报"接口失败"的概率；重试仍失败则如实上报。
    """
    last_err: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                f"{BASE_URL}{path}",
                params=params,
                headers={"X-api-key": API_KEY},
                timeout=TIMEOUT,
            )
            if r.status_code == 429:
                last_err = "触发限流(HTTP 429)"
            else:
                payload = r.json()
                code = payload.get("code")
                if code == 0:
                    return payload.get("data"), None
                last_err = f"接口返回 code={code}: {payload.get('message')}"
        except requests.Timeout:
            last_err = "请求超时"
        except requests.RequestException as e:
            last_err = f"网络异常: {type(e).__name__}"
        except ValueError:
            last_err = "响应非合法 JSON"
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    return None, last_err


# ---------------------------------------------------------------------------
# 来源层级推断（真实数据没有现成的 tier 标签，需按来源名称与文本判定）
# ---------------------------------------------------------------------------
OFFICIAL_HINTS = ["公告", "交易所", "证监会", "监管", "上交所", "深交所"]
COMPANY_HINTS = ["官微", "官方", "投资者关系", "公司发布", "业绩说明会", "e互动", "互动易"]
MAJOR_MEDIA = [
    "证券时报", "证券日报", "上海证券报", "中国证券报", "第一财经", "21世纪经济报道",
    "新华社", "央视", "人民日报", "经济日报", "财联社", "每日经济新闻", "澎湃", "界面",
    "新浪财经", "华尔街见闻", "e公司",
]
RESEARCH_HINTS = ["研报", "研究所", "证券研究", "机构", "评级", "策略", "首席", "分析师", "券商"]
RUMOR_HINTS = ["传闻", "传", "据传", "市场消息", "小道", "网传", "未证实", "股吧"]


def infer_tier(source_name: str, title: str, content: str) -> str:
    """按来源名称与文本推断来源层级。"""
    s = source_name or ""
    blob = f"{title}{content}"
    if any(h in s for h in COMPANY_HINTS):
        return "company"
    if any(h in s for h in OFFICIAL_HINTS):
        return "official"
    if any(h in s for h in MAJOR_MEDIA):
        return "media_major"
    # 研报类通常出现在标题里（"机构建议…"）
    if any(h in s for h in RESEARCH_HINTS) or any(h in blob for h in ["机构建议", "研报", "评级"]):
        return "research"
    if any(h in s for h in RUMOR_HINTS) or any(h in blob for h in RUMOR_HINTS):
        return "unattributed"
    return "media_general"


def infer_claim(title: str, content: str, tier: str) -> str:
    """推断声明类型：事实/观点/推测/传闻。"""
    blob = f"{title}{content}"
    if tier in ("official", "company"):
        return "fact"
    if any(h in blob for h in RUMOR_HINTS):
        return "rumor"
    if any(h in blob for h in ["建议", "观点", "认为", "预计", "展望", "看好", "关注", "评级", "复盘"]):
        return "opinion"
    if any(h in blob for h in ["或", "有望", "可能", "将", "拟"]):
        return "speculation"
    return "fact"


def load_mock() -> list[dict]:
    """加载构造数据（严格遵循官方字段契约）。"""
    with open(MOCK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_watchlist(preferred: list[str]) -> list[str]:
    if preferred:
        return preferred[:MAX_WATCH]
    return [w.strip() for w in DEFAULT_WATCH.split(",") if w.strip()][:MAX_WATCH]


def fetch_real(health: DataHealth, watchlist: list[str] | None = None) -> list[dict]:
    """从扶摇真实接口拉取：个股新闻(主) + 快讯 + 异动解读(辅)。"""
    records: list[dict] = []
    now_ms = int(time.time() * 1000)
    watch = _resolve_watchlist(watchlist or [])
    health.watchlist = watch

    # ---- 1) 个股分时新闻（主数据源：有来源 + 发布时间 + 关联标的）----
    for thscode in watch:
        data, err = _call("/api/news/stock/timeline-list",
                          {"thscode": thscode, "offset": "first", "size": 40})
        if err:
            health.sources[f"新闻:{thscode}"] = "failed"
            health.warn(f"个股分时新闻({thscode})拉取失败：{err}")
            continue
        items = (data or {}).get("items") or []
        health.sources[f"新闻:{thscode}"] = "ok" if items else "empty"
        if not items:
            health.warn(f"个股分时新闻({thscode})返回空列表——可能是服务降级，不代表该标的无新闻")
        for it in items:
            title = it.get("title") or ""
            content = it.get("summary") or title
            src = it.get("source") or it.get("author") or "未知来源"
            tier = infer_tier(src, title, content)
            records.append({
                "raw_id": f"timeline:{it.get('id')}",
                "channel": tier,
                "title": title,
                "content": content,
                "source_name": src,
                "source_url": it.get("jump_url") or "",
                # 新闻无独立更新时间字段，以发布时间为准
                "publish_time_ms": it.get("publish_time_ms"),
                "update_time_ms": it.get("publish_time_ms"),
                "instruments": [thscode],
                "fetched_at_ms": now_ms,
                "hint_source_tier": tier,
                "hint_claim_type": infer_claim(title, content, tier),
                "data_origin": "个股新闻",
            })

    # ---- 2) 快讯（含 create/update 双时间戳 → 可判定"发布后被修改"）----
    data, err = _call("/api/news/flash/list", {"tag_id": 21103, "seq": "0"})
    if err:
        health.sources["快讯"] = "failed"
        health.warn(f"快讯拉取失败：{err}")
    else:
        items = (data or {}).get("items") or []
        health.sources["快讯"] = "ok" if items else "empty"
        if not items:
            health.warn("快讯返回空列表——可能是服务降级")
        for it in items:
            title = it.get("title") or ""
            content = it.get("summary") or title
            src = "同花顺快讯"
            tier = infer_tier(src, title, content)
            create_ms = it.get("create_time_ms")
            update_ms = it.get("update_time_ms")
            records.append({
                "raw_id": f"flash:{it.get('id')}",
                "channel": tier,
                "title": title,
                "content": content,
                "source_name": src,
                "source_url": it.get("encoded_url") or "",
                "publish_time_ms": create_ms,
                "update_time_ms": update_ms,
                "instruments": [],
                "fetched_at_ms": now_ms,
                "hint_source_tier": tier,
                "hint_claim_type": infer_claim(title, content, tier),
                "data_origin": "快讯",
            })

    # ---- 3) 异动解读（市场级事件；仅取 A 股域）----
    today = time.strftime("%Y-%m-%d")
    begin = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
    data, err = _call("/api/news/anomaly/analysis", {"begin_date": begin, "end_date": today})
    if err:
        health.sources["异动解读"] = "failed"
        health.warn(f"异动解读拉取失败：{err}")
    else:
        items = (data or {}).get("items") or []
        a_items = [x for x in items if x.get("domain") == "A_SHARES"]
        health.sources["异动解读"] = "ok" if a_items else "empty"
        if (data or {}).get("was_truncated"):
            health.truncated = True
            health.warn("异动解读响应被上游截断(was_truncated=true)，结果不完整")
        if not a_items:
            health.warn("异动解读(A股域)返回空列表——可能是服务降级，不能据此断言期间无异动")
        for it in a_items:
            kws = it.get("key_word_list") or []
            tag = it.get("tag_name") or "异动"
            # 用关键词构造标题：异动解读原文标题高度相似（都是"大涨/大跌"），
            # 直接当标题会让所有解读互相误归并，因此改用关键词增强区分度。
            title = f"异动解读（{tag}）：{'、'.join(kws[:3])}" if kws else f"异动解读（{tag}）"
            content = (it.get("analysis_content") or "")[:1200]
            records.append({
                "raw_id": f"anomaly:{it.get('analysis_id')}",
                "channel": "research",
                "title": title,
                "content": content,
                "source_name": "同花顺异动解读",
                "source_url": it.get("link_url") or "",
                "publish_time_ms": it.get("first_publish_time"),
                "update_time_ms": it.get("tag_code_update_time"),
                "instruments": [],
                "fetched_at_ms": now_ms,
                "hint_source_tier": "research",
                "hint_claim_type": "opinion",
                "keywords": kws,
                "data_origin": "异动解读",
            })

    # ---- 按数据源分别限量，避免某一类数据淹没其他来源 ----
    def _quota(origin: str, n: int) -> list[dict]:
        sub = [r for r in records if r.get("data_origin") == origin and r.get("publish_time_ms")]
        sub.sort(key=lambda r: r["publish_time_ms"], reverse=True)
        return sub[:n]

    quota_map = {
        "个股新闻": int(os.getenv("EVIDENT_QUOTA_NEWS_PER_STOCK", "25")) * max(len(watch), 1),
        "快讯": int(os.getenv("EVIDENT_QUOTA_FLASH", "30")),
        "异动解读": int(os.getenv("EVIDENT_QUOTA_ANOMALY", "25")),
    }
    kept: list[dict] = []
    for origin, n in quota_map.items():
        kept.extend(_quota(origin, n))

    kept.sort(key=lambda r: r["publish_time_ms"] or 0, reverse=True)
    dropped = len([r for r in records if r.get("publish_time_ms")]) - len(kept)
    if dropped > 0:
        health.warn(f"按数据源配额截取，略过 {dropped} 条较旧或超配额记录（各源配额：{quota_map}）")
        health.truncated = True
    records = kept

    health.real_record_count = len(records)
    if not records:
        health.warn("真实数据源未返回任何可用记录")
    return records


def fetch_market_context(watchlist: list[str]) -> dict[str, Any]:
    """补充行情/估值上下文（用于展示标的与事件的关系，不做涨跌预测）。"""
    ctx: dict[str, Any] = {"quotes": {}, "valuations": {}, "errors": []}
    if not watchlist:
        return ctx
    codes = ",".join(watchlist)
    d, err = _call("/api/a-share/prices/snapshot", {"thscodes": codes})
    if err:
        ctx["errors"].append(f"行情快照失败：{err}")
    else:
        for it in (d or {}).get("item") or []:
            ctx["quotes"][it.get("thscode")] = {
                "last_price": it.get("last_price"),
                "change_pct": it.get("price_change_ratio_pct"),
                "turnover": it.get("turnover"),
            }
    d2, err2 = _call("/api/a-share/valuations/snapshot", {"thscodes": codes})
    if err2:
        ctx["errors"].append(f"估值快照失败：{err2}")
    else:
        for it in (d2 or {}).get("item") or []:
            ctx["valuations"][it.get("thscode")] = {
                "name": it.get("name"),
                "pe_ttm": it.get("pe_ttm"),
                "pb_mrq": it.get("pb_mrq"),
            }
    return ctx


def fetch_records(watchlist: list[str] | None = None) -> tuple[list[dict], DataHealth]:
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

        records = fetch_real(health, watchlist)
        if records:
            health.mode = "real"
            health.ok = True
            return records, health

        if mode == "auto":
            health.mode = "mock"
            health.warn("真实数据源不可用，已自动降级为构造数据模式（mock）")
            return load_mock(), health

        health.ok = False
        return [], health

    health.ok = False
    health.warn(f"未知 EVIDENT_DATA_MODE={mode}")
    return [], health
