"""
领域模型：证据原子 / 事件 / 状态机。

设计要点（对应题目要求）：
- claim_type  : 区分【事实 fact / 观点 opinion / 推测 speculation / 传闻 rumor】
- source_tier : 体现【证据权重】的来源层级
- 四个时间戳  : event_time(发生) / disclose_time(披露) / fetch_time(抓取) / update_time(更新)
  其中 disclose_time != update_time 即"该条记录被修改过"的直接证据。
- 事件状态由【全量证据按权重仲裁】得出，而非简单取最后一条。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class ClaimType(str, Enum):
    """声明类型——题目要求「区分事实、观点、推测和传闻」。"""

    FACT = "fact"                # 公告/官方确认的事实
    OPINION = "opinion"          # 研报观点、分析师判断
    SPECULATION = "speculation"  # 推测、合理推断
    RUMOR = "rumor"              # 传闻、未证实消息


class SourceTier(str, Enum):
    """来源层级——决定证据权重（题目要求「体现证据权重」）。"""

    OFFICIAL = "official"            # 交易所/监管/公司正式公告
    COMPANY = "company"              # 公司回应、投资者关系、官方口径
    MEDIA_MAJOR = "media_major"      # 权威财经媒体（一手报道）
    MEDIA_GENERAL = "media_general"  # 一般媒体/转载
    RESEARCH = "research"            # 研报、券商观点
    UNATTRIBUTED = "unattributed"    # 无署名传闻、小道消息


# 来源权重表——显式声明，可审计。
# 数值越高，在冲突仲裁中越占优。
SOURCE_WEIGHT: dict[SourceTier, float] = {
    SourceTier.OFFICIAL: 1.00,
    SourceTier.COMPANY: 0.90,
    SourceTier.MEDIA_MAJOR: 0.70,
    SourceTier.RESEARCH: 0.55,
    SourceTier.MEDIA_GENERAL: 0.40,
    SourceTier.UNATTRIBUTED: 0.20,
}

# 声明类型的可信度折扣——传闻天然弱于事实。
CLAIM_WEIGHT: dict[ClaimType, float] = {
    ClaimType.FACT: 1.00,
    ClaimType.OPINION: 0.60,
    ClaimType.SPECULATION: 0.45,
    ClaimType.RUMOR: 0.30,
}

TIER_LABEL: dict[SourceTier, str] = {
    SourceTier.OFFICIAL: "官方公告",
    SourceTier.COMPANY: "公司回应",
    SourceTier.MEDIA_MAJOR: "权威媒体",
    SourceTier.MEDIA_GENERAL: "一般媒体",
    SourceTier.RESEARCH: "研报观点",
    SourceTier.UNATTRIBUTED: "未署名传闻",
}

CLAIM_LABEL: dict[ClaimType, str] = {
    ClaimType.FACT: "事实",
    ClaimType.OPINION: "观点",
    ClaimType.SPECULATION: "推测",
    ClaimType.RUMOR: "传闻",
}


class EventStatus(str, Enum):
    """事件当前状态——更新/否认/更正/过期都会改变它。"""

    RUMORED = "RUMORED"        # 传闻中，尚无官方口径
    CONFIRMED = "CONFIRMED"    # 已被官方证实
    DENIED = "DENIED"          # 已被官方否认
    CORRECTED = "CORRECTED"    # 出现更正
    EXPIRED = "EXPIRED"        # 超期无后续
    DISPUTED = "DISPUTED"      # 证据冲突且无法按权重压制


STATUS_LABEL: dict[EventStatus, str] = {
    EventStatus.RUMORED: "传闻中",
    EventStatus.CONFIRMED: "已证实",
    EventStatus.DENIED: "已否认",
    EventStatus.CORRECTED: "已更正",
    EventStatus.EXPIRED: "已过期",
    EventStatus.DISPUTED: "存在冲突",
}


@dataclass
class EvidenceAtom:
    """证据原子——系统的最小不可分单元。

    每条原始记录归一化为一个原子，**永不修改**，保证可回溯到原始字段。
    """

    atom_id: str
    raw_id: str                  # 原始记录 ID（溯源用）
    title: str
    content: str
    source_name: str             # 来源名称，如"同花顺快讯"
    source_url: str
    claim_type: ClaimType
    source_tier: SourceTier

    # ---- 四种时间（题目核心要求）----
    event_time: Optional[int] = None      # 事件实际发生时间
    disclose_time: Optional[int] = None   # 披露/发布时间
    fetch_time: Optional[int] = None      # 我方抓取时间
    update_time: Optional[int] = None     # 上游更新时间

    instruments: list[str] = field(default_factory=list)   # 关联标的
    keywords: list[str] = field(default_factory=list)
    polarity: str = "neutral"    # affirm(证实) / deny(否认) / correct(更正) / neutral

    @property
    def weight(self) -> float:
        """证据权重 = 来源层级权重 × 声明类型折扣。"""
        return round(SOURCE_WEIGHT[self.source_tier] * CLAIM_WEIGHT[self.claim_type], 4)

    @property
    def was_edited(self) -> bool:
        """披露时间 != 更新时间 → 该条记录在发布后被修改过。"""
        if self.disclose_time is None or self.update_time is None:
            return False
        return self.update_time > self.disclose_time

    @property
    def lag_seconds(self) -> Optional[int]:
        """披露滞后 = 披露时间 - 发生时间。体现信息传导速度。"""
        if self.event_time is None or self.disclose_time is None:
            return None
        return self.disclose_time - self.event_time

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["claim_type"] = self.claim_type.value
        d["source_tier"] = self.source_tier.value
        d["claim_label"] = CLAIM_LABEL[self.claim_type]
        d["tier_label"] = TIER_LABEL[self.source_tier]
        d["weight"] = self.weight
        d["was_edited"] = self.was_edited
        d["lag_seconds"] = self.lag_seconds
        return d


@dataclass
class Event:
    """事件——由多个证据原子归并而成，拥有状态机。"""

    event_id: str
    title: str
    summary: str
    instruments: list[str]
    atoms: list[EvidenceAtom] = field(default_factory=list)

    status: EventStatus = EventStatus.RUMORED
    status_reason: str = ""
    conflicts: list[dict] = field(default_factory=list)
    resolve_basis: str = ""      # 归并依据（可审计）

    # 事件级四时间
    first_seen: Optional[int] = None       # 最初来源时间
    last_updated: Optional[int] = None     # 最近更新时间
    origin_atom_id: str = ""               # 最初来源原子

    def to_dict(self, include_atoms: bool = True) -> dict[str, Any]:
        atoms = sorted(self.atoms, key=lambda a: (a.disclose_time or 0))
        d: dict[str, Any] = {
            "event_id": self.event_id,
            "title": self.title,
            "summary": self.summary,
            "instruments": self.instruments,
            "status": self.status.value,
            "status_label": STATUS_LABEL[self.status],
            "status_reason": self.status_reason,
            "conflicts": self.conflicts,
            "resolve_basis": self.resolve_basis,
            "first_seen": self.first_seen,
            "last_updated": self.last_updated,
            "origin_atom_id": self.origin_atom_id,
            "atom_count": len(atoms),
            "version_count": len({a.disclose_time for a in atoms}),
            "tiers": sorted({a.source_tier.value for a in atoms}),
            "has_conflict": len(self.conflicts) > 0,
        }
        if include_atoms:
            d["atoms"] = [a.to_dict() for a in atoms]
        return d


def make_id(*parts: str) -> str:
    """稳定 ID 生成——保证同输入同输出，便于幂等与对账。"""
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return h[:12]
