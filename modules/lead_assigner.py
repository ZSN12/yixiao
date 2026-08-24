# -*- coding: utf-8 -*-
"""线索分配器(lead_assigner): 规则硬约束 + RAG 语义软排序 + 负载均衡 的混合分配决策。

职责:
    把"规则硬约束 + RAG 语义软排序"融合成最终分配决策(借鉴开源 LeadGenius 的
    "硬约束过滤 → 分数排序 → 负载均衡加权"三层设计), 产出可追溯的分配结果:
    match_reason 引用规则命中的点(行业/城市)与 RAG 命中的经验片段关键词,
    防"拍脑袋分配"、防把线索推给不相关销售, 也防编造分配理由。

决策流程(对每个无归属客户):
    1. 硬约束过滤(规则优先, 是硬约束):
       行业 ∈ 销售.good_at_industries 的销售 → 候选1;
       候选1为空时, 再考虑 城市 ∈ 销售.responsible_cities → 候选2;
       候选1/候选2 都为空 → 走 RAG 软匹配或兜底。
    2. RAG 软排序(语义参考):
       query = build_customer_query_text(客户基础信息 + 画像 customer_profile),
       调 rag_retriever.match_customer_to_sales(query, sales_list, experiences, top_k)
       得到 SalesMatch 列表(带分数与命中经验片段溯源)。
    3. 融合决策:
       优先选"同时命中规则候选 且 RAG 排名靠前"的销售;
       规则候选与 RAG Top1 一致 → 直接定;
       不一致 → 以规则候选为主 + RAG 分做参考(match_reason 中说明两者是否一致)。
    4. 负载均衡: 同分候选里选 current_load 最小者。
    5. 兜底: 规则无 + RAG 无 → 默认管理员(needs_human=True, 待人工二次分配)。

健壮性:
    - rag_retriever / profile_analyzer 均为可选依赖: try 导入 + 失败降级纯规则,
      保证"分析结果为空 / 检索器不可用 / 无经验语料"时模块照常产出结果;
    - 单个客户分配异常不中断整批(兜底默认管理员)。

Agent Memory 闭环(可选, t12 集成):
    - assign_leads_with_memory(...): 复用原 assign_leads 匹配逻辑后叠加记忆增强;
      分配前按客户 query 查 search_similar_memory —— 命中强记忆(相似度>0.25)时其
      correct_sales_id 作为候选加分(等同一次行业命中权重)与规则/RAG 竞争, 并把
      "命中历史强记忆(S002)" 写进 match_reason; 弱记忆仅轻加权(0.5 倍);
      分配完成后为"规则与 RAG Top1 一致"的高置信结果自动 write_weak_memory
      (confidence=0.9, decision=confirm, correct=sales_id), 形成
      "自动写弱记忆 → 人工复核 → 升级强记忆 → 复用影响分配"的可持续闭环。
    - submit_feedback(customer_id, correct_sales_id, note): 人工反馈接口(未来接
      FastAPI), 将该客户最近一条弱记忆升级为强记忆(修正→decision=correct,
      确认→decision=confirm); 无记忆时直接新建一条强记忆。
    - 原 assign_leads 签名与行为完全不变(向后兼容, 所有既有调用方不受影响);
      memory=None 时 assign_leads_with_memory 行为等同 assign_leads。

依赖方向(单向, 与全项目一致):
    lead_assigner → rag_retriever 吃 profile_analyzer 产出的 customer_profile 文本;
    记忆可选依赖 agent_memory(t11), 懒加载降级; 本模块不反向依赖调度/推送模块。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from modules.data_loader import Customer, Sales, SalesExperience

logger = logging.getLogger(__name__)

# ---- 可选依赖: 宽松导入, 失败降级(纯规则) ----
try:
    from modules.profile_analyzer import AnalysisResult  # noqa: F401 —— 仅类型标注用
except Exception:  # noqa: BLE001 —— 画像分析模块未就绪时降级
    AnalysisResult = None  # type: ignore[assignment]

try:
    from modules.rag_retriever import SalesMatch  # noqa: F401 —— 仅类型标注用
    from modules.rag_retriever import match_customer_to_sales as _match_customer_to_sales
    RAG_AVAILABLE: bool = True
except Exception:  # noqa: BLE001 —— 检索器不可用时降级纯规则
    SalesMatch = None  # type: ignore[assignment]
    _match_customer_to_sales = None  # type: ignore[assignment]
    RAG_AVAILABLE = False
    logger.warning("rag_retriever 导入失败, 线索分配降级为纯规则模式")

# ---- Agent Memory 记忆模块(可选依赖; 缺失时记忆功能整体降级) ----
# 说明: agent_memory 是 t11 交付的记忆存储层。本模块不硬依赖它:
# - assign_leads_with_memory / submit_feedback 运行时懒加载; 未安装/导入失败时
#   自动降级为"无记忆"等价行为, 绝不抛异常打断调用方。
try:
    from modules import agent_memory as _agent_memory_module
    from modules.agent_memory import MemoryEntry  # noqa: F401 —— 供返回注解使用
    _MEMORY_AVAILABLE: bool = True
except Exception:  # noqa: BLE001
    _agent_memory_module = None  # type: ignore[assignment]
    MemoryEntry = None  # type: ignore[assignment]
    _MEMORY_AVAILABLE = False
    logger.warning("agent_memory 导入失败, 记忆增强功能不可用(分配器仍然正常工作)")

# 记忆增强参数(对齐 rag_retriever 的行业命中权重 0.15; 弱记忆轻加权 0.5 倍)
MEMORY_STRONG_BONUS: float = 0.15    # 强记忆命中 ≈ 等同一次行业命中权重
MEMORY_WEAK_BONUS: float = 0.075     # 弱记忆轻加权(0.5 倍)
MEMORY_STRONG_SIM_THRESHOLD: float = 0.25  # 强记忆相似度门槛
MEMORY_HIT_EFFECT: str = "历史记忆"  # match_reason 中的溯源标签

# 兜底销售(规则无 + RAG 无时的默认归属)
FALLBACK_SALES_ID = "admin"
FALLBACK_SALES_NAME = "默认管理员"

# 经验片段年份/动作前缀(摘要时去掉, 如 "2024年中跟进南京..." -> "南京...")
_YEAR_PREFIX = re.compile(
    r"^20\d{2}(?:年初|年中|下半年|年|年底|底)?\s*(?:中?跟进|初?跟进|服务|深耕|参与)?\s*"
)
# 经验摘要的最大长度(超长截断加省略号)
_EXPERIENCE_SNIPPET_MAX = 26


# ============================================================
# 输出模型(全团队共享契约, 字段精确)
# ============================================================


class AssignmentResult(BaseModel):
    """单个客户的线索分配结果 —— 全团队共享的分配产物契约。"""

    customer_id: str
    customer_name: str
    sales_id: str
    sales_name: str
    match_reason: str            # 中文可追溯理由(引用规则与 RAG 依据)
    rag_score: float = 0.0       # RAG 匹配分(来自 SalesMatch.score, 无则 0)
    rule_matched: bool = True    # 是否命中规则(行业/城市任一)
    needs_human: bool = False    # 是否需要人工二次分配


# ============================================================
# 通用工具
# ============================================================


def build_customer_query_text(
    customer: Customer,
    analysis_result: Optional[Any] = None,
) -> str:
    """把客户基础信息 + 画像文本拼成检索 query(供 match_customer_to_sales 使用)。

    Args:
        customer: 客户模型(Customer)。
        analysis_result: 该客户的画像分析结果(AnalysisResult 或含 customer_profile
                         字段的对象/dict), 可空 —— 为空时只用客户基础信息。

    Returns:
        str: 拼接后的检索文本, 形如
             "【客户基础】企业:xxx; 行业:智能制造; 城市:苏州; 规模:中大型。\n【客户画像】..."
    """
    parts: List[str] = [
        f"【客户基础】企业:{customer.customer_name}; 行业:{customer.industry}; "
        f"城市:{customer.city}; 规模:{customer.scale}; 建档时间:{customer.create_time}。"
    ]
    profile = ""
    if analysis_result is not None:
        if isinstance(analysis_result, dict):
            profile = str(analysis_result.get("customer_profile") or "")
        else:
            profile = str(getattr(analysis_result, "customer_profile", "") or "")
    if profile:
        parts.append(f"【客户画像】{profile}")
    return "\n".join(parts)


def _summarize_experience(
    content: str,
    exp_by_content: Dict[str, SalesExperience],
) -> str:
    """从经验片段内容提炼可追溯的短摘要(去年份/动作前缀, 取首分句, 附结果标记)。

    Args:
        content: 经验片段内容(通常来自 SalesMatch.matched_experiences)。
        exp_by_content: {experience.content: SalesExperience} 映射, 用于补充结果标记。

    Returns:
        str: 短摘要, 如 "杭州某新能源电池厂(约300人)·成单"; 内容缺失时
             返回 "未命中具体经验片段"。
    """
    if not content:
        return "未命中具体经验片段"
    text = _YEAR_PREFIX.sub("", content).strip()
    clause = re.split(r"[，。;；]", text, maxsplit=1)[0].strip()
    if len(clause) > _EXPERIENCE_SNIPPET_MAX:
        clause = clause[:_EXPERIENCE_SNIPPET_MAX] + "…"
    exp = exp_by_content.get(content)
    if exp is not None and exp.outcome:
        clause = f"{clause}·{exp.outcome}"
    return clause


def _pick_rule_candidate(
    rule_candidates: List[Sales],
    rag_by_id: Dict[str, Any],
) -> Sales:
    """从规则候选中选最终销售: 优先级 = 同时命中RAG候选 → RAG分高 → 当前负载小。

    Args:
        rule_candidates: 规则候选销售列表(候选1 或 候选2)。
        rag_by_id: {sales_id: SalesMatch} 的 RAG 匹配映射。

    Returns:
        Sales: 最终选择的销售。
    """
    def _key(sales: Sales):
        match = rag_by_id.get(sales.sales_id)
        if match is not None:
            return (0, -float(match.score), sales.current_load)
        return (1, 0.0, sales.current_load)

    return min(rule_candidates, key=_key)


def _pick_rag_candidate(
    rag_matches: List[SalesMatch],
    sales_by_id: Dict[str, Sales],
) -> SalesMatch:
    """RAG 候选为主时选最终销售: RAG 分高优先, 同分取当前负载最小者。

    Args:
        rag_matches: RAG 匹配列表(按分降序)。
        sales_by_id: {sales_id: Sales} 映射。

    Returns:
        SalesMatch: 最终选择的匹配项。
    """
    def _key(match: SalesMatch):
        sales = sales_by_id.get(match.sales_id)
        load = sales.current_load if sales is not None else 0
        return (-float(match.score), load)

    return min(rag_matches, key=_key)


def _build_rule_reason(
    rule_basis: str,
    customer: Customer,
    chosen: Sales,
    chosen_rag: Optional[SalesMatch],
    rag_top1: Optional[SalesMatch],
    exp_by_content: Dict[str, SalesExperience],
) -> str:
    """构造"规则命中"路径的可追溯分配理由(match_reason)。"""
    parts: List[str] = []
    # 1) 规则命中的点(硬约束依据)
    if rule_basis == "行业":
        parts.append(f"行业匹配({customer.industry})")
    else:
        parts.append(f"城市匹配({customer.city})")
    # 2) RAG 语义依据 + 规则/RAG 一致性说明
    if chosen_rag is not None:
        snippet = (
            _summarize_experience(chosen_rag.matched_experiences[0], exp_by_content)
            if chosen_rag.matched_experiences
            else "未命中具体经验片段"
        )
        if rag_top1 is not None and chosen.sales_id == rag_top1.sales_id:
            parts.append(f"RAG语义匹配(相似经验: {snippet})且与RAG Top1一致")
        elif rag_top1 is not None:
            parts.append(
                f"RAG语义匹配(相似经验: {snippet}); RAG Top1为{rag_top1.sales_id}"
                f"{rag_top1.sales_name or ''}, 与规则不一致, 规则硬约束优先"
            )
        else:
            parts.append(f"RAG语义匹配(相似经验: {snippet})")
    # 3) 负载均衡依据
    parts.append(f"负载均衡(当前负载{chosen.current_load})")
    return " + ".join(parts) + f", 推荐{chosen.sales_id}{chosen.name}"


def _build_rag_reason(
    customer: Customer,
    chosen: SalesMatch,
    sales_by_id: Dict[str, Sales],
    exp_by_content: Dict[str, SalesExperience],
) -> str:
    """构造"规则未命中、RAG 语义匹配"路径的可追溯分配理由(match_reason)。"""
    sales = sales_by_id.get(chosen.sales_id)
    sales_name = sales.name if sales is not None else ""
    load = sales.current_load if sales is not None else 0
    snippet = (
        _summarize_experience(chosen.matched_experiences[0], exp_by_content)
        if chosen.matched_experiences
        else "未命中具体经验片段"
    )
    return (
        f"规则未命中(行业{customer.industry}/城市{customer.city}均无覆盖销售) + "
        f"RAG语义匹配(相似经验: {snippet}) + 负载均衡(当前负载{load}), "
        f"推荐{chosen.sales_id}{sales_name}"
    )


# ============================================================
# 主流程
# ============================================================


def assign_leads(
    unassigned_customers: List[Customer],
    sales_list: List[Sales],
    experiences: List[SalesExperience],
    analysis_results: Optional[Dict[str, AnalysisResult]] = None,
    top_k: int = 5,
) -> List[AssignmentResult]:
    """混合线索分配主入口: 规则硬约束 + RAG 语义软排序 + 负载均衡。

    Args:
        unassigned_customers: 无归属客户列表(owner_sales_id 为空); 若混入已归属
                              客户将被跳过并记日志。
        sales_list: 销售人员列表(load_sales 结果, 常含兜底销售 admin)。
        experiences: 销售经验片段列表(load_sales_experiences 结果, 可为空)。
        analysis_results: {customer_id: AnalysisResult} 画像分析结果, 可空
                          (为空时内部用客户基础信息拼 query)。
        top_k: RAG 检索返回候选数, 默认 5; 非法(<=0)时回落默认值。

    Returns:
        list[AssignmentResult]: 每个输入客户一条分配结果(顺序与输入一致,
                已归属客户跳过; 规则无 + RAG 无时分配默认管理员并需人工介入)。
    """
    results: List[AssignmentResult] = []
    if top_k is None or top_k <= 0:
        top_k = 5

    sales_by_id: Dict[str, Sales] = {s.sales_id: s for s in sales_list or []}
    exp_by_content: Dict[str, SalesExperience] = {e.content: e for e in experiences or []}
    analysis_results = analysis_results or {}

    for customer in unassigned_customers or []:
        # 防御: 只分配无归属客户
        if customer.owner_sales_id:
            logger.warning("客户 %s 已有归属(%s), 跳过分配",
                           customer.customer_id, customer.owner_sales_id)
            continue
        try:
            results.append(_assign_one(
                customer=customer,
                sales_list=sales_list or [],
                experiences=experiences or [],
                exp_by_content=exp_by_content,
                sales_by_id=sales_by_id,
                analysis_results=analysis_results,
                top_k=top_k,
            ))
        except Exception as exc:  # noqa: BLE001 —— 单客户分配异常不中断整批
            logger.error("客户 %s 分配异常(%s), 兜底默认管理员", customer.customer_id, exc)
            results.append(AssignmentResult(
                customer_id=customer.customer_id,
                customer_name=customer.customer_name,
                sales_id=FALLBACK_SALES_ID,
                sales_name=FALLBACK_SALES_NAME,
                match_reason="分配过程异常, 待人工二次分配",
                rag_score=0.0,
                rule_matched=False,
                needs_human=True,
            ))

    logger.info("线索分配完成: 输入 %d 家, 产出 %d 条结果",
                len(unassigned_customers or []), len(results))
    return results


# ============================================================
# 内部: 单客户匹配(规则+RAG, 无记忆)与结果构造(供记忆路径复用)
# ============================================================


def _match_customer(
    customer: Customer,
    sales_list: List[Sales],
    experiences: List[SalesExperience],
    exp_by_content: Dict[str, SalesExperience],
    sales_by_id: Dict[str, Sales],
    analysis_results: Dict[str, AnalysisResult],
    top_k: int,
) -> Dict[str, Any]:
    """单客户匹配中间结果(规则候选 / RAG 软排序 / 融合选择, 不含记忆)。

    记忆路径(assign_leads_with_memory)复用本函数拿到基础匹配后叠加记忆增强,
    保证两条路径的规则/RAG 逻辑完全一致(单一来源)。
    """
    # ---- 1) 硬约束过滤(规则优先): 行业候选1 → 城市候选2 ----
    candidate1 = [
        s for s in sales_list
        if s.sales_id != FALLBACK_SALES_ID and customer.industry in s.good_at_industries
    ]
    if candidate1:
        rule_candidates: List[Sales] = candidate1
        rule_basis: str = "行业"
    else:
        candidate2 = [
            s for s in sales_list
            if s.sales_id != FALLBACK_SALES_ID and customer.city in s.responsible_cities
        ]
        rule_candidates = candidate2
        rule_basis = "城市"

    # ---- 2) RAG 软排序(语义参考; 无经验语料/检索器不可用时跳过) ----
    query_text = build_customer_query_text(
        customer, analysis_results.get(customer.customer_id)
    )
    rag_matches: List[SalesMatch] = []
    if RAG_AVAILABLE and _match_customer_to_sales is not None and experiences and query_text.strip():
        try:
            rag_matches = list(_match_customer_to_sales(
                query_text, sales_list, experiences, top_k=top_k
            ) or [])
        except Exception as exc:  # noqa: BLE001 —— 检索失败降级(规则或兜底)
            logger.warning("客户 %s RAG 检索失败(%s), 降级", customer.customer_id, exc)
            rag_matches = []
    rag_by_id: Dict[str, SalesMatch] = {m.sales_id: m for m in rag_matches}
    rag_top1 = rag_matches[0] if rag_matches else None

    # ---- 3)+4) 融合决策 + 负载均衡 ----
    chosen: Optional[Sales] = None
    chosen_rag: Optional[SalesMatch] = None
    rag_chosen: Optional[SalesMatch] = None
    if rule_candidates:
        chosen = _pick_rule_candidate(rule_candidates, rag_by_id)
        chosen_rag = rag_by_id.get(chosen.sales_id)
    elif rag_matches:
        rag_chosen = _pick_rag_candidate(rag_matches, sales_by_id)

    return {
        "customer": customer,
        "query_text": query_text,
        "sales_by_id": sales_by_id,
        "rule_candidates": rule_candidates,
        "rule_basis": rule_basis,           # "行业" | "城市"
        "rag_matches": rag_matches,
        "rag_by_id": rag_by_id,
        "rag_top1": rag_top1,
        "chosen": chosen,                    # 规则命中的最终销售(None 表示规则未命中)
        "chosen_rag": chosen_rag,            # chosen 对应的 RAG 匹配(可能无)
        "rag_chosen": rag_chosen,            # 规则未命中时按 RAG 选的匹配(可能无)
    }


def _build_result_from_match(
    match: Dict[str, Any],
    exp_by_content: Dict[str, SalesExperience],
    memory_desc: Optional[str] = None,
) -> AssignmentResult:
    """把匹配中间结果(可含记忆增强说明)构造成 AssignmentResult。

    Args:
        match: _match_customer 返回的中间结果; 若调用方已应用记忆增强,
               可替换其中的 chosen / rag_chosen。
        exp_by_content: 经验内容映射(溯源摘要用)。
        memory_desc: 可选的记忆溯源说明, 追加在 match_reason 中, None 不加。
    """
    customer: Customer = match["customer"]
    rule_candidates: List[Sales] = match["rule_candidates"]
    rag_top1: Optional[SalesMatch] = match["rag_top1"]
    memory_part = f" + {memory_desc}" if memory_desc else ""

    if rule_candidates:
        chosen: Sales = match["chosen"]
        chosen_rag: Optional[SalesMatch] = match["chosen_rag"]
        reason = _build_rule_reason(
            match["rule_basis"], customer, chosen, chosen_rag, rag_top1, exp_by_content
        ) + memory_part
        return AssignmentResult(
            customer_id=customer.customer_id,
            customer_name=customer.customer_name,
            sales_id=chosen.sales_id,
            sales_name=chosen.name,
            match_reason=reason,
            rag_score=round(float(chosen_rag.score), 4) if chosen_rag else 0.0,
            rule_matched=True,
            needs_human=False,
        )
    if match["rag_chosen"] is not None:
        chosen_m: SalesMatch = match["rag_chosen"]
        sales_by_id: Dict[str, Sales] = match["sales_by_id"]
        sales_name = sales_by_id.get(chosen_m.sales_id, Sales(
            sales_id=chosen_m.sales_id, name="未知", good_at_industries=[],
            responsible_cities=[], current_load=0,
        )).name
        return AssignmentResult(
            customer_id=customer.customer_id,
            customer_name=customer.customer_name,
            sales_id=chosen_m.sales_id,
            sales_name=sales_name,
            match_reason=_build_rag_reason(
                customer, chosen_m, sales_by_id, exp_by_content
            ) + memory_part,
            rag_score=round(float(chosen_m.score), 4),
            rule_matched=False,
            needs_human=False,
        )
    # 兜底: 规则无 + RAG 无
    return AssignmentResult(
        customer_id=customer.customer_id,
        customer_name=customer.customer_name,
        sales_id=FALLBACK_SALES_ID,
        sales_name=FALLBACK_SALES_NAME,
        match_reason="无匹配销售，待人工二次分配",
        rag_score=0.0,
        rule_matched=False,
        needs_human=True,
    )


# ============================================================
# Agent Memory 闭环: 记忆增强分配 + 人工反馈接口
# ============================================================


def _memory_similarity(query_text: str, memory_query_text: str) -> float:
    """计算 query 与记忆库 query 的相似度(2-gram Jaccard, 供记忆命中判断)。

    Args:
        query_text: 当前客户的检索 query 文本。
        memory_query_text: 记忆条目中记录的 query 文本。

    Returns:
        float: 相似度 0~1。
    """
    try:
        from modules.rag_retriever import local_similarity
        return local_similarity(query_text, memory_query_text)
    except Exception:  # noqa: BLE001 —— rag 不可用时用本地复刻的 Jaccard 兜底
        grams_a = {query_text[i:i + 2] for i in range(max(0, len(query_text) - 1))}
        grams_b = {memory_query_text[i:i + 2] for i in range(max(0, len(memory_query_text) - 1))}
        union = grams_a | grams_b
        return (len(grams_a & grams_b) / len(union)) if union else 0.0


def _memory_hint(
    memory,
    query_text: str,
) -> Dict[str, Any]:
    """查询记忆库, 返回针对该 query 的记忆提示(强/弱记忆命中, 含相似度)。

    Args:
        memory: agent_memory 模块绑定(必须提供 search_similar_memory)。
        query_text: 当前客户的检索 query 文本。

    Returns:
        dict: {"strong": MemoryEntry|None, "weak": MemoryEntry|None,
               "strong_sim": float, "weak_sim": float,
               "target_sales_id": str|None, "is_strong": bool}
        检索异常时返回全空/None 的占位, 不抛错。
    """
    empty: Dict[str, Any] = {
        "strong": None, "weak": None, "strong_sim": 0.0, "weak_sim": 0.0,
        "target_sales_id": None, "is_strong": False,
    }
    try:
        if not query_text or not query_text.strip():
            return empty
        hits = memory.search_similar_memory(query_text, top_k=3)
    except Exception as exc:  # noqa: BLE001 —— 记忆查询失败不影响分配
        logger.warning("记忆检索失败(%s), 跳过记忆增强", exc)
        return empty

    hits = hits or []
    strong, weak = None, None
    strong_sim, weak_sim = 0.0, 0.0
    for entry in hits:
        sim = _memory_similarity(query_text, entry.query_text)
        if entry.source == "strong" and strong is None and sim > MEMORY_STRONG_SIM_THRESHOLD:
            strong, strong_sim = entry, sim
        elif entry.source == "weak" and weak is None:
            weak, weak_sim = entry, sim
    if strong is not None:
        return {
            "strong": strong, "weak": weak, "strong_sim": strong_sim, "weak_sim": weak_sim,
            "target_sales_id": strong.correct_sales_id, "is_strong": True,
        }
    if weak is not None:
        return {
            "strong": None, "weak": weak, "strong_sim": 0.0, "weak_sim": weak_sim,
            "target_sales_id": weak.correct_sales_id, "is_strong": False,
        }
    return empty


def _pick_with_memory(
    rule_candidates: List[Sales],
    rag_by_id: Dict[str, Any],
    target_sales_id: Optional[str],
    bonus: float,
) -> Optional[Sales]:
    """候选选择(带记忆加分): 规则候选内, 记忆目标销售加 bonus 分参与排序。

    Args:
        rule_candidates: 规则候选销售列表。
        rag_by_id: {sales_id: SalesMatch}。
        target_sales_id: 记忆提示的目标销售 ID(可能不在规则候选内)。
        bonus: 记忆加分(强 0.15=等同行业命中; 弱 0.075=轻加权)。

    Returns:
        Sales|None: 最终选择; 无候选返回 None。
    """
    if not rule_candidates:
        return None

    def _key(sales: Sales):
        match = rag_by_id.get(sales.sales_id)
        rag_score = float(match.score) if match is not None else 0.0
        mem_bonus = bonus if sales.sales_id == target_sales_id else 0.0
        total = rag_score + mem_bonus
        return (-total, sales.current_load)

    return min(rule_candidates, key=_key)


def assign_leads_with_memory(
    unassigned_customers: List[Customer],
    sales_list: List[Sales],
    experiences: List[SalesExperience],
    analysis_results: Optional[Dict[str, AnalysisResult]] = None,
    top_k: int = 5,
    memory: Optional[Any] = None,
) -> List[AssignmentResult]:
    """记忆增强版线索分配(Agent Memory 闭环入口)。

    与 assign_leads 完全兼容: memory=None 时直接复用 assign_leads(行为等同原
    assign_leads); 传入 agent_memory 模块绑定后:
    1. 分配前: 对每个客户 query 调 search_similar_memory; 命中强记忆
       (相似度>0.25)时其 correct_sales_id 作为候选加分(等同一次行业命中权重,
       即 0.15)与规则/RAG Top1 竞争, 并把"命中历史强记忆(S002)"写进
       match_reason; 弱记忆仅轻加权(0.5 倍, 即 0.075);
    2. 分配后: 为"规则与 RAG Top1 一致"的高置信结果自动 write_weak_memory
       (confidence=0.9, decision=confirm, correct=sales_id)。

    Args:
        unassigned_customers / sales_list / experiences / analysis_results / top_k:
            同 assign_leads。
        memory: agent_memory 模块绑定(需含 search_similar_memory /
                init_memory_db / write_weak_memory); None=不用记忆,
                行为等同原 assign_leads。

    Returns:
        list[AssignmentResult]: 同 assign_leads; 记忆模块缺失或异常时自动降级
                为无记忆分配, 绝不抛错。
    """
    # 兼容: 未指定/不可用记忆模块 → 直接用原分配逻辑
    if memory is None or not hasattr(memory, "search_similar_memory"):
        if memory is None:
            return assign_leads(
                unassigned_customers, sales_list, experiences,
                analysis_results=analysis_results, top_k=top_k,
            )
        logger.warning("传入的 memory 对象缺少 search_similar_memory, 降级无记忆分配")
        return assign_leads(
            unassigned_customers, sales_list, experiences,
            analysis_results=analysis_results, top_k=top_k,
        )

    if top_k is None or top_k <= 0:
        top_k = 5
    # 确保记忆库表存在(幂等)
    if hasattr(memory, "init_memory_db"):
        try:
            memory.init_memory_db()
        except Exception as exc:  # noqa: BLE001
            logger.warning("初始化记忆库失败(%s), 降级无记忆分配", exc)
            return assign_leads(
                unassigned_customers, sales_list, experiences,
                analysis_results=analysis_results, top_k=top_k,
            )

    exp_by_content: Dict[str, SalesExperience] = {e.content: e for e in experiences or []}
    analysis_results = analysis_results or {}
    results: List[AssignmentResult] = []

    for customer in unassigned_customers or []:
        if customer.owner_sales_id:
            logger.warning("客户 %s 已有归属(%s), 跳过分配",
                           customer.customer_id, customer.owner_sales_id)
            continue
        try:
            match = _match_customer(
                customer, sales_list or [], experiences or [], exp_by_content,
                {s.sales_id: s for s in sales_list or []},
                analysis_results, top_k,
            )
            hint = _memory_hint(memory, match["query_text"])
            memory_desc: Optional[str] = None
            # 记忆增强: 目标销售合法且不兜底时, 才允许影响选择
            target = hint["target_sales_id"]
            target_ok = (
                target is not None
                and target != FALLBACK_SALES_ID
                and any(s.sales_id == target for s in (sales_list or []))
            )
            if target_ok and match["chosen"] is not None:
                bonus = MEMORY_STRONG_BONUS if hint["is_strong"] else MEMORY_WEAK_BONUS
                chosen = _pick_with_memory(
                    match["rule_candidates"], match["rag_by_id"], target, bonus,
                )
                if chosen is not None:
                    if chosen.sales_id == target:
                        memory_desc = (
                            f"命中历史强记忆({target}, 相似度{hint['strong_sim']:.2f})"
                            if hint["is_strong"]
                            else f"命中历史弱记忆({target}, 相似度{hint['weak_sim']:.2f}, 轻加权)"
                        )
                        if hint["is_strong"]:
                            memory_desc += " 与规则/RAG竞争后胜出"
                        else:
                            memory_desc += ", 规则为主打分参考"
                    match["chosen"] = chosen
                    match["chosen_rag"] = match["rag_by_id"].get(chosen.sales_id)
            elif target_ok and match["rag_chosen"] is not None and hint["is_strong"]:
                # 规则未命中, 但强记忆提示的销售有效 → 与 RAG Top1 竞争
                chosen = next(
                    (s for s in (sales_list or []) if s.sales_id == target), None
                )
                if chosen is not None:
                    memory_desc = (
                        f"命中历史强记忆({target}, 相似度{hint['strong_sim']:.2f})"
                        ", 规则未命中, 以历史记忆参考 RAG 重新选择"
                    )
                    # 比较: 记忆目标有加分(等同行业命中) vs RAG Top1 分
                    if hint["strong_sim"] >= MEMORY_STRONG_SIM_THRESHOLD and (
                        match["rag_top1"] is None
                        or hint["strong_sim"] > float(match["rag_top1"].score)
                    ):
                        match["rag_chosen"] = SalesMatch(
                            sales_id=target,
                            sales_name=chosen.name,
                            score=hint["strong_sim"],
                            similarity=hint["strong_sim"],
                            rule_bonus=0.0,
                            matched_experiences=[],
                            match_method="memory",
                        )

            # 记忆决策后构造结果
            result = _build_result_from_match(match, exp_by_content, memory_desc=memory_desc)
            results.append(result)

            # 分配后: 高置信(规则与 RAG Top1 一致)且非兜底 → 自动写弱记忆
            if (
                hasattr(memory, "write_weak_memory")
                and match["chosen"] is not None
                and match["rag_top1"] is not None
                and match["chosen"].sales_id == match["rag_top1"].sales_id
                and result.sales_id != FALLBACK_SALES_ID
                and hint["is_strong"] is False
            ):
                try:
                    entry = memory.MemoryEntry(
                        customer_id=customer.customer_id,
                        query_text=match["query_text"],
                        sales_id=result.sales_id,
                        decision="confirm",
                        correct_sales_id=result.sales_id,
                        confidence=0.9,
                    )
                    memory.write_weak_memory(entry)
                    logger.info("已自动写入弱记忆: customer=%s sales=%s",
                                customer.customer_id, result.sales_id)
                except Exception as exc:  # noqa: BLE001 —— 写记忆失败不影响分配
                    logger.warning("写入弱记忆失败(customer=%s): %s",
                                   customer.customer_id, exc)
        except Exception as exc:  # noqa: BLE001 —— 单客户异常不中断整批
            logger.error("客户 %s 记忆分配异常(%s), 兜底默认管理员", customer.customer_id, exc)
            results.append(AssignmentResult(
                customer_id=customer.customer_id,
                customer_name=customer.customer_name,
                sales_id=FALLBACK_SALES_ID,
                sales_name=FALLBACK_SALES_NAME,
                match_reason="分配过程异常, 待人工二次分配",
                rag_score=0.0,
                rule_matched=False,
                needs_human=True,
            ))

    logger.info("记忆增强分配完成: 输入 %d 家, 产出 %d 条结果",
                len(unassigned_customers or []), len(results))
    return results


def submit_feedback(
    customer_id: str,
    correct_sales_id: str,
    note: str = "",
    memory: Optional[Any] = None,
) -> Optional["MemoryEntry"]:
    """人工反馈接口(未来接 FastAPI): 把客户记忆升级/新建为强记忆。

    流程:
    1. 查该客户最近一条 weak 记忆(list_memories 按 created_at 倒序取第一);
    2. 有 weak 记忆:
       - correct_sales_id != 原推荐 sales_id → 升级为 strong(decision=correct);
       - correct_sales_id == 原推荐 sales_id   → 升级为 strong(decision=confirm);
    3. 无 weak 记忆 → 直接新建一条 strong(decision=correct, 人工指定)。

    Args:
        customer_id: 客户 ID。
        correct_sales_id: 人工确认/修正后的正确销售 ID。
        note: 人工备注(可选)。
        memory: agent_memory 模块绑定; None 时用模块级懒加载的绑定
                (导入失败返回 None 不抛错)。

    Returns:
        Optional[MemoryEntry]: 写入/升级后的强记忆条目; agent_memory 不可用时
                记日志返回 None(调用方决定是否报错提示用户)。
    """
    mem_mod = memory if memory is not None else _agent_memory_module
    if mem_mod is None or not hasattr(mem_mod, "upgrade_to_strong"):
        logger.error("submit_feedback: agent_memory 不可用, 无法记录人工反馈"
                     "(customer=%s, correct=%s)", customer_id, correct_sales_id)
        return None

    try:
        # 1) 查该客户最近一条 weak 记忆(倒序取第一条同客户 weak)
        entries = mem_mod.list_memories(limit=500) if hasattr(mem_mod, "list_memories") else []
        weak_entry = next(
            (e for e in entries
             if e.customer_id == customer_id and getattr(e, "source", "") == "weak"),
            None,
        )
        if weak_entry is not None:
            # 2) 按人工结论修正 decision/correct_sales_id 后升级为强记忆
            #    清空 memory_id/created_at 让 upgrade 生成新 id —— 保持弱记忆
            #    与强记忆并存(agent_memory.upgrade_to_strong 的语义: 升级不影响
            #    该客户已有的 weak 记忆, 两者各留一条)。
            is_correct = weak_entry.correct_sales_id != correct_sales_id
            weak_entry.decision = "correct" if is_correct else "confirm"
            weak_entry.correct_sales_id = correct_sales_id
            weak_entry.memory_id = ""
            weak_entry.created_at = ""
            upgraded = mem_mod.upgrade_to_strong(weak_entry, feedback_note=note)
            logger.info("人工反馈: customer=%s weak→strong(decision=%s, correct=%s)",
                        customer_id, upgraded.decision, upgraded.correct_sales_id)
            return upgraded

        # 3) 无 weak 记忆 → 直接新建强记忆(人工指定)
        entry = mem_mod.MemoryEntry(
            customer_id=customer_id,
            query_text=f"[人工反馈] 客户{customer_id} 由人工指定归属 {correct_sales_id}",
            sales_id=correct_sales_id,
            decision="correct",
            correct_sales_id=correct_sales_id,
            confidence=1.0,
            feedback_note=note,
        )
        created = mem_mod.upgrade_to_strong(entry)
        logger.info("人工反馈(无弱记忆): customer=%s 新建 strong, correct=%s",
                    customer_id, correct_sales_id)
        return created
    except Exception as exc:  # noqa: BLE001 —— 反馈登记异常不让调用方崩溃
        logger.error("人工反馈登记失败(customer=%s): %s", customer_id, exc)
        return None


def _assign_one(
    customer: Customer,
    sales_list: List[Sales],
    experiences: List[SalesExperience],
    exp_by_content: Dict[str, SalesExperience],
    sales_by_id: Dict[str, Sales],
    analysis_results: Dict[str, AnalysisResult],
    top_k: int,
) -> AssignmentResult:
    """对单个客户执行完整分配(硬约束 → RAG → 融合 → 负载均衡 → 兜底)。"""
    match = _match_customer(
        customer, sales_list, experiences, exp_by_content, sales_by_id,
        analysis_results, top_k,
    )
    return _build_result_from_match(match, exp_by_content)