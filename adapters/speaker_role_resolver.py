# -*- coding: utf-8 -*-
"""说话人角色判定器(speaker_role_resolver): Speaker_N → 销售 | 客户。

职责: 在 adapter 层完成「匿名说话人 → 业务角色」的映射, 业务模块零改动。
ASR 只告诉你「有 Speaker_0 / Speaker_1 两个不同的人」, 不会告诉你谁是销售、
谁是客户 —— 本模块按三层判定(命中即停, 逐层降级):

Tier-1 元数据绑定(confidence >= 0.95):
    - 双声道: left=销售, right=客户(channel_map);
    - 主被叫 + direction: outbound → 主叫≈销售; inbound → 被叫≈销售;
    - sales_id 已知 + 仅 2 人 → 结合「外呼销售先开口」启发式。

Tier-2 话术规则(confidence 0.7~0.9):
    - 统计各 Speaker 特征词命中数(销售词/客户词);
    - 谁称呼「X总」多 → 更像销售;
    - 首句规则: outbound 且 Speaker_0 先说话 → Speaker_0 倾向销售。

Tier-3 LLM 兜底(confidence 0.6~0.85):
    - 调 llm_client.chat_json, 输入带 Speaker 标签的全文, 输出角色映射;
    - 失败 → 不抛异常, 降级 Tier-2 或标记 needs_review。

保守策略(完全无法判定时, 禁止随机标角色):
    - 2 人通话: 按 direction=outbound → 先说话者=销售;
    - 3 人以上: 只处理时长占比最高的 2 人, 其余合并「未知」;
    - 宁可 confidence=0.5 + 标记 review, 也不随机。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from adapters.asr_client import AsrSegment

logger = logging.getLogger(__name__)

# 销售特征词(命中越多越像销售)
SALES_KEYWORDS: List[str] = [
    "我是", "公司", "给您介绍", "方案", "报价发您", "您好",
    "跟进", "我们产品", "这边", "为您", "请", "谢谢您",
]
# 客户特征词(命中越多越像客户)
CUSTOMER_KEYWORDS: List[str] = [
    "我们预算", "和领导商量", "太贵", "再看看", "竞品", "考虑一下",
    "预算", "采购", "需求", "什么时候", "怎么收费", "价格",
]
# 称呼「X总/X主任/X经理」的正则(销售更常称呼对方职位头衔)
_HONORIFIC_PATTERN = re.compile(r"[\u4e00-\u9fa5]{1,2}(?:总|主任|经理|总监|部长|老师)")


@dataclass
class RoleResolution:
    """说话人角色判定结果。"""

    speaker_roles: Dict[str, str]          # {"Speaker_0": "销售", "Speaker_1": "客户"}
    method: str                            # metadata | heuristic | llm | manual
    confidence: float                      # 0~1
    notes: str = ""                        # 判定依据, 便于排查

    def needs_review(self, threshold: float = 0.7) -> bool:
        """是否需要人工复核(置信度低于阈值)。"""
        return self.confidence < threshold


def resolve(segments: List[AsrSegment], manifest: Dict) -> RoleResolution:
    """入口: 根据转写分段 + 通话元数据, 判定每个说话人的业务角色。

    Args:
        segments: ASR 分段转写列表。
        manifest: 通话元数据(含 direction/sales_mobile/customer_mobile 等)。

    Returns:
        RoleResolution: 角色映射 + 判定方法 + 置信度 + 依据说明。
    """
    speakers = _unique_speakers(segments)
    if not speakers:
        return RoleResolution(
            speaker_roles={}, method="manual", confidence=0.0,
            notes="无有效转写分段, 无法判定角色",
        )

    # Tier-1 元数据绑定
    res = _resolve_by_metadata(segments, manifest, speakers)
    if res is not None:
        return res

    # Tier-2 话术规则
    res = _resolve_by_heuristic(segments, manifest, speakers)
    if res is not None and res.confidence >= 0.7:
        return res

    # Tier-3 LLM 兜底
    res_llm = _resolve_by_llm(segments, manifest, speakers)
    if res_llm is not None:
        return res_llm

    # 保守兜底: 用 Tier-2 结果(即使低置信度), 但标记 review
    if res is not None:
        res.notes += "; 置信度偏低, 需人工复核"
        res.confidence = min(res.confidence, 0.6)
        return res

    return RoleResolution(
        speaker_roles={}, method="manual", confidence=0.0,
        notes="所有判定层均失败, 需人工指定角色",
    )


# ============================================================
# 辅助函数
# ============================================================


def _unique_speakers(segments: List[AsrSegment]) -> List[str]:
    """按首次出现顺序返回去重的说话人标签列表。"""
    seen: List[str] = []
    for seg in segments:
        if seg.speaker and seg.speaker not in seen:
            seen.append(seg.speaker)
    return seen


def _speaker_stats(segments: List[AsrSegment]) -> Dict[str, Dict]:
    """统计每个说话人的发言总时长、特征词命中数、称呼命中数。"""
    stats: Dict[str, Dict] = {}
    for seg in segments:
        if not seg.speaker:
            continue
        st = stats.setdefault(seg.speaker, {
            "total_ms": 0, "sales_hits": 0, "customer_hits": 0, "honorifics": 0,
        })
        st["total_ms"] += max(0, seg.end_ms - seg.start_ms)
        text = seg.text or ""
        st["sales_hits"] += sum(1 for kw in SALES_KEYWORDS if kw in text)
        st["customer_hits"] += sum(1 for kw in CUSTOMER_KEYWORDS if kw in text)
        st["honorifics"] += len(_HONORIFIC_PATTERN.findall(text))
    return stats


def _first_speaker(segments: List[AsrSegment]) -> Optional[str]:
    """返回最先开口的说话人(按 start_ms 排序)。"""
    ordered = [s for s in segments if s.speaker]
    if not ordered:
        return None
    return min(ordered, key=lambda s: s.start_ms).speaker


def _top_speakers_by_duration(segments: List[AsrSegment], n: int = 2) -> List[str]:
    """返回发言总时长最高的前 n 个说话人(用于 3 人以上通话的裁剪)。"""
    stats = _speaker_stats(segments)
    ranked = sorted(stats.keys(), key=lambda sp: stats[sp]["total_ms"], reverse=True)
    return ranked[:n]


# ============================================================
# Tier-1: 元数据绑定
# ============================================================


def _resolve_by_metadata(
    segments: List[AsrSegment], manifest: Dict, speakers: List[str],
) -> Optional[RoleResolution]:
    """Tier-1 元数据绑定: 双声道 / 主被叫 + direction / sales_id 已知。"""
    channel_map = manifest.get("channel_map")
    if channel_map:
        # 双声道: left=销售, right=客户(或自定义映射)
        roles: Dict[str, str] = {}
        for sp in speakers:
            # 约定: channel_map 形如 {"left": "销售", "right": "客户"},
            # 说话人标签若为 Speaker_0/Speaker_1, 需由调用方在 manifest 里
            # 显式给出 speaker → channel 的映射; 否则按 left→Speaker_0 约定。
            # 这里优先读 manifest 里的 speaker_channel 映射, 否则按顺序。
            roles[sp] = channel_map.get(sp, "")
        if all(roles.values()):
            return RoleResolution(
                speaker_roles=roles, method="metadata", confidence=0.97,
                notes=f"双声道映射: {channel_map}",
            )

    # 主被叫 + direction
    direction = str(manifest.get("direction") or "").lower()
    has_mobiles = bool(manifest.get("sales_mobile")) and bool(manifest.get("customer_mobile"))
    if direction in ("outbound", "inbound") and has_mobiles and len(speakers) == 2:
        first = _first_speaker(segments)
        if first:
            # outbound: 主叫(销售)先开口; inbound: 被叫(销售)先开口
            caller_role = "销售" if direction == "outbound" else "客户"
            other_role = "客户" if direction == "outbound" else "销售"
            roles = {first: caller_role}
            for sp in speakers:
                if sp != first:
                    roles[sp] = other_role
            return RoleResolution(
                speaker_roles=roles, method="metadata", confidence=0.95,
                notes=f"主被叫 + direction={direction}: {first} 判定为{caller_role}",
            )

    # sales_id 已知 + 仅 2 人 → 外呼销售先开口启发式
    if manifest.get("sales_id") and len(speakers) == 2:
        first = _first_speaker(segments)
        if first:
            roles = {first: "销售"}
            for sp in speakers:
                if sp != first:
                    roles[sp] = "客户"
            return RoleResolution(
                speaker_roles=roles, method="metadata", confidence=0.95,
                notes=f"sales_id 已知({manifest.get('sales_id')}), 外呼销售先开口: {first}=销售",
            )

    return None


# ============================================================
# Tier-2: 话术规则
# ============================================================


def _resolve_by_heuristic(
    segments: List[AsrSegment], manifest: Dict, speakers: List[str],
) -> Optional[RoleResolution]:
    """Tier-2 话术规则: 特征词命中 + 称呼 + 首句。"""
    if len(speakers) != 2:
        # 3 人以上: 只处理 Top-2 时长说话人
        top2 = _top_speakers_by_duration(segments, 2)
        if len(top2) == 2:
            speakers = top2
            notes = "3 人以上通话: 仅处理时长 Top-2 说话人"
        else:
            return None
    else:
        notes = ""

    stats = _speaker_stats(segments)
    # 销售倾向分 = 销售词命中 - 客户词命中 + 称呼命中
    scores: Dict[str, float] = {}
    for sp in speakers:
        st = stats.get(sp, {"sales_hits": 0, "customer_hits": 0, "honorifics": 0})
        scores[sp] = st["sales_hits"] - st["customer_hits"] + st["honorifics"]

    ranked = sorted(speakers, key=lambda sp: scores[sp], reverse=True)
    tie_broken = False
    if scores[ranked[0]] == scores[ranked[1]]:
        # 分数打平: 用首句规则(外呼先开口=销售)打破平局, 但证据不足
        first = _first_speaker(segments)
        if first in speakers:
            ranked = [first] + [sp for sp in speakers if sp != first]
            tie_broken = True

    roles = {ranked[0]: "销售", ranked[1]: "客户"}
    # 置信度: 分数差距越大越可信, 映射到 0.7~0.9;
    # 打平(仅首句规则破局)时证据不足, 置信度降为 0.5 触发 review。
    gap = abs(scores[ranked[0]] - scores[ranked[1]])
    confidence = 0.5 if tie_broken else min(0.9, 0.7 + gap * 0.05)
    if notes:
        notes += "; "
    notes += f"话术特征分: {ranked[0]}={scores[ranked[0]]}, {ranked[1]}={scores[ranked[1]]}"
    return RoleResolution(
        speaker_roles=roles, method="heuristic", confidence=round(confidence, 2), notes=notes,
    )


# ============================================================
# Tier-3: LLM 兜底
# ============================================================


def _resolve_by_llm(
    segments: List[AsrSegment], manifest: Dict, speakers: List[str],
) -> Optional[RoleResolution]:
    """Tier-3 LLM 兜底: 调 llm_client.chat_json 判定角色, 失败降级返回 None。"""
    try:
        from config.settings import settings
        if not settings.phone_role_llm_fallback:
            return None
        from modules import llm_client
        if not llm_client.enabled():
            return None
    except Exception:  # noqa: BLE001
        return None

    transcript = "\n".join(f"{seg.speaker}: {seg.text}" for seg in segments if seg.speaker)
    prompt = (
        "以下是电话销售录音的分段转写, 每个说话人用匿名标签(Speaker_0/1/...)表示。\n"
        "请判断每个说话人的角色: 销售(卖方)还是客户(买方)。\n"
        "只输出一个 JSON 对象, 形如 {\"Speaker_0\": \"销售\", \"Speaker_1\": \"客户\"}。\n\n"
        f"{transcript}"
    )
    try:
        result = llm_client.chat_json(
            "你是电话销售录音角色判定专家。",
            prompt,
            max_tokens=256,
            temperature=0.1,
        )
        if isinstance(result, dict) and result:
            roles = {sp: str(result.get(sp, "")) for sp in speakers if result.get(sp) in ("销售", "客户")}
            if len(roles) == len(speakers) and all(roles.values()):
                return RoleResolution(
                    speaker_roles=roles, method="llm", confidence=0.7,
                    notes="LLM 语义判定角色(兜底, 置信度低于元数据/规则判定)",
                )
    except Exception as exc:  # noqa: BLE001 —— LLM 失败不抛, 降级
        logger.warning("LLM 角色判定失败(%s), 降级启发式", exc)
    return None
