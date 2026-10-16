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
from typing import Any, Optional

try:
    import jieba as _jieba_mod

    _jieba_mod.setLogLevel(60)  # 关闭初始化日志
    _JIEBA: Any = _jieba_mod
except Exception:  # pragma: no cover - 环境缺少 jieba 时降级
    _JIEBA = None

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


def _explicit_kw(atom: EvidenceAtom) -> set[str]:
    """上游/人工标注的显式主题词（如 key_word_list、mock 的 keywords）。

    这类标注可信度高于正文抽取，因此单独作为归并的强证据。
    """
    s: set[str] = set()
    for k in atom.keywords or []:
        s |= _tokens(str(k))
    return s


def _kw_set(atom: EvidenceAtom) -> set[str]:
    """原子用于归并的有效实体集合（已过滤停用词）。"""
    return _explicit_kw(atom) | _tokens(atom.title + " " + atom.content)


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

    规则（按可信度从高到低）：
    1. 显式主题词（上游 key_word_list / 标注 keywords）有重合 -> 相容；
    2. 共享任一主题锚点 -> 相容；
    3. 一方无明确主题（__generic__）-> 相容（便于"传闻->澄清"链条归并）；
    4. 两个都命中明确锚点且完全不重叠 -> 不同事件。
    """
    if _explicit_kw(a) & _explicit_kw(b):
        return True
    aa, ab = _anchors(a), _anchors(b)
    if "__generic__" in aa or "__generic__" in ab:
        return True
    return bool(aa & ab)


# 归并门槛：两条记录至少共享这么多个"有效实体"才考虑合并。
# 真实财经新闻里，同标的下常有大量互不相关的事件，仅凭同标的+通用词
# （如"茅台""白酒"）会把不同事件错误合并，因此必须要求足够的实体重合。
MIN_SHARED_ENTITIES = 2


def _title_kw(atom: EvidenceAtom) -> set[str]:
    """标题实体。标题信息密度最高、噪声最小，是归并的主要依据。

    为何不只用全文：正文里公司的拆分词（"贵州"/"茅台"）会同时出现在
    大量互不相关的新闻中，仅凭这些共同词会把不同事件错误合并。
    """
    return _tokens(atom.title)


def _similarity(a: EvidenceAtom, b: EvidenceAtom) -> float:
    """归并相似度——显式可解释，返回 0~1。"""
    if not _share_anchor(a, b):
        return 0.0

    ta, tb = _title_kw(a), _title_kw(b)
    ka, kb = _kw_set(a), _kw_set(b)
    if not ka or not kb:
        return 0.0

    inter_t = ta & tb          # 共享标题实体
    inter = ka & kb            # 共享全文实体
    explicit = _explicit_kw(a) & _explicit_kw(b)   # 共享显式主题词

    # 官方/公司口径是事件的"权威锚"，但其措辞常与媒体报道差异极大
    # （如媒体说"海外订单"、公告写"重大合同"），标题层面几乎无共同词。
    # 因此对这类记录放宽门槛：只要同标的 + 共享主题锚点即可归并。
    authoritative = (
        a.source_tier in (SourceTier.OFFICIAL, SourceTier.COMPANY)
        or b.source_tier in (SourceTier.OFFICIAL, SourceTier.COMPANY)
    )
    same_inst = bool(set(a.instruments) & set(b.instruments))
    anchor_overlap = _anchors(a) & _anchors(b)
    authoritative_link = authoritative and same_inst and bool(anchor_overlap)

    # 门槛：这是一条"防过合并"的关键闸门。
    # - 常规：标题实体与全文实体都要共享至少 MIN_SHARED_ENTITIES 个；
    # - 上游显式主题词重合（可信标注）或权威口径关联时可放宽。
    if not explicit and not authoritative_link:
        if len(inter_t) < MIN_SHARED_ENTITIES or len(inter) < MIN_SHARED_ENTITIES:
            return 0.0

    score = 0.0
    # 1) 标的重合：有利证据，但单独不足以归并
    if a.instruments and b.instruments:
        if same_inst:
            score += 0.25
        else:
            return 0.0  # 都有标的且不重叠 -> 不同事件

    # 2) 显式主题词重合（强证据）
    if explicit:
        score += 0.25

    # 3) 权威口径关联（官方公告 + 同标的 + 同主题）
    if authoritative_link:
        score += 0.22

    # 4) 标题实体重合度（主要依据）
    jt = len(inter_t) / len(ta | tb) if (ta | tb) else 0.0
    score += min(jt * 2.0, 0.45)

    # 5) 全文实体重合度（辅助）
    jb = len(inter) / len(ka | kb) if (ka | kb) else 0.0
    score += min(jb * 1.2, 0.25)

    # 6) 共享标题实体数量的加成
    score += min(len(inter_t) * 0.06, 0.20)

    return max(0.0, min(score, 1.0))


# 停用词：金融新闻中的高频通用词。不过滤会导致"下跌/板块/公司"这类词
# 把毫无关系的内容误归并为同一事件（过合并）。
STOPWORDS = {
    # 涨跌与盘面
    "下跌", "上涨", "大涨", "大跌", "涨停", "跌停", "炸板", "走强", "走弱", "回落", "反弹",
    "异动", "解读", "板块", "概念", "行业", "题材", "个股", "股票", "股价", "成交", "换手",
    "指数", "市场", "大盘", "沪深", "两市", "A股", "港股", "美股", "a股",
    # 主体与通用名
    "公司", "股份", "集团", "有限", "上市公司", "企业", "机构", "券商", "分析师", "投资者",
    "资金", "主力", "融资", "北向", "南向", "净买入", "净卖出", "评级", "研报", "观点",
    # 叙事功能词
    "消息", "新闻", "快讯", "报道", "表示", "认为", "预计", "有望", "可能", "将", "拟",
    "宣布", "发布", "披露", "获悉", "今日", "昨日", "月内", "年内", "近期", "目前",
    "以来", "同比", "环比", "持续", "进一", "以及", "此外", "同时", "值得", "关注",
    # 单位与量词
    "亿元", "万元", "万股", "亿", "万", "元", "股", "同比增", "增长", "下降", "提升",
    # 异动解读的通用措辞（否则不同行业的解读会因这些套话互相误归并）
    "涨幅", "较大", "此前", "局部", "承压", "走弱", "影响", "业务", "板块下跌",
}


def _is_meaningful(tok: str) -> bool:
    """判断一个片段是否具备区分度。"""
    if len(tok) < 2:
        return False
    if tok in STOPWORDS:
        return False
    if tok.isdigit():
        return False
    # 纯数字+单位的组合无区分度
    if re.fullmatch(r"[\d\.]+[%亿万]", tok):
        return False
    return True


def _tokens(text: str) -> set[str]:
    """抽取有区分度的实体词。

    用 jieba 分词（而非滑动 n-gram）：
    - n-gram 会切出大量重叠片段（"异动解读（大跌）" → "异动解读""动解读（"…），
      既无法被停用词精确命中，又会让无关内容因噪声片段而误归并。
    - 分词后"异动/解读/大跌"都会成为独立词并被停用词过滤，实体匹配显著更准。
    """
    t = _norm(text)
    if _JIEBA is not None:
        words = _JIEBA.lcut(t)
    else:  # 兜底：无 jieba 时退回 n-gram
        for sw in STOPWORDS:
            t = t.replace(sw, " ")
        words = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,6}", t)
    return {w for w in words if _is_meaningful(w)}


# 归并时间窗：与簇代表点相隔超过此值就不再比较
WINDOW_MS = 7 * 86400 * 1000


def resolve_events(atoms: list[EvidenceAtom], threshold: float = 0.42) -> list[Event]:
    """事件归并：把描述同一事件的多条证据聚成一簇。

    采用**代表点聚类**而非并查集连通：
    并查集存在"链式传递"缺陷——A~B 相似、B~C 相似，即使 A 与 C 毫不相关
    也会被连成一簇，导致事件无限膨胀（实测会把数十条无关新闻并成一条）。
    代表点聚类要求新记录与簇的**代表记录**（最早的那条，通常即"最初来源"）
    达到阈值才归入，从机制上杜绝链式膨胀。
    """
    order = sorted(range(len(atoms)), key=lambda i: atoms[i].disclose_time or 0)
    clusters: list[dict[str, Any]] = []

    for idx in order:
        atom = atoms[idx]
        best_c: Optional[dict[str, Any]] = None
        best_s = 0.0
        for c in clusters:
            rep = c["rep"]
            t_rep = rep.disclose_time or 0
            t_atom = atom.disclose_time or 0
            if abs(t_atom - t_rep) > WINDOW_MS:
                continue
            s = _similarity(rep, atom)
            if s > best_s:
                best_s, best_c = s, c
        if best_c is not None and best_s >= threshold:
            best_c["members"].append(atom)
        else:
            clusters.append({"rep": atom, "members": [atom]})

    events: list[Event] = []
    for c in clusters:
        members = sorted(c["members"], key=lambda a: a.disclose_time or 0)
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
                f"初始来源={origin.source_name}({TIER_LABEL[origin.source_tier]})；"
                f"聚类方式=代表点（阈值{threshold}）"
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
    """把事件内的证据按主题锚点拆成子线索。

    用**共享锚点连通**（并查集）分组，而不是取单个锚点做 key。
    原因：一条记录常同时命中多个锚点（如"控股股东增持…不涉及控制权变更"
    同时命中 {股东增减持, 收购要约}）。若只取其中一个作 key，会把本属同一
    线索的记录拆散，导致更正/否认无法在同一线索内生效。
    """
    n = len(atoms)
    if n == 0:
        return {}
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

    anchor_sets = [_anchors(a) for a in atoms]
    for i in range(n):
        for j in range(i + 1, n):
            ai, aj = anchor_sets[i], anchor_sets[j]
            if "__generic__" in ai or "__generic__" in aj:
                continue  # 无明确主题的记录不参与跨线索合并
            if ai & aj:
                union(i, j)

    threads: dict[str, list[EvidenceAtom]] = defaultdict(list)
    for i in range(n):
        root = find(i)
        if not threads:
            threads[str(root)] = []
        threads[str(root)].append(atoms[i])
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
