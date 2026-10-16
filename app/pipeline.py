"""
核心流水线：Atomize -> Resolve(事件归并) -> Adjudicate(冲突/状态机)。

这是产品的算法心脏。设计取舍：
- 归并采用【规则优先 + 可解释】策略，每个归并决策都输出依据(resolve_basis)，
  而不是黑盒相似度，以保证"核心结论必须能回到原始字段"。
- 状态由【全量证据按权重仲裁】得出，不取最后一条。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .model import (
    CLAIM_LABEL,
    CLAIM_WEIGHT,
    SOURCE_WEIGHT,
    TIER_LABEL,
    ClaimType,
    Event,
    EventStatus,
    EvidenceAtom,
    SourceTier,
    make_id,
)

# 传闻线索词 / 否认线索词 / 更正线索词——用于弱监督分类
RUMOR_CUES = ["传闻", "传", "市场消息", "据传", "流传", "未获确认", "未见官方", "小道"]
DENY_CUES = ["不存在", "否认", "澄清", "无此事", "未筹划", "不涉及", "未有应披露"]
CORRECT_CUES = ["更正", "更正为", "勘误", "修正", "口径有误", "统计有误"]
OPINION_CUES = ["分析师", "观点", "认为", "预计", "展望", "评级", "估值"]
SPECULATION_CUES = ["或", "有望", "预计将", "可能", "据悉将"]
OFFICIAL_CHANNELS = {"official"}
COMPANY_CHANNELS = {"company"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def _kw_set(rec: dict) -> set[str]:
    """从标题+正文+显式关键词中抽取用于归并的关键词。"""
    kws: set[str] = set(rec.get("keywords") or [])
    text = _norm(rec.get("title", "")) + _norm(rec.get("content", ""))
    # 抽取 2-6 字的中文片段作为粗特征，配合显式关键词使用
    for m in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,6}", text):
        kws.add(m.lower())
    return kws


def classify_claim(rec: dict) -> ClaimType:
    """声明类型分类：显式 hint 优先，其次线索词规则。"""
    if rec.get("hint_claim_type"):
        try:
            return ClaimType(rec["hint_claim_type"])
        except ValueError:
            pass

    ch = rec.get("channel", "")
    if ch in OFFICIAL_CHANNELS or ch in COMPANY_CHANNELS:
        return ClaimType.FACT

    blob = _norm(rec.get("title", "")) + _norm(rec.get("content", ""))
    if any(c in blob for c in RUMOR_CUES):
        return ClaimType.RUMOR
    if any(c in blob for c in OPINION_CUES):
        return ClaimType.OPINION
    if any(c in blob for c in SPECULATION_CUES):
        return ClaimType.SPECULATION
    return ClaimType.FACT


def classify_tier(rec: dict) -> SourceTier:
    """来源层级分类：显式 hint 优先，其次按渠道映射。"""
    if rec.get("hint_source_tier"):
        try:
            return SourceTier(rec["hint_source_tier"])
        except ValueError:
            pass
    mapping = {
        "official": SourceTier.OFFICIAL,
        "company": SourceTier.COMPANY,
        "media_major": SourceTier.MEDIA_MAJOR,
        "media_general": SourceTier.MEDIA_GENERAL,
        "research": SourceTier.RESEARCH,
        "unattributed": SourceTier.UNATTRIBUTED,
    }
    return mapping.get(rec.get("channel", ""), SourceTier.MEDIA_GENERAL)


def detect_polarity(rec: dict, claim: ClaimType) -> str:
    """极性识别：证实 / 否认 / 更正 / 中性。"""
    if rec.get("hint_polarity"):
        return rec["hint_polarity"]
    blob = _norm(rec.get("title", "")) + _norm(rec.get("content", ""))
    if any(c in blob for c in CORRECT_CUES):
        return "correct"
    if any(c in blob for c in DENY_CUES):
        return "deny"
    return "neutral"


def atomize(records: list[dict]) -> list[EvidenceAtom]:
    """原始记录 -> 证据原子。原子一经生成不再修改（保证可溯源）。"""
    atoms: list[EvidenceAtom] = []
    for rec in records:
        claim = classify_claim(rec)
        tier = classify_tier(rec)
        polarity = detect_polarity(rec, claim)

        publish = rec.get("publish_time_ms")
        update = rec.get("update_time_ms")
        # 事件的真实发生时间：这里以最早可得时间近似，缺失则回退披露时间
        event_time = rec.get("event_time_ms") or publish

        atoms.append(
            EvidenceAtom(
                atom_id=make_id("atom", rec.get("raw_id", ""), str(publish)),
                raw_id=rec.get("raw_id", ""),
                title=rec.get("title", ""),
                content=rec.get("content", ""),
                source_name=rec.get("source_name", "未知来源"),
                source_url=rec.get("source_url", ""),
                claim_type=claim,
                source_tier=tier,
                event_time=event_time,
                disclose_time=publish,
                fetch_time=rec.get("fetched_at_ms"),
                update_time=update,
                instruments=list(rec.get("instruments") or []),
                keywords=list(rec.get("keywords") or []),
                polarity=polarity,
            )
        )
    return atoms


# 事件主题锚点：同一标的下的不同事项（收购/增持/订单/财报…）必须区分。
# 没有共享锚点的记录，即使同标的、时间接近，也不应合并。
TOPIC_ANCHORS: dict[str, list[str]] = {
    "收购要约": ["收购", "要约", "并购", "产业资本", "控制权", "股权转让"],
    "股东增减持": ["增持", "减持", "控股股东", "实际控制人", "回购"],
    "重大合同": ["合同", "订单", "供货", "中标", "采购", "亿元"],
    "业绩财务": ["业绩", "营收", "净利润", "财报", "预增", "预亏", "毛利"],
    "产品技术": ["固态电池", "量产", "发布", "技术", "车型", "产品"],
    "监管合规": ["问询", "监管", "处罚", "立案", "违规", "关注函"],
    "澄清回应": ["澄清", "否认", "传闻", "不存在", "未筹划"],
}


def _anchors(atom: EvidenceAtom) -> set[str]:
    """识别一条记录命中的主题锚点集合。"""
    blob = _norm(atom.title) + _norm(atom.content)
    hits = {name for name, kws in TOPIC_ANCHORS.items() if any(k in blob for k in kws)}
    if not hits:
        hits = {"__generic__"}
    return hits


def _share_anchor(a: EvidenceAtom, b: EvidenceAtom) -> bool:
    """主题锚点是否相容。

    规则：共享任一锚点即相容；一方为 __generic__（无明确主题）时视为相容
    （便于"传闻->澄清"这类锚点不同的链条仍能归并）。
    但两个都命中明确锚点且完全不重叠时，判定为不同事件。
    """
    aa, ab = _anchors(a), _anchors(b)
    if "__generic__" in aa or "__generic__" in ab:
        return True
    return bool(aa & ab)


def _similarity(a: EvidenceAtom, b: EvidenceAtom) -> float:
    """归并相似度——显式可解释，返回 0~1。"""
    score = 0.0
    # 1) 标的重合：同一标的是归并的强前提
    if a.instruments and b.instruments:
        inter = set(a.instruments) & set(b.instruments)
        if inter:
            score += 0.45
        else:
            score -= 0.25  # 标的完全不重叠，倾向不合并

    # 2) 主题锚点：不同事项必须切开
    if not _share_anchor(a, b):
        return 0.0

    # 3) 关键词重合（Jaccard on 关键词）
    ka, kb = _kw_set({"keywords": a.keywords}).union(_tokens(a.title + a.content)), \
             _kw_set({"keywords": b.keywords}).union(_tokens(b.title + b.content))
    if ka and kb:
        jac = len(ka & kb) / len(ka | kb)
        score += min(jac * 2.0, 0.55)

    return max(0.0, min(score, 1.0))


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,6}", _norm(text)))


def resolve_events(atoms: list[EvidenceAtom], threshold: float = 0.42) -> list[Event]:
    """事件归并：把描述同一事件的多条证据聚成一簇。

    使用并查集式的贪心聚类。低于阈值的不强合并，标为独立事件。
    """
    n = len(atoms)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    # 时间邻近前置：只在时间窗内做比较，避免无序比较造成误合并
    order = sorted(range(n), key=lambda i: atoms[i].disclose_time or 0)
    for idx_a in range(n):
        i = order[idx_a]
        for idx_b in range(idx_a + 1, n):
            j = order[idx_b]
            ti = atoms[i].disclose_time or 0
            tj = atoms[j].disclose_time or 0
            if tj - ti > 12 * 86400 * 1000:  # 超过 12 天不再比较
                break
            if _similarity(atoms[i], atoms[j]) >= threshold:
                union(i, j)

    clusters: dict[int, list[EvidenceAtom]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(atoms[i])

    events: list[Event] = []
    for members in clusters.values():
        members = sorted(members, key=lambda a: a.disclose_time or 0)
        origin = members[0]
        instruments = sorted({ins for m in members for ins in m.instruments})
        event = Event(
            event_id=make_id("event", origin.raw_id, ",".join(instruments)),
            title=_event_title(members),
            summary=_event_summary(members),
            instruments=instruments,
            atoms=members,
            origin_atom_id=origin.atom_id,
            first_seen=min((m.disclose_time for m in members if m.disclose_time), default=None),
            last_updated=max(
                (m.update_time or m.disclose_time or 0 for m in members), default=None
            ),
            resolve_basis=(
                f"归并依据：标的={','.join(instruments) or '未标注'}；"
                f"时间跨度={_fmt_span(members)}；"
                f"共{len(members)}条原始记录、{len({m.disclose_time for m in members})}个版本；"
                f"初始来源={origin.source_name}({TIER_LABEL[origin.source_tier]})"
            ),
        )
        events.append(event)

    for e in events:
        adjudicate(e)
    return events


def _event_title(members: list[EvidenceAtom]) -> str:
    """事件标题：优先取"事件的起点叙事"，而非单纯权重最高者。

    理由：同一事件常以传闻开篇、以官方口径收尾。若直接用最高权重标题，
    事件会显示成公告标题，丢失"这是一个从传闻演化而来的事件"的信息。
    策略：若存在传闻/推测，取最早的传闻标题；否则取权重最高者。
    """
    weak = [m for m in members if m.claim_type in (ClaimType.RUMOR, ClaimType.SPECULATION)]
    if weak:
        earliest = min(weak, key=lambda a: a.disclose_time or 0)
        return earliest.title
    return max(members, key=lambda a: a.weight).title


def _event_summary(members: list[EvidenceAtom]) -> str:
    top = sorted(members, key=lambda a: a.weight, reverse=True)[:2]
    return " / ".join(a.title for a in top)


def _fmt_span(members: list[EvidenceAtom]) -> str:
    ts = [m.disclose_time for m in members if m.disclose_time]
    if not ts:
        return "未知"
    days = (max(ts) - min(ts)) / 86400000
    return f"{days:.1f}天"


def adjudicate(event: Event) -> None:
    """冲突检测 + 状态机。

    关键设计：状态在【主题子线程】(sub-thread) 内仲裁，而非整簇一把抓。
    原因：同一标的可能同时存在"收购传闻被否认"和"增持公告被更正"两条独立线索，
    若在整簇上仲裁，更正会错误地覆盖否认结论。
    """
    atoms = event.atoms
    if not atoms:
        return

    # ---- 冲突检测（全局，用于展示）----
    conflicts: list[dict] = []
    rumors = [a for a in atoms if a.claim_type == ClaimType.RUMOR]
    denials = [a for a in atoms if a.polarity == "deny"]

    for r in rumors:
        for o in denials:
            if o.weight > r.weight and _share_anchor(r, o):
                conflicts.append({
                    "type": "rumor_vs_denial",
                    "label": "传闻与官方否认冲突",
                    "left": {"atom_id": r.atom_id, "title": r.title, "tier": r.source_tier.value,
                             "weight": r.weight, "claim": r.claim_type.value},
                    "right": {"atom_id": o.atom_id, "title": o.title, "tier": o.source_tier.value,
                              "weight": o.weight, "claim": o.claim_type.value},
                    "resolved_by": f"{TIER_LABEL[o.source_tier]}权重({o.weight}) > {TIER_LABEL[r.source_tier]}权重({r.weight})",
                    "adopted": o.atom_id,
                })

    for i in range(len(atoms)):
        for j in range(i + 1, len(atoms)):
            a, b = atoms[i], atoms[j]
            if a.claim_type == ClaimType.RUMOR or b.claim_type == ClaimType.RUMOR:
                continue
            if not _share_anchor(a, b):
                continue
            if _numeric_conflict(a, b):
                conflicts.append({
                    "type": "numeric_inconsistency",
                    "label": "同一事件的关键数据不一致",
                    "left": {"atom_id": a.atom_id, "title": a.title, "tier": a.source_tier.value,
                             "weight": a.weight, "claim": a.claim_type.value},
                    "right": {"atom_id": b.atom_id, "title": b.title, "tier": b.source_tier.value,
                              "weight": b.weight, "claim": b.claim_type.value},
                    "resolved_by": "以来源层级更高者为采纳侧，低层级记录保留供复核",
                    "adopted": a.atom_id if a.weight >= b.weight else b.atom_id,
                })

    event.conflicts = conflicts

    # ---- 状态机：按主题子线程分别仲裁，再取"最需要关注"的结果作为事件状态 ----
    threads = _group_threads(atoms)
    verdicts: list[tuple[EventStatus, str, float]] = []
    for _, members in threads.items():
        verdicts.append(_thread_verdict(members, event))

    if not verdicts:
        event.status = EventStatus.RUMORED
        event.status_reason = "无可裁定的证据记录。"
        return

    # 优先级：更正 > 否认 > 证实 > 冲突 > 过期 > 传闻中
    prio = {
        EventStatus.DISPUTED: 0,
        EventStatus.DENIED: 1,
        EventStatus.CORRECTED: 2,
        EventStatus.CONFIRMED: 3,
        EventStatus.EXPIRED: 4,
        EventStatus.RUMORED: 5,
    }
    verdicts.sort(key=lambda v: (prio.get(v[0], 9), -v[2]))
    event.status, event.status_reason = verdicts[0][0], verdicts[0][1]
    if len(threads) > 1:
        event.status_reason += f"（本事件含 {len(threads)} 条独立子线索，已分别裁定）"


def _group_threads(atoms: list[EvidenceAtom]) -> dict[str, list[EvidenceAtom]]:
    """把事件内的证据按主题锚点拆成子线索。"""
    threads: dict[str, list[EvidenceAtom]] = defaultdict(list)
    for a in atoms:
        hits = _anchors(a)
        key = sorted(hits)[0] if "__generic__" not in hits else "__generic__"
        threads[key].append(a)
    return threads


def _thread_verdict(members: list[EvidenceAtom], event: Event) -> tuple[EventStatus, str, float]:
    """在单条子线索内做加权仲裁，返回 (状态, 理由, 该线索最高权重)。"""
    top_weight = max((m.weight for m in members), default=0.0)
    official = [m for m in members if m.source_tier in (SourceTier.OFFICIAL, SourceTier.COMPANY)]
    corrections = [m for m in members if m.polarity == "correct"]
    denials = [m for m in members if m.polarity == "deny"]
    affirmations = [m for m in members if m.polarity == "affirm"]
    rumors = [m for m in members if m.claim_type == ClaimType.RUMOR]
    conflicted = any(
        c["adopted"] in {m.atom_id for m in members} for c in event.conflicts
    )

    if corrections:
        latest = max(corrections, key=lambda a: a.disclose_time or 0)
        return (
            EventStatus.CORRECTED,
            f"出现更正记录（{latest.source_name}）：{latest.title[:40]}…"
            f"该更正于 {_ts(latest.update_time)} 更新。",
            top_weight,
        )
    if denials and official:
        d = max(denials, key=lambda a: a.weight)
        return (
            EventStatus.DENIED,
            f"官方口径否认（{TIER_LABEL[d.source_tier]}，权重 {d.weight}）：{d.title[:40]}…"
            + (f"同线索存在 {len(rumors)} 条传闻，已被更高权重来源压制。" if rumors else ""),
            top_weight,
        )
    if affirmations:
        a = max(affirmations, key=lambda a: a.weight)
        return (
            EventStatus.CONFIRMED,
            f"高权重来源证实（{TIER_LABEL[a.source_tier]}，权重 {a.weight}）：{a.title[:40]}…",
            top_weight,
        )
    if conflicted:
        return (EventStatus.DISPUTED, "存在未消解的证据冲突，无法按来源层级压制出唯一结论。", top_weight)

    now = max((m.fetch_time or 0) for m in members)
    last = max((m.update_time or m.disclose_time or 0) for m in members)
    age_days = (now - last) / 86400000 if now and last else 0
    if rumors and age_days > 5:
        return (
            EventStatus.EXPIRED,
            f"传闻至今未见官方口径，距最后更新已 {age_days:.1f} 天，视为已过期。",
            top_weight,
        )
    return (EventStatus.RUMORED, "仅有传闻或中性记录，尚未获得官方口径。", top_weight)


def _numeric_conflict(a: EvidenceAtom, b: EvidenceAtom) -> bool:
    """检测两条记录的数值口径是否互斥（同标的、共享主题词、数字不同）。"""
    if not (set(a.instruments) & set(b.instruments)):
        return False
    na = re.findall(r"\d+(?:\.\d+)?\s*(?:亿|万|%)", a.title + a.content)
    nb = re.findall(r"\d+(?:\.\d+)?\s*(?:亿|万|%)", b.title + b.content)
    if not na or not nb:
        return False
    # 共享主题词（排除纯数字）才比较
    ta = {t for t in _tokens(a.title + a.content) if not t.isdigit()}
    tb = {t for t in _tokens(b.title + b.content) if not t.isdigit()}
    if len(ta & tb) < 2:
        return False
    return set(na) != set(nb)


def _ts(ms: int | None) -> str:
    if not ms:
        return "未知"
    import datetime

    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def build_dashboard(records: list[dict], health: Any) -> dict[str, Any]:
    """完整流水线出口：原始记录 -> 事件面板数据。"""
    atoms = atomize(records)
    events = resolve_events(atoms)

    # 统计
    tier_stats: dict[str, int] = defaultdict(int)
    claim_stats: dict[str, int] = defaultdict(int)
    for a in atoms:
        tier_stats[a.source_tier.value] += 1
        claim_stats[a.claim_type.value] += 1

    status_stats: dict[str, int] = defaultdict(int)
    for e in events:
        status_stats[e.status.value] += 1

    # 标的索引
    inst_map: dict[str, list[str]] = defaultdict(list)
    for e in events:
        for ins in e.instruments:
            inst_map[ins].append(e.event_id)

    return {
        "health": health.to_dict() if hasattr(health, "to_dict") else health,
        "stats": {
            "record_count": len(records),
            "atom_count": len(atoms),
            "event_count": len(events),
            "conflict_count": sum(len(e.conflicts) for e in events),
            "by_tier": dict(tier_stats),
            "by_claim": dict(claim_stats),
            "by_status": dict(status_stats),
            "tier_labels": TIER_LABEL,
            "claim_labels": CLAIM_LABEL,
            "source_weight": {k.value: v for k, v in SOURCE_WEIGHT.items()},
            "claim_weight": {k.value: v for k, v in CLAIM_WEIGHT.items()},
        },
        "events": [e.to_dict() for e in _sort_events(events)],
        "instruments": [
            {"code": code, "event_ids": ids, "event_count": len(ids)}
            for code, ids in sorted(inst_map.items())
        ],
    }


def _sort_events(events: list[Event]) -> list[Event]:
    """排序：有冲突的优先，其次按最近更新倒序——把最需要人看的放最前。"""
    prio = {
        EventStatus.DISPUTED: 0,
        EventStatus.DENIED: 1,
        EventStatus.CORRECTED: 2,
        EventStatus.CONFIRMED: 3,
        EventStatus.RUMORED: 4,
        EventStatus.EXPIRED: 5,
    }
    return sorted(events, key=lambda e: (prio.get(e.status, 9), -(e.last_updated or 0)))
