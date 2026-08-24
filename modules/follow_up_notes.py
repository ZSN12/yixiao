# -*- coding: utf-8 -*-
"""跟进小记回传 + 意向动态再分析(AI 闭环进化)模块。

业务闭环:
    销售在飞书卡片/手机端录入一段跟进小记(如"今天和李总聊了, 下月有 50 万
    预算, 但担心交付周期"), 本模块:
    1. 持久化小记到 SQLite(follow_up_notes 表);
    2. 把「小记文本」作为新的意向/流失信号, 叠加到规则引擎重新打分,
       动态重估客户的意向等级 / 流失风险 / 核心诉求 / 跟进策略;
    3. 返回「再分析结果 + 等级变化(升级/降级/持平)」, 供上层刷新卡片与
       飞书多维表格。

设计约束:
    - 复用 profile_analyzer 的关键词词典(INTENT_POSITIVE/NEGATIVE_KEYWORDS,
      CHURN_POSITIVE_KEYWORDS), 保证与流水线画像分析同一套判定口径;
    - 规则引擎确定性、可复现, 不依赖 LLM(mock_mode 下开箱即跑);
    - 不污染 mock_customers.json 测试基线(小记存 SQLite, 与画像历史同源)。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from modules import data_loader
from modules.data_loader import Base, Mapped, String, Text, mapped_column
from modules.profile_analyzer import (
    CHURN_POSITIVE_KEYWORDS,
    INTENT_NEGATIVE_KEYWORDS,
    INTENT_POSITIVE_KEYWORDS,
    _score_to_churn_level,
    _score_to_intention_level,
)

logger = logging.getLogger(__name__)


# ============================================================
# 跟进小记存储(SQLite)
# ============================================================

class FollowUpNote(Base):
    """跟进小记表: 记录销售每次录入的跟进内容与当时的意向重估结果。"""

    __tablename__ = "follow_up_notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_name: Mapped[str] = mapped_column(String(128))
    sales_id: Mapped[str] = mapped_column(String(32), default="")
    note_text: Mapped[str] = mapped_column(Text)
    intention_before: Mapped[str] = mapped_column(String(8), default="")
    intention_after: Mapped[str] = mapped_column(String(8), default="")
    churn_before: Mapped[str] = mapped_column(String(8), default="")
    churn_after: Mapped[str] = mapped_column(String(8), default="")
    result_json: Mapped[str] = mapped_column(Text)   # 再分析结果快照
    created_at: Mapped[str] = mapped_column(String(32))


def save_note(
    customer_id: str,
    customer_name: str,
    note_text: str,
    sales_id: str = "",
    intention_before: str = "",
    intention_after: str = "",
    churn_before: str = "",
    churn_after: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> Optional[FollowUpNote]:
    """持久化一条跟进小记 + 再分析结果。

    Returns:
        FollowUpNote: 保存成功返回实例, 失败返回 None。
    """
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is None:
            logger.warning("数据库不可用, 跳过保存跟进小记")
            return None
        note = FollowUpNote(
            customer_id=customer_id,
            customer_name=customer_name,
            sales_id=sales_id,
            note_text=note_text,
            intention_before=intention_before,
            intention_after=intention_after,
            churn_before=churn_before,
            churn_after=churn_after,
            result_json=json.dumps(result or {}, ensure_ascii=False),
            created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        )
        session.add(note)
        session.commit()
        session.refresh(note)
        session.close()
        logger.info("已保存跟进小记: customer=%s, 意向 %s->%s", customer_id, intention_before, intention_after)
        return note
    except Exception as exc:  # noqa: BLE001
        logger.error("保存跟进小记失败(%s): %s", customer_id, exc)
        return None


def list_notes(customer_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """查询跟进小记列表(可按客户过滤, 按时间倒序)。"""
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is None:
            return []
        query = session.query(FollowUpNote).order_by(FollowUpNote.created_at.desc())
        if customer_id:
            query = query.filter(FollowUpNote.customer_id == customer_id)
        rows = query.limit(limit).all()
        result = [
            {
                "customer_id": r.customer_id,
                "customer_name": r.customer_name,
                "sales_id": r.sales_id,
                "note_text": r.note_text,
                "intention_before": r.intention_before,
                "intention_after": r.intention_after,
                "churn_before": r.churn_before,
                "churn_after": r.churn_after,
                "created_at": r.created_at,
            }
            for r in rows
        ]
        session.close()
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("查询跟进小记失败: %s", exc)
        return []


# ============================================================
# 意向动态再分析引擎(规则引擎, 确定性)
# ============================================================

def _count_keywords(text: str, keywords: List[str]) -> Dict[str, int]:
    """统计关键词命中次数(与 profile_analyzer 同口径)。"""
    return {kw: text.count(kw) for kw in keywords if kw in text}


def _latest_analysis(customer_id: str) -> Dict[str, Any]:
    """读取该客户最新一条画像分析结果(意向/流失/诉求/建议)。"""
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is None:
            return {}
        rows = (
            session.query(data_loader.AnalysisHistory)
            .filter(data_loader.AnalysisHistory.customer_id == customer_id)
            .order_by(data_loader.AnalysisHistory.created_at.desc())
            .limit(1)
            .all()
        )
        session.close()
        if not rows:
            return {}
        try:
            return json.loads(rows[0].result_json)
        except (json.JSONDecodeError, TypeError):
            return {}
    except Exception as exc:  # noqa: BLE001
        logger.error("读取最新画像失败(%s): %s", customer_id, exc)
        return {}


def reanalyze_with_note(
    customer_id: str,
    customer_name: str,
    note_text: str,
    sales_id: str = "",
    persist: bool = True,
) -> Dict[str, Any]:
    """把跟进小记作为新信号, 动态重估客户意向/流失等级。

    判定口径(与 profile_analyzer 规则引擎一致):
        - 意向加分词(预算/合同/尽快/需求...) → 意向升;
        - 意向减分词(再看看/太贵/考虑...) → 意向降;
        - 流失加分词(竞品/暂缓/不回复...) → 流失升 + 意向降。
    在小记文本上统计命中, 得到「信号增量」, 叠加到当前等级上重估。

    Args:
        customer_id: 客户 ID。
        customer_name: 客户名称。
        note_text: 跟进小记文本。
        sales_id: 录入小记的销售 ID。
        persist: 是否持久化小记与再分析结果。

    Returns:
        dict: {customer_id, customer_name, note_text, intention_before,
            intention_after, churn_before, churn_after, intention_change
            (upgrade/downgrade/unchanged), signal_detail, new_core_demands,
            new_follow_up_suggestion}。
    """
    note_text = (note_text or "").strip()
    latest = _latest_analysis(customer_id)

    intention_before = latest.get("intention_level", "中")
    churn_before = latest.get("churn_risk", "低")

    # 小记文本信号统计
    pos_hits = _count_keywords(note_text, INTENT_POSITIVE_KEYWORDS)
    neg_hits = _count_keywords(note_text, INTENT_NEGATIVE_KEYWORDS)
    churn_hits = _count_keywords(note_text, CHURN_POSITIVE_KEYWORDS)

    pos_total = sum(pos_hits.values())
    neg_total = sum(neg_hits.values())
    churn_total = sum(churn_hits.values())

    # 小记信号增量分(与规则引擎同向)
    intention_delta = pos_total - neg_total - churn_total
    churn_delta = churn_total

    # 等级重估: 用「当前等级对应的基准分 + 增量」重算
    level_base = {"高": 3, "中": 1, "低": 0}[intention_before]
    new_intention_score = level_base + intention_delta
    intention_after = _score_to_intention_level(new_intention_score)

    churn_base = {"高": 2, "中": 1, "低": 0}[churn_before]
    new_churn_score = churn_base + churn_delta
    churn_after = _score_to_churn_level(new_churn_score)

    # 意向变化方向
    rank = {"高": 2, "中": 1, "低": 0}
    if rank[intention_after] > rank[intention_before]:
        intention_change = "upgrade"
    elif rank[intention_after] < rank[intention_before]:
        intention_change = "downgrade"
    else:
        intention_change = "unchanged"

    # 信号明细(供卡片展示溯源)
    def _fmt(hits: Dict[str, int]) -> str:
        return "/".join(f"{k}x{n}" for k, n in hits.items()) if hits else "无"

    signal_detail = {
        "positive": _fmt(pos_hits),
        "negative": _fmt(neg_hits),
        "churn": _fmt(churn_hits),
        "intention_delta": intention_delta,
    }

    # 新的核心诉求: 在最新诉求基础上, 用命中的需求话术模板补充
    new_core_demands = list(latest.get("core_demands", []) or [])
    new_follow_up = _build_updated_suggestion(
        intention_after, churn_after, pos_hits, neg_hits, churn_hits
    )

    result = {
        "customer_id": customer_id,
        "customer_name": customer_name,
        "note_text": note_text,
        "intention_before": intention_before,
        "intention_after": intention_after,
        "churn_before": churn_before,
        "churn_after": churn_after,
        "intention_change": intention_change,
        "signal_detail": signal_detail,
        "new_core_demands": new_core_demands,
        "new_follow_up_suggestion": new_follow_up,
    }

    if persist:
        save_note(
            customer_id=customer_id,
            customer_name=customer_name,
            note_text=note_text,
            sales_id=sales_id,
            intention_before=intention_before,
            intention_after=intention_after,
            churn_before=churn_before,
            churn_after=churn_after,
            result=result,
        )

    logger.info(
        "跟进小记再分析: customer=%s, 意向 %s->%s, 流失 %s->%s, 变化=%s",
        customer_id, intention_before, intention_after, churn_before, churn_after, intention_change,
    )
    return result


def _build_updated_suggestion(
    intention_after: str,
    churn_after: str,
    pos_hits: Dict[str, int],
    neg_hits: Dict[str, int],
    churn_hits: Dict[str, int],
) -> str:
    """根据再分析结果 + 小记信号, 生成更新的跟进策略建议。"""
    parts: List[str] = []

    # 意向升级: 给出加速推进建议
    if intention_after == "高":
        parts.append("客户意向升温, 建议 24 小时内推进报价/合同/立项, 抓住决策窗口。")
    elif intention_after == "中":
        parts.append("客户意向保持观望, 建议 3 日内针对顾虑给出回应并安排决策层拜访。")
    else:
        parts.append("客户意向偏弱, 建议转为低频价值型维护, 等待重启信号。")

    # 流失风险提示
    if churn_after == "高":
        parts.append("⚠️ 流失风险升高, 建议优先挽回: 主动了解流失原因, 提供专属优惠或进阶服务。")
    elif churn_after == "中":
        parts.append("存在一定流失风险, 注意巩固关系, 及时回应疑虑防转投竞品。")

    # 针对小记里命中的具体信号给定向建议
    if "预算" in pos_hits:
        parts.append("小记提及预算, 建议尽快提供有竞争力的正式报价锁定预算窗口。")
    if "合同" in pos_hits or "签约" in pos_hits:
        parts.append("小记提及合同/签约意向, 建议准备框架合同与商务条款草案。")
    if "竞品" in churn_hits:
        parts.append("小记提及竞品, 建议制定差异化策略并输出成功案例背书。")
    if "暂缓" in churn_hits or "搁置" in churn_hits:
        parts.append("小记提及暂缓/搁置, 建议保持低频触达并关注重启信号。")
    if not (pos_hits or neg_hits or churn_hits):
        parts.append("小记未检出强信号, 建议补充关键信息(预算/决策人/时间节点/竞品情况)。")

    return "；".join(parts)
