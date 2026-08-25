# -*- coding: utf-8 -*-
"""画像分析器(profile_analyzer): 生成客户画像/意向等级/需求点/流失风险/跟进建议。

本模块是"分析师 Agent"的核心产出物 —— 分析结果中的 customer_profile 文本
会被 rag_retriever.retrieve_top_sales 当作 query_text 使用(单向依赖:
检索器吃分析器的输出, 本模块不 import rag_retriever)。

双引擎设计(渐进降级, 与全项目一致):
1. 规则引擎(mock/default): 不调 LLM, 基于聊天记录关键词打分确定性生成结果,
   全部可复现, 保证"没有 API Key 也能开箱即跑"。
2. LLM 引擎(配置 kimi_api_key 时): 调 Kimi K2.7 Code(Anthropic Messages
   接口)并约束 JSON 输出; 任何失败(网络/超时/格式/校验失败) → 记日志 →
   自动降级到规则引擎结果, 保证不中断。

关键词词典(任务规定"至少覆盖", 以下为覆盖全集; 均用于子串命中计数):
- 意向加分:   预算/采购/合同/签约/报价/方案/立项/尽快/时间/需求
- 意向减分:   再看看/考虑一下/和领导商量/太贵
               + 补充:审批/考虑(延迟、犹豫类信号, 修正"暂缓审批仍计高分"的问题)
- 流失加分:   竞品/已购/暂缓/搁置/不回复/预算不足/长期未联系
               + 补充:预算紧张/供应商(对应 mock 数据中"预算紧张"与"看其他供应商"场景)

意向规则: 意向分 = 意向加分命中数 - 意向减分命中数 - 流失加分命中数
          (流失信号同时压降意向分, 避免"客户已暂缓/在看竞品"仍被判高意向),
          再由阈值映射为 高/中/低。
流失规则: 流失分 = 流失加分命中数, 由阈值映射为 高/中/低。
全部确定性、可复现。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel
from typing_extensions import Literal

from modules.data_loader import ChatRecord, Customer

logger = logging.getLogger(__name__)

# ============================================================
# 关键词词典(子串命中计数; "至少覆盖"任务规定集, 另含少量语义补充)
# ============================================================

# 意向加分词: 积极的采购/推进信号
INTENT_POSITIVE_KEYWORDS: List[str] = [
    "预算", "采购", "合同", "签约", "报价", "方案", "立项", "尽快", "时间", "需求",
]

# 意向减分词: 犹豫、推迟、砍价等消极信号(含补充: 审批/考虑 —— 延迟审批与观望)
INTENT_NEGATIVE_KEYWORDS: List[str] = [
    "再看看", "考虑一下", "和领导商量", "太贵",
    "审批", "考虑",
]

# 流失加分词: 竞品介入、已购他方、项目暂停等流失风险信号
# (补充: 预算紧张/供应商 —— 预算收紧与转向其他供应商同样是高危信号)
CHURN_POSITIVE_KEYWORDS: List[str] = [
    "竞品", "已购", "暂缓", "搁置", "不回复", "预算不足", "长期未联系",
    "预算紧张", "供应商",
]

# 意向等级阈值: 意向分 >= 高阈值 → 高; >= 低阈值 → 中; 否则 → 低
INTENTION_HIGH_SCORE: int = 3
INTENTION_MID_SCORE: int = 1
# 流失等级阈值: 流失分 >= 高阈值 → 高; >= 中阈值 → 中; 否则 → 低
CHURN_HIGH_SCORE: int = 2
CHURN_MID_SCORE: int = 1

# 价格信号时间衰减: 7 天新鲜度窗口(超过 7 天的报价视为过期意向价格, 降权)
PRICE_FRESH_DAYS: int = 7
# 价格信号对意向评分的权重(过期价格经衰减后乘此系数计入意向分)
PRICE_SIGNAL_WEIGHT: float = 1.0

# 需求话术模板: 关键词 → 一条需求描述(用于 core_demands 生成)
_DEMAND_TEMPLATES: Dict[str, str] = {
    "预算": "在客户预算范围内提供完整报价与分项成本明细, 支持预算报批",
    "采购": "配合客户采购流程, 提供规范的产品清单/招标与验收材料",
    "合同": "准备框架合同与商务条款草案, 与客户对齐签约条件",
    "签约": "推动合同签署, 明确签约时间节点与交付承诺",
    "报价": "尽快提供有竞争力的正式报价, 锁定客户预算窗口",
    "方案": "提供贴合业务场景的定制化方案, 并附成功案例背书",
    "立项": "输出立项所需的方案与预算材料, 配合客户完成内部立项流程",
    "尽快": "加快响应与交付节奏, 满足客户对时效的要求",
    "时间": "明确实施与交付时间表, 确认关键节点与里程碑",
    "需求": "深入梳理并确认客户的具体需求细节与优先级",
    "审批": "准备决策支持材料, 缩短客户内部审批周期",
    "考虑": "针对客户观望顾虑给出化解方案, 推动早日下决心",
    "再看看": "持续提供价值内容与案例, 引导客户从观望转向实质推进",
    "考虑一下": "针对客户观望顾虑给出化解方案, 推动早日下决心",
    "和领导商量": "提供决策人汇报材料, 协助客户内部向上沟通",
    "太贵": "提供降本/分期/按需付费等灵活商务方案, 论证长期价值",
    "竞品": "制定竞品差异化策略, 突出自身产品优势与客户案例",
    "已购": "保持关系维护, 挖掘增购/换机/服务续约等二次商机",
    "暂缓": "提供决策支持材料与激励政策, 推动项目尽快重启",
    "搁置": "保持低频触达, 关注客户重启信号, 等待时机重新激活",
    "不回复": "调整触达方式(电话/上门/新话题), 重新建立联系",
    "预算不足": "提供轻量起步方案(降配/分期/订阅), 降低客户决策门槛",
    "预算紧张": "提供轻量起步方案(降配/分期/订阅), 缓解预算压力",
    "长期未联系": "安排主动回访, 了解客户近期动态并重新建立联系",
    "供应商": "主动澄清与对比, 输出差异化优势与案例, 防止客户转向他方",
}

# 需求不足 3 条时的通用补充话术
_GENERIC_DEMANDS: List[str] = [
    "建立定期沟通机制, 持续跟进客户需求变化",
    "安排高层/技术对接, 深化客户关系与信任",
]

# 跟进建议模板(按意向等级)
_SUGGESTION_BY_INTENTION: Dict[str, str] = {
    "高": "客户意向明确: 建议24小时内完成正式报价与合同条款对齐, "
          "由资深销售主导推进签约, 明确立项与采购时间节点; 同步防范竞品介入。",
    "中": "客户有意向但存在顾虑: 建议3日内针对价格/竞品/审批节奏等顾虑给出回应, "
          "提供差异化方案与客户案例, 安排决策层拜访推动决策。",
    "低": "客户当前意向偏弱: 建议转为低频价值型维护(定期分享行业干货/方案更新), "
          "关注客户预算与采购时机变化, 等待重启窗口, 不以推销施压。",
}

# 流失风险叠加建议
_CHURN_ADJUST_SUGGESTION: Dict[str, str] = {
    "高": "流失风险高: 建议优先挽回, 主动了解流失原因, 提供专属优惠或进阶服务作为补救。",
    "中": "存在一定流失风险: 跟进时注意巩固关系, 及时回应客户疑虑, 防止转投竞品。",
    "低": "流失风险低: 维持正常推进节奏即可。",
}

# LLM 系统提示词(任务给定原文, 照抄)
SYSTEM_PROMPT = """
你是资深B端销售客户分析师，请根据客户基础信息和历史沟通记录，输出专业的结构化分析结果。
要求：
1. 只输出一个纯 JSON 对象，不要任何额外解释、开场白、结束语或 Markdown 代码块包裹
2. 所有判断必须基于提供的信息，禁止编造
3. 意向等级和流失风险必须从给定枚举中选择
4. 核心需求提炼要精准，贴合客户真实诉求

重要 —— 对话上下文理解与价格时间衰减规则：
1. 区分说话者：只有「客户」主动表达的意向/价格/顾虑才是真实信号，销售的话术不应与客户意图混同。
2. 价格/报价按时间衰减：客户报出的价格/预算若超过 7 天，视为「过期意向价格」，不得作为最新意向价依据，
   应在 customer_profile 与 intention_reason 中明确标注「该价格已过期(>7天)，可能非最新意向」。
3. 7 天内客户报的价格/预算才视为「最新有效意向价格」，可正常参与意向判断。
4. 若输入中提供了「价格时间衰减分析」，请直接参考其结果，不要重新计算时间。

JSON 字段契约（严格按此结构输出）：
{
  "customer_profile": "客户画像: 含行业属性/决策角色/核心痛点/预算范围的中文描述",
  "intention_level": "高|中|低",
  "intention_reason": "意向判断依据",
  "core_demands": ["3-5条核心需求描述"],
  "churn_risk": "高|中|低",
  "churn_reason": "流失风险原因",
  "follow_up_suggestion": "跟进建议"
}
"""

# 称呼正则(抽取决策角色, 如"王总/李主任/陈经理")
_ROLE_PATTERN = re.compile(r"([\u4e00-\u9fa5]{1,2}(?:总|主任|经理|总监|部长|老师))")
# 金额正则(抽取预算范围, 如"500万"/"300万", 只取数字+万)
_BUDGET_PATTERN = re.compile(r"(\d+(?:\.\d+)?万)")
# 价格金额正则(更宽: 数字+万/亿/元/块, 用于"报价/价格"语境下的金额识别)
_PRICE_AMOUNT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿|元|块)")
# 价格/报价语境词(用于判断一条消息是否在"谈价格")
_PRICE_CONTEXT_KEYWORDS = ["报价", "价格", "预算", "多少钱", "收费", "成本", "单价", "总价", "费用"]


# ============================================================
# 时间衰减 + 价格信号提取(对话上下文理解的核心增强)
# ============================================================


def _parse_chat_date(chat_time: str) -> Optional[date]:
    """解析聊天时间字符串为 date 对象。

    支持 `YYYY-MM-DD`、`YYYY-MM-DDTHH:MM:SS`、`YYYY-MM-DD HH:MM:SS` 等格式。
    解析失败返回 None(该条消息的时间无法用于衰减计算, 按"过期"处理由上层决定)。
    """
    if not chat_time:
        return None
    try:
        # 优先按日期解析(取前 10 位)
        return datetime.strptime(chat_time[:10], "%Y-%m-%d").date()
    except (ValueError, IndexError):
        return None


def _now_date() -> date:
    """返回真实当前日期(时间衰减的基准时间)。"""
    return datetime.now().date()


def _days_since(chat_date: Optional[date], reference: Optional[date] = None) -> Optional[int]:
    """计算聊天日期距基准日期的天数(负数表示未来, 按 0 处理为"最新")。

    Args:
        chat_date: 聊天日期(可 None)。
        reference: 基准日期(默认真实当前日期)。

    Returns:
        int | None: 距今天数(>=0); chat_date 为 None 时返回 None。
    """
    if chat_date is None:
        return None
    ref = reference or _now_date()
    delta = (ref - chat_date).days
    return max(0, delta)


def price_freshness_weight(chat_time: str, reference: Optional[date] = None) -> Tuple[float, Optional[int]]:
    """计算价格信号的时间衰减权重。

    规则(7 天新鲜度窗口):
        - 0~7 天内: 权重 1.0(最新有效意向价格);
        - 超过 7 天: 按半衰期衰减 —— 每超过 7 天权重减半, 最低保底 0.1
          (过期价格仍保留弱信号, 不彻底忽略)。

    Args:
        chat_time: 聊天时间字符串。
        reference: 基准日期(默认真实当前日期)。

    Returns:
        tuple[float, int | None]: (衰减权重 0.1~1.0, 距今天数; 时间无法解析时天数为 None)。
    """
    chat_date = _parse_chat_date(chat_time)
    days = _days_since(chat_date, reference)
    if days is None:
        return 0.1, None
    if days <= PRICE_FRESH_DAYS:
        return 1.0, days
    # 半衰期衰减: 每超过一个 7 天窗口权重减半, 保底 0.1
    windows = (days - PRICE_FRESH_DAYS) // PRICE_FRESH_DAYS + 1
    weight = max(0.1, 1.0 / (2 ** windows))
    return weight, days


def _extract_price_signal(
    chat_records: List[ChatRecord],
    reference: Optional[date] = None,
) -> List[Dict[str, object]]:
    """从对话中提取「价格/报价信号」, 附带说话者、金额、时间与衰减权重。

    上下文理解要点:
        1. 只认「客户」说的话(销售报的价是卖方报价, 客户报的才是真实意向预算);
        2. 消息须同时命中价格语境词 + 金额数字, 才算一条有效价格信号;
        3. 按 chat_time 计算时间衰减权重, 输出最新/过期标注。

    Args:
        chat_records: 会话记录列表。
        reference: 基准日期(默认真实当前日期)。

    Returns:
        list[dict]: 每条含 {amount, chat_time, days, weight, fresh, content}。
    """
    signals: List[Dict[str, object]] = []
    for record in chat_records:
        for msg in record.messages:
            if msg.role != "客户":
                continue  # 只认客户说的话(真实意向价格)
            if not any(kw in msg.content for kw in _PRICE_CONTEXT_KEYWORDS):
                continue
            m = _PRICE_AMOUNT_PATTERN.search(msg.content)
            if not m:
                continue
            weight, days = price_freshness_weight(record.chat_time, reference)
            signals.append({
                "amount": m.group(0),
                "chat_time": record.chat_time,
                "days": days,
                "weight": weight,
                "fresh": days is not None and days <= PRICE_FRESH_DAYS,
                "content": msg.content,
            })
    return signals


def _aggregate_price_signals(signals: List[Dict[str, object]]) -> Dict[str, object]:
    """聚合价格信号: 取权重最高的有效价格作为「最新意向价格」, 并汇总过期信息。

    Args:
        signals: _extract_price_signal 的输出。

    Returns:
        dict: {latest_amount, latest_weight, latest_days, fresh, expired_count,
               total_count, detail_text}。
    """
    if not signals:
        return {
            "latest_amount": "", "latest_weight": 0.0, "latest_days": None,
            "fresh": False, "expired_count": 0, "total_count": 0, "detail_text": "",
        }
    # 按权重降序(权重相同取更新的即天数更小的)
    best = max(signals, key=lambda s: (float(s["weight"]), -(int(s["days"] or 9999))))
    expired = [s for s in signals if not s["fresh"]]
    detail_parts = []
    for s in sorted(signals, key=lambda x: -(int(x["days"] or 9999))):
        tag = "最新" if s["fresh"] else "过期"
        detail_parts.append(f"{s['amount']}({tag}, {s['days']}天, 权重{s['weight']:.2f})")
    return {
        "latest_amount": best["amount"],
        "latest_weight": float(best["weight"]),
        "latest_days": best["days"],
        "fresh": bool(best["fresh"]),
        "expired_count": len(expired),
        "total_count": len(signals),
        "detail_text": "；".join(detail_parts),
    }


# ============================================================
# 输出模型(全团队共享契约, 字段精确)
# ============================================================


class AnalysisResult(BaseModel):
    """单客户画像分析结果 —— 全团队共享的分析产物契约。"""

    customer_profile: str            # 客户画像: 含 行业属性/决策角色/核心痛点/预算范围 的中文描述
    intention_level: Literal["高", "中", "低"]
    intention_reason: str            # 意向判断依据
    core_demands: List[str]          # 3-5 条核心需求
    churn_risk: Literal["高", "中", "低"]
    churn_reason: str                # 流失风险原因
    follow_up_suggestion: str        # 跟进建议


# ============================================================
# 通用工具
# ============================================================


def _join_chat_text(chat_records: List[ChatRecord]) -> str:
    """拼接全部聊天文本(供关键词命中计数)。"""
    parts: List[str] = []
    for record in chat_records:
        for msg in record.messages:
            parts.append(f"{msg.role}:{msg.content}")
    return "\n".join(parts)


def _join_customer_text(chat_records: List[ChatRecord]) -> str:
    """只拼「客户」角色的消息文本(用于区分说话者, 客户表达的意向信号更可信)。"""
    parts: List[str] = []
    for record in chat_records:
        for msg in record.messages:
            if msg.role == "客户":
                parts.append(msg.content)
    return "\n".join(parts)


def _count_keywords(text: str, keywords: List[str]) -> Dict[str, int]:
    """统计每个关键词在文本中的出现次数(子串命中, 确定性)。"""
    return {kw: text.count(kw) for kw in keywords if kw in text}


def _normalize_demands(demands: List[str]) -> List[str]:
    """规范 core_demands 到 3-5 条: 去重、截断到 5 条、不足 3 条时用通用话术补齐。"""
    unique: List[str] = []
    for d in demands:
        d = d.strip()
        if d and d not in unique:
            unique.append(d)
    result = unique[:5]
    for generic in _GENERIC_DEMANDS:
        if len(result) >= 3:
            break
        if generic not in result:
            result.append(generic)
    return result


def _score_to_intention_level(score: int) -> str:
    """意向分 → 等级(确定性阈值映射)。"""
    if score >= INTENTION_HIGH_SCORE:
        return "高"
    if score >= INTENTION_MID_SCORE:
        return "中"
    return "低"


def _score_to_churn_level(score: int) -> str:
    """流失分 → 等级(确定性阈值映射)。"""
    if score >= CHURN_HIGH_SCORE:
        return "高"
    if score >= CHURN_MID_SCORE:
        return "中"
    return "低"


def _list_hit_keywords(hit_map: Dict[str, int]) -> List[str]:
    """按词典顺序返回命中关键词(供画像/原因文本使用)。"""
    return list(hit_map.keys())


# ============================================================
# 规则引擎
# ============================================================


def _extract_role(chat_records: List[ChatRecord]) -> str:
    """从聊天记录中抽取决策角色称呼(如"王总/李主任"), 抽取不到返回空串。

    优先从客户侧消息中找称呼; 找不到时再看销售侧消息
    (销售常以"王总您好"开场, 称呼同样指向客户决策人)。
    """
    # 第一遍: 只从客户侧消息中找(客户称呼自己/他人时的角色线索)
    for record in chat_records:
        for msg in record.messages:
            if msg.role == "客户":
                m = _ROLE_PATTERN.search(msg.content)
                if m:
                    return m.group(1)
    # 第二遍: 找不到再看销售侧消息(销售常以"王总您好"开场, 称呼指向客户决策人)
    for record in chat_records:
        for msg in record.messages:
            if msg.role == "销售":
                m = _ROLE_PATTERN.search(msg.content)
                if m:
                    return m.group(1)
    return ""


def _extract_budget(chat_records: List[ChatRecord]) -> str:
    """从聊天记录中抽取预算金额范围(如"500万"), 抽取不到返回空串。"""
    for record in chat_records:
        for msg in record.messages:
            if "预算" in msg.content:
                m = _BUDGET_PATTERN.search(msg.content)
                if m:
                    return m.group(1)
    return ""


def _extract_pain_points(text: str, neg_hits: Dict[str, int], churn_hits: Dict[str, int]) -> str:
    """从聊天文本中提炼核心痛点: 命中异议/流失关键词的句子(截取前2条)。"""
    sensitivity = set(neg_hits) | set(churn_hits)
    if not sensitivity:
        return ""
    sentences = [s.strip() for s in re.split(r"[。！？!?]", text) if s.strip()]
    points: List[str] = []
    for sent in sentences:
        if any(kw in sent for kw in sensitivity) and len(sent) <= 60:
            if sent not in points:
                points.append(sent)
        if len(points) >= 2:
            break
    return "；".join(points)


def _build_customer_profile(
    customer: Customer,
    chat_records: List[ChatRecord],
    pos_hits: Dict[str, int],
    neg_hits: Dict[str, int],
    churn_hits: Dict[str, int],
    price_agg: Optional[Dict[str, object]] = None,
) -> str:
    """确定性生成客户画像文本: 行业属性/决策角色/核心痛点/预算范围 + 沟通要点。

    该文本即 RAG 检索器 retrieve_top_sales 的 query_text 输入来源。

    增强: 预算范围按价格信号时间衰减标注「最新有效 / 过期(>7天)」。
    """
    sections: List[str] = []
    sections.append(
        f"【客户基础】企业:{customer.customer_name}; 行业:{customer.industry}; "
        f"城市:{customer.city}; 规模:{customer.scale}; 建档时间:{customer.create_time}。"
    )
    role = _extract_role(chat_records)
    if role:
        sections.append(f"【决策角色】主要沟通对象:{role}(采购/立项相关决策角色)。")
    pain = _extract_pain_points(_join_chat_text(chat_records), neg_hits, churn_hits)
    if pain:
        sections.append(f"【核心痛点】{pain}。")
    else:
        sections.append("【核心痛点】暂无明确异议, 痛点需进一步沟通挖掘。")

    # ---- 预算范围: 结合价格信号时间衰减 ----
    if price_agg and price_agg.get("latest_amount"):
        if price_agg["fresh"]:
            sections.append(
                f"【预算范围】最新有效意向价格:{price_agg['latest_amount']}"
                f"({price_agg['latest_days']}天前, 7天内有效)。"
            )
        else:
            sections.append(
                f"【预算范围】最新报价 {price_agg['latest_amount']} 已过期"
                f"({price_agg['latest_days']}天前, 超过{PRICE_FRESH_DAYS}天, 权重{price_agg['latest_weight']:.2f}), "
                f"可能非最新意向价格, 需重新确认。"
            )
        if price_agg.get("expired_count"):
            sections.append(
                f"【价格时间衰减】共{price_agg['total_count']}条价格信号, "
                f"{price_agg['expired_count']}条已过期(>7天)。"
            )
    else:
        budget = _extract_budget(chat_records)
        if budget:
            sections.append(f"【预算范围】客户沟通中提及预算约:{budget}。")
        else:
            sections.append("【预算范围】未明确金额, 需在跟进中确认预算空间。")

    if pos_hits:
        sections.append(f"【意向信号】命中:{'/'.join(_list_hit_keywords(pos_hits))}。")
    if neg_hits:
        sections.append(f"【异议信号】命中:{'/'.join(_list_hit_keywords(neg_hits))}。")
    if churn_hits:
        sections.append(f"【流失信号】命中:{'/'.join(_list_hit_keywords(churn_hits))}。")
    return "\n".join(sections)


def _build_core_demands(
    pos_hits: Dict[str, int],
    neg_hits: Dict[str, int],
    churn_hits: Dict[str, int],
) -> List[str]:
    """按命中关键词映射生成 3-5 条核心需求话术(确定性)。"""
    demands: List[str] = []
    # 优先级: 意向加分 → 意向减分 → 流失加分(词典顺序)
    for hit_map in (pos_hits, neg_hits, churn_hits):
        for kw in hit_map:
            phrase = _DEMAND_TEMPLATES.get(kw)
            if phrase and phrase not in demands:
                demands.append(phrase)
    return _normalize_demands(demands)


def _build_follow_up_suggestion(intention_level: str, churn_risk: str) -> str:
    """按意向等级生成跟进建议, 并按流失风险叠加补充建议。"""
    main = _SUGGESTION_BY_INTENTION.get(intention_level, _SUGGESTION_BY_INTENTION["中"])
    adjust = _CHURN_ADJUST_SUGGESTION.get(churn_risk, "")
    return f"{main}{adjust}" if adjust else main


def _fallback_to_rules(customer: Customer, chat_records: List[ChatRecord]) -> AnalysisResult:
    """规则引擎兜底: 不调 LLM, 关键词打分 + 价格时间衰减确定性生成结果(可复现)。

    增强点(对话上下文理解 + 价格时间衰减):
        1. 按角色统计信号: 客户说的话比销售的话更可信(客户主动表达的意向/价格
           才是真实信号), 因此在关键词计数时对客户侧消息加权;
        2. 价格信号按 chat_time 做 7 天新鲜度衰减 —— 超过 7 天的报价视为
           "过期意向价格", 降权参与意向评分, 并在画像/原因中显式标注。
    """
    text = _join_chat_text(chat_records)
    pos_hits = _count_keywords(text, INTENT_POSITIVE_KEYWORDS)
    neg_hits = _count_keywords(text, INTENT_NEGATIVE_KEYWORDS)
    churn_hits = _count_keywords(text, CHURN_POSITIVE_KEYWORDS)

    # ---- 增强1: 按角色区分信号(客户说的更可信) ----
    # 客户侧文本只拼「客户」角色的消息, 用于额外加权统计
    customer_text = _join_customer_text(chat_records)
    customer_pos = _count_keywords(customer_text, INTENT_POSITIVE_KEYWORDS)
    customer_neg = _count_keywords(customer_text, INTENT_NEGATIVE_KEYWORDS)
    customer_churn = _count_keywords(customer_text, CHURN_POSITIVE_KEYWORDS)

    # 客户主动表达的积极信号 +1 加权(意向真实性强于销售话术)
    pos_total = sum(pos_hits.values()) + sum(customer_pos.values())
    neg_total = sum(neg_hits.values()) + sum(customer_neg.values())
    churn_total = sum(churn_hits.values()) + sum(customer_churn.values())

    # ---- 增强2: 价格信号时间衰减 ----
    price_signals = _extract_price_signal(chat_records)
    price_agg = _aggregate_price_signals(price_signals)
    # 价格信号按衰减权重折算计入意向分: 最新价格(权重1.0)计入 1 次积极信号
    # 的强度, 过期价格按衰减权重打折(降权参与, 不彻底忽略)。
    price_score = price_agg["latest_weight"] if price_signals else 0.0

    # 意向分 = 加分 - 减分 - 流失分 + 价格新鲜度加权(浮点, 保留价格信号的影响)
    intention_score = float(pos_total - neg_total - churn_total) + price_score * PRICE_SIGNAL_WEIGHT
    intention_level = _score_to_intention_level(round(intention_score))
    churn_level = _score_to_churn_level(churn_total)

    # 原因文本: 命中明细 + 得分 + 判定
    def _hit_detail(hits: Dict[str, int]) -> str:
        return "/".join(f"{kw}x{n}" for kw, n in hits.items()) if hits else "无"

    # 价格信号详情(供原因溯源)
    price_note = ""
    if price_signals:
        if price_agg["fresh"]:
            price_note = (
                f"价格信号: 最新报价 {price_agg['latest_amount']}"
                f"({price_agg['latest_days']}天前, 有效, 权重{price_agg['latest_weight']:.2f})"
            )
        else:
            price_note = (
                f"价格信号: 最新报价 {price_agg['latest_amount']} 已过期"
                f"({price_agg['latest_days']}天前 > {PRICE_FRESH_DAYS}天, "
                f"降权至{price_agg['latest_weight']:.2f})"
            )
        if price_agg["expired_count"]:
            price_note += f"; 共{price_agg['total_count']}条价格信号, {price_agg['expired_count']}条过期"

    intention_reason = (
        f"意向信号命中 {pos_total} 次({_hit_detail(pos_hits)}), "
        f"异议信号命中 {neg_total} 次({_hit_detail(neg_hits)}), "
        f"流失信号命中 {churn_total} 次({_hit_detail(churn_hits)}); "
        f"意向得分 = {pos_total}-{neg_total}-{churn_total}+价格加权{price_score:.2f} = {intention_score:.2f}, "
        f"判定意向等级为「{intention_level}」。"
    )
    if price_note:
        intention_reason += f" {price_note}。"

    churn_reason = (
        f"流失信号命中 {churn_total} 次({_hit_detail(churn_hits)}), "
        f"流失得分 = {churn_total}, 判定流失风险为「{churn_level}」。"
    )

    result = AnalysisResult(
        customer_profile=_build_customer_profile(
            customer, chat_records, pos_hits, neg_hits, churn_hits, price_agg,
        ),
        intention_level=intention_level,
        intention_reason=intention_reason,
        core_demands=_build_core_demands(pos_hits, neg_hits, churn_hits),
        churn_risk=churn_level,
        churn_reason=churn_reason,
        follow_up_suggestion=_build_follow_up_suggestion(intention_level, churn_level),
    )
    logger.info("规则引擎生成画像: customer_id=%s, 意向=%s, 流失=%s, 价格信号=%d条(最新%0.2f)",
                customer.customer_id, result.intention_level, result.churn_risk,
                len(price_signals), price_agg["latest_weight"])
    return result


# ============================================================
# LLM 引擎(可选依赖, 延迟导入; 失败即降级规则引擎)
# ============================================================


def _llm_enabled() -> bool:
    """判断是否启用 LLM 引擎: 当前 provider 已配置 key 即启用。

    未配置 key 时自动降级规则引擎(零依赖、可复现, 开箱即跑)。
    """
    from modules import llm_client
    return llm_client.enabled()


def _analyze_with_llm(customer: Customer, chat_records: List[ChatRecord]) -> AnalysisResult:
    """LLM 引擎: 调 Kimi K2.7 Code 强制 JSON 输出画像分析。

    Args:
        customer: 客户模型。
        chat_records: 该客户的会话记录列表。

    Returns:
        AnalysisResult: LLM 解析校验通过的分析结果。

    Raises:
        Exception: 网络/超时/格式/校验失败 —— 由调用方(降级逻辑)兜底。
    """
    from modules import llm_client

    # 价格时间衰减分析(规则层预计算, 注入给 LLM 参考, 保证时间语义一致)
    price_signals = _extract_price_signal(chat_records)
    price_agg = _aggregate_price_signals(price_signals)

    # user 消息 = 客户基础信息 JSON + 聊天记录 JSON + 价格时间衰减分析
    payload = {
        "客户基础信息": customer.model_dump(),
        "聊天记录": [record.model_dump() for record in chat_records],
        "价格时间衰减分析": price_agg if price_signals else None,
    }
    data = llm_client.chat_json(
        SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False),
        max_tokens=2000,
        temperature=0.2,
    )
    result = AnalysisResult.model_validate(data)   # 枚举/字段校验失败 → 抛异常降级
    # 防御性规范: 保证 core_demands 落在 3-5 条契约范围内
    result.core_demands = _normalize_demands(result.core_demands)
    logger.info("LLM 引擎生成画像: customer_id=%s, 意向=%s, 流失=%s",
                customer.customer_id, result.intention_level, result.churn_risk)
    return result


# ============================================================
# 对外接口
# ============================================================


def analyze_customer(customer: Customer, chat_records: List[ChatRecord]) -> AnalysisResult:
    """分析单个客户: LLM 引擎优先, 失败/未配置自动降级规则引擎, 保证不中断。

    Args:
        customer: 客户模型(Customer)。
        chat_records: 该客户的会话记录列表(可为空列表)。

    Returns:
        AnalysisResult: 客户画像分析结果(画像/意向/需求/流失/跟进建议)。
    """
    if _llm_enabled():
        try:
            return _analyze_with_llm(customer, chat_records)
        except Exception as exc:  # noqa: BLE001 —— 任何失败都降级规则引擎
            logger.error("客户 %s LLM 分析失败(%s), 降级规则引擎",
                         customer.customer_id, exc)
    return _fallback_to_rules(customer, chat_records)


def analyze_customers_batch(
    customers: List[Customer],
    chat_map: Dict[str, List[ChatRecord]],
) -> Dict[str, AnalysisResult]:
    """批量分析客户: 单条失败不中断, 打印错误日志并降级规则引擎。

    启用 LLM 时并发调用(线程池), 显著缩短大批量画像分析的耗时
    (Kimi 单次响应约 10s, 串行 9 家需 90s+, 并发后约等于单次耗时)。

    Args:
        customers: 客户模型列表。
        chat_map: 以 customer_id 为键的会话记录分组(可由 data_loader.build_chat_map 生成)。

    Returns:
        dict[str, AnalysisResult]: {customer_id: 该客户的分析结果}。
    """
    results: Dict[str, AnalysisResult] = {}

    def _analyze_one(customer: Customer) -> None:
        records = chat_map.get(customer.customer_id, [])
        try:
            results[customer.customer_id] = analyze_customer(customer, records)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整批
            logger.error("客户 %s 分析异常(%s), 使用规则引擎兜底结果",
                         customer.customer_id, exc)
            results[customer.customer_id] = _fallback_to_rules(customer, records)

    if _llm_enabled():
        # LLM 模式: 并发分析(线程池), 大幅缩短总耗时
        from concurrent.futures import ThreadPoolExecutor
        max_workers = min(8, max(1, len(customers)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_analyze_one, customers))
    else:
        for customer in customers:
            _analyze_one(customer)

    logger.info("批量画像分析完成: 共 %d 家客户", len(results))
    return results