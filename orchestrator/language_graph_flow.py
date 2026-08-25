# -*- coding: utf-8 -*-
"""LangGraph 多智能体调度层(language_graph_flow): 把流水线建模为状态图。

设计(面试亮点):
- 用 langgraph StateGraph 把"每日定时 → 画像分层 → RAG 匹配 → 钉钉触达"闭环
  建模为三个专职智能体节点, 共享同一个状态对象(State), 节点间只通过状态流转:
    分析师(analyst / image_profiling)  ->  匹配师(matcher / lead_matching)  ->  推送员(pusher / report_push)
    最后接一个汇总节点(summarize)产出全流水线摘要。
- 节点内调用模型一律直接用 openai SDK(配置来自 settings.LLM_API_BASE/KEY/MODEL),
  不引入 langchain-openai/langchain 全家桶 —— 这正是本项目为解决
  "openai 3.x 与 requirements 锁定的 openai==1.51.0 冲突" 定下的技术方案:
  模型调用只依赖低层 SDK, 上层纯用 LangGraph 的状态图编排。
  mock_mode=True(或 LLM_API_KEY 为空)时, 节点内全部走规则引擎/模块内置降级,
  不花一分钱、开箱即跑; 任何 LLM 调用失败自动降级规则, 绝不中断整条流水线。

健壮性(逐条落实):
1. langgraph 为可选依赖: 导入失败时 LANGUAGE_GRAPH_AVAILABLE=False,
   run_pipeline_graph 直接走"按顺序直调各模块"的降级路径;
2. 图编译(compile)或调用(invoke)异常同样捕获, 降级为顺序执行 ——
   保证 main.py 全流程不依赖 langgraph 可用性;
3. 节点内单个环节失败(分析/分配/推送)记入 state["errors"], 不中断整图。

状态说明:
- 状态用 TypedDict 声明(langgraph 标准做法), 节点收到的是普通 dict(只含
  已写入的通道), 因此节点内一律用 dict.get() 取字段, 保证健壮。
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from collections import Counter
from typing import Any, Dict, List, TypedDict

from config.settings import settings
from modules import data_loader
from modules import dingtalk_notifier
from modules import lead_assigner
from modules import notify
from modules import profile_analyzer

logger = logging.getLogger(__name__)

# 推送互斥锁: 定时任务与手动触发/并发请求同时跑流水线时,
# 串行化发送环节, 避免重复推送、并发网络请求导致限流或资源争用。
# 用普通 Lock 而非 RLock: _report_push_node 是叶子节点, 不会重入。
_push_lock = threading.Lock()

# ============================================================
# LangGraph 可选依赖(宽松导入, 失败降级顺序执行)
# ============================================================

try:
    from langgraph.graph import END, START, StateGraph

    LANGUAGE_GRAPH_AVAILABLE: bool = True
    _langgraph_import_error: Exception | None = None
except Exception as exc:  # noqa: BLE001 —— langgraph 不可用时降级顺序执行
    LANGUAGE_GRAPH_AVAILABLE = False
    _langgraph_import_error = exc
    logger.warning("langgraph 导入失败(%s), 编排层降级为顺序直调模式", exc)


# ============================================================
# 共享状态对象(全图唯一的数据通道)
# ============================================================


class PipelineState(TypedDict, total=False):
    """流水线共享状态(全图唯一数据通道, langgraph 标准 TypedDict 声明)。

    - 输入通道(种子状态写入): customers / chat_map / sales / experiences
    - 中间产物(各智能体节点写入):
        analysis_results(分析师) / assignments(匹配师) / push_reports(推送员)
    - 汇总通道(汇总节点写入): stats / summary
    - 运行期信息: analyst_note / matcher_note / errors / meta
    """

    # 输入
    customers: List[Any]
    chat_map: Dict[str, List[Any]]
    sales: List[Any]
    experiences: List[Any]
    skip_push: bool
    # 分析师产物
    analysis_results: Dict[str, Any]
    analyst_note: str
    # 匹配师产物
    assignments: List[Any]
    matcher_note: str
    # 推送员产物
    push_reports: Dict[str, Any]
    # 汇总
    stats: Dict[str, Dict[str, int]]
    summary: Dict[str, Any]
    # 运行期
    errors: List[str]
    meta: Dict[str, Any]


# 所有通道的默认值(缺什么补什么, 保证任何时刻状态完整)
_STATE_DEFAULTS: Dict[str, Any] = {
    "customers": [],
    "chat_map": {},
    "sales": [],
    "experiences": [],
    "skip_push": False,
    "analysis_results": {},
    "analyst_note": "",
    "assignments": [],
    "matcher_note": "",
    "push_reports": {},
    "stats": {},
    "summary": {},
    "errors": [],
    "meta": {},
}


def _safe(state: dict, key: str, default: Any = None) -> Any:
    """从任意状态对象安全取字段(兼容 TypedDict dict 与自定义对象)。"""
    if state is None:
        return _STATE_DEFAULTS.get(key, default)
    if isinstance(state, dict):
        return state.get(key, _STATE_DEFAULTS.get(key, default))
    return getattr(state, key, _STATE_DEFAULTS.get(key, default))


# ============================================================
# 节点内模型调用(openai SDK 直连, 不经过 langchain)
# ============================================================


def _llm_enabled() -> bool:
    """判断节点内是否启用 LLM 调用: 当前 provider 已配置 key 即启用。

    未配置 key 时分析师/匹配师的 LLM 总结走规则文本, 不产生网络请求。
    """
    from modules import llm_client
    return llm_client.enabled()


def _llm_chat_completion(
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 512,
) -> str:
    """节点内调用统一 LLM 网关(按 LLM_PROVIDER 分派 Kimi / OpenAI 兼容)。

    Args:
        messages: [{"role": ..., "content": ...}] 消息列表(system/user)。
        temperature: 采样温度。
        max_tokens: 回复最大 token 数。

    Returns:
        str: 模型回复文本。

    Raises:
        Exception: 网络/超时/鉴权等 —— 由调用方(节点)兜底降级。
    """
    from modules import llm_client

    system = ""
    user = ""
    for m in messages:
        if m.get("role") == "system":
            system = m.get("content", "")
        elif m.get("role") == "user":
            user = m.get("content", "")
    return llm_client.chat(system, user, max_tokens=max_tokens, temperature=temperature)


# ============================================================
# 分析师节点(image_profiling): 画像分析智能体
# ============================================================

# 分析师 LLM 系统提示词(节点内直接调模型时使用)
_ANALYST_SYSTEM_PROMPT = (
    "你是销售线索分析师智能体。你会收到一批客户的画像分析结果, "
    "请用不超过200字总结这批客户的整体画像特征、意向分布与跟进要点, "
    "只输出结论, 不要输出任何 JSON 或多余格式。"
)


def _analyst_llm_note(analysis_results: Dict[str, Any]) -> str:
    """分析师节点内的 LLM 总结(openai SDK 直连); 失败/未启用时降级规则文本。"""
    if not _llm_enabled():
        return "mock 模式: 分析师按规则引擎完成画像分析(未调用大模型)。"
    try:
        payload = {
            "客户数": len(analysis_results),
            "意向分布": dict(Counter(
                r.intention_level for r in analysis_results.values()
            )),
            "流失分布": dict(Counter(
                r.churn_risk for r in analysis_results.values()
            )),
        }
        note = _llm_chat_completion([
            {"role": "system", "content": _ANALYST_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ])
        logger.info("分析师 LLM 总结成功: %d 字符", len(note))
        return note
    except Exception as exc:  # noqa: BLE001 —— LLM 失败不影响分析产物
        logger.warning("分析师 LLM 总结失败(%s), 使用规则文本", exc)
        return "LLM 总结不可用, 以规则引擎统计为准。"


def _image_profiling_node(state: dict) -> dict:
    """分析师节点: 调 profile_analyzer 批量生成客户画像(LLM 引擎或规则引擎)。

    Args:
        state: 当前状态 dict(含 customers / chat_map / errors)。

    Returns:
        dict: 更新 analysis_results / stats / analyst_note / errors 的部分状态。
    """
    customers: List = _safe(state, "customers") or []
    chat_map: Dict = _safe(state, "chat_map") or {}
    errors: List = list(_safe(state, "errors") or [])
    try:
        results = profile_analyzer.analyze_customers_batch(customers, chat_map)
        note = _analyst_llm_note(results)
        logger.info("分析师节点完成: %d 家客户画像", len(results))
        return {
            "analysis_results": results,
            "stats": _compute_profile_stats(results),
            "analyst_note": note,
            "meta": {"analyst_engine": "llm" if _llm_enabled() else "rules"},
        }
    except Exception as exc:  # noqa: BLE001 —— 分析失败记录错误, 图继续走
        logger.error("分析师节点异常(%s)", exc)
        return {
            "analysis_results": {},
            "stats": _compute_profile_stats({}),
            "analyst_note": f"分析师节点异常: {exc}",
            "errors": errors + [f"image_profiling: {exc}"],
            "meta": {"analyst_engine": "failed"},
        }


# ============================================================
# 匹配师节点(lead_matching): 线索分配智能体
# ============================================================

_MATCHER_SYSTEM_PROMPT = (
    "你是线索匹配师智能体。你会收到一份线索分配结果清单, "
    "请用不超过200字总结分配走向(推荐销售分布/待人工处理情况)与后续动作建议, "
    "只输出结论, 不要输出 JSON 或多余格式。"
)


def _matcher_llm_note(assignments: List[Any]) -> str:
    """匹配师节点内的 LLM 总结(openai SDK 直连); 失败/未启用时降级规则文本。"""
    if not _llm_enabled():
        return "mock 模式: 匹配师按规则+RAG 混合分配完成线索匹配(未调用大模型)。"
    try:
        payload = []
        for a in assignments:
            payload.append({
                "customer": a.customer_name,
                "sales": a.sales_name,
                "needs_human": a.needs_human,
            })
        note = _llm_chat_completion([
            {"role": "system", "content": _MATCHER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ])
        logger.info("匹配师 LLM 总结成功: %d 字符", len(note))
        return note
    except Exception as exc:  # noqa: BLE001 —— LLM 失败不影响分配产物
        logger.warning("匹配师 LLM 总结失败(%s), 使用规则文本", exc)
        return "LLM 总结不可用, 以规则+RAG 分配结果为准。"


def _lead_matching_node(state: dict) -> dict:
    """匹配师节点: 调 lead_assigner 做规则硬约束 + RAG 语义 + 负载均衡的分配。

    Args:
        state: 当前状态 dict(含 customers / sales / experiences / analysis_results)。

    Returns:
        dict: 更新 assignments / matcher_note / errors 的部分状态。
    """
    customers: List = _safe(state, "customers") or []
    sales: List = _safe(state, "sales") or []
    experiences: List = _safe(state, "experiences") or []
    analysis_results: Dict = _safe(state, "analysis_results") or {}
    errors: List = list(_safe(state, "errors") or [])

    try:
        # 只分配无归属客户(已归属的由原销售继续跟进)
        unassigned = [c for c in customers if not getattr(c, "owner_sales_id", None)]
        assignments = lead_assigner.assign_leads(
            unassigned,
            sales_list=sales,
            experiences=experiences,
            analysis_results=analysis_results,
        )
        note = _matcher_llm_note(assignments)
        logger.info("匹配师节点完成: %d 家线索分配", len(assignments))
        return {
            "assignments": assignments,
            "matcher_note": note,
            "meta": {"matcher_engine": "llm" if _llm_enabled() else "rules"},
        }
    except Exception as exc:  # noqa: BLE001 —— 分配失败记录错误, 图继续走
        logger.error("匹配师节点异常(%s)", exc)
        return {
            "assignments": [],
            "matcher_note": f"匹配师节点异常: {exc}",
            "errors": errors + [f"lead_matching: {exc}"],
            "meta": {"matcher_engine": "failed"},
        }


# ============================================================
# 推送员节点(report_push): 钉钉触达智能体
# ============================================================

_PUSH_REPORT_TITLE_DAILY = "销售线索智能日报"
_PUSH_REPORT_TITLE_BATCH = "销售线索分配明细"


def _report_push_node(state: dict) -> dict:
    """推送员节点: 经 notify 统一入口推送日报与分配明细(尊重 notifier_channel)。

    mock 模式(webhook 未配置)下各通道会打印提示并返回 False —— 这是预期行为,
    不崩溃、不中断; 日报数据已落库, 稍后可重推。

    Args:
        state: 当前状态 dict(含 customers / stats / assignments / skip_push)。

    Returns:
        dict: 更新 push_reports / errors 的部分状态。
    """
    stats: Dict = _safe(state, "stats") or {}
    assignments: List = _safe(state, "assignments") or []
    customers: List = _safe(state, "customers") or []
    analysis_results: Dict = _safe(state, "analysis_results") or {}
    errors: List = list(_safe(state, "errors") or [])

    # --skip-push: 在节点内真正跳过推送(而非事后改 summary 文本)
    if _safe(state, "skip_push"):
        logger.info("--skip-push 生效, 推送员节点跳过(数据已落库可稍后重推)")
        return {
            "push_reports": {
                "daily_report": False,
                "assignment_batch": False,
                "personal_notifications": 0,
                "report_text": "",
                "hint": "--skip-push 跳过推送(数据已落库可稍后重推)",
            },
            "meta": {"pusher_status": "skipped"},
        }

    try:
        need_human = [a for a in assignments if getattr(a, "needs_human", False)]
        human_text = f"{len(need_human)}家待人工分配" if need_human else ""
        report_text = dingtalk_notifier.build_daily_report_text(
            customer_count=len(customers),
            profile_stats=stats,
            assignment_summary={
                "recommend": _recommend_summary(assignments),
                "needs_human": human_text,
            },
        )
        # 发送环节整体串行化: 防止定时任务 + 手动触发并发时重复推送同一批日报,
        # 也避免飞书/钉钉接口被并发请求打爆触发限流。
        with _push_lock:
            # 日报/分配明细走 notify 统一入口(按 settings.notifier_channel 选择通道)
            ok_daily = notify.send_daily_report(
                report_text, title=_PUSH_REPORT_TITLE_DAILY
            )
            ok_batch = False
            personal_sent = 0
            if assignments:
                ok_batch = notify.send_assignment_batch(assignments)
                # 飞书企业自建应用: 给对应销售发送个人工作通知卡片
                try:
                    personal_sent = notify.send_personal_assignments(assignments, analysis_results=analysis_results)
                except Exception as e:
                    logger.warning("发送飞书个人工作通知异常: %s", e)

        report = {
            "daily_report": ok_daily,
            "assignment_batch": ok_batch,
            "personal_notifications": personal_sent,
            "report_text": report_text,
            "hint": (
                f"已向销售发送 {personal_sent} 条飞书个人工作通知"
                if personal_sent > 0 else (
                    "webhook 未配置, 跳过推送(数据已落库可重推)"
                    if not _webhook_configured() else ""
                )
            ),
        }
        logger.info("推送员节点完成: daily=%s batch=%s personal=%d", ok_daily, ok_batch, personal_sent)
        return {
            "push_reports": report,
            "meta": {"pusher_status": "done"},
        }
    except Exception as exc:  # noqa: BLE001 —— 推送失败记录错误, 图继续走
        logger.error("推送员节点异常(%s)", exc)
        return {
            "push_reports": {"daily_report": False, "assignment_batch": False,
                             "error": str(exc)},
            "errors": errors + [f"report_push: {exc}"],
            "meta": {"pusher_status": "failed"},
        }


def _webhook_configured() -> bool:
    """任意通知通道的 webhook 是否已配置(飞书/钉钉)。"""
    return bool(
        (settings.dingtalk_webhook_url or "").strip()
        or (settings.feishu_webhook_url or "").strip()
    )


def _recommend_summary(assignments: List[Any], limit: int = 6) -> str:
    """生成分配摘要中的推荐销售文本(按销售聚合计数, 取前 limit)。"""
    counter: Counter = Counter(
        f"{a.sales_name}({a.sales_id})" for a in assignments
        if not getattr(a, "needs_human", False)
    )
    if not counter:
        return ""
    parts = [f"{name}{n}单" for name, n in counter.most_common(limit)]
    return "/".join(parts)


# ============================================================
# 汇总节点(summarize): 生成全流水线摘要
# ============================================================


def _summarize_node(state: dict) -> dict:
    """汇总节点: 把各环节产物压成全流水线 summary(供 main.py 打印)。"""
    customers: List = _safe(state, "customers") or []
    analysis_results: Dict = _safe(state, "analysis_results") or {}
    assignments: List = _safe(state, "assignments") or []
    push_reports: Dict = _safe(state, "push_reports") or {}
    errors: List = _safe(state, "errors") or []

    intent = dict(Counter(
        r.intention_level for r in analysis_results.values()
    ))
    churn = dict(Counter(
        r.churn_risk for r in analysis_results.values()
    ))

    summary = {
        "customer_count": len(customers),
        "analyzed_count": len(analysis_results),
        "intention_stats": {"高": intent.get("高", 0), "中": intent.get("中", 0),
                            "低": intent.get("低", 0)},
        "churn_stats": {"高": churn.get("高", 0), "中": churn.get("中", 0),
                        "低": churn.get("低", 0)},
        "assignment_count": len(assignments),
        "needs_human_count": len(
            [a for a in assignments if getattr(a, "needs_human", False)]
        ),
        "push_ok": bool(push_reports.get("daily_report"))
        or bool(push_reports.get("assignment_batch")),
        "push_hint": push_reports.get("hint", ""),
        "errors": list(errors),
    }
    return {"summary": summary}


def _compute_profile_stats(analysis_results: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """从分析结果统计 意向/流失 分层(缺失等级补 0, 键与钉钉日报契约一致)。"""
    intent = Counter(r.intention_level for r in analysis_results.values())
    churn = Counter(r.churn_risk for r in analysis_results.values())
    return {
        "意向": {"高": intent.get("高", 0), "中": intent.get("中", 0),
                 "低": intent.get("低", 0)},
        "流失": {"高": churn.get("高", 0), "中": churn.get("中", 0),
                 "低": churn.get("低", 0)},
    }


# ============================================================
# 图装配 + 顺序降级
# ============================================================


def build_pipeline_graph():
    """构建并编译 LangGraph 状态图(分析师 -> 匹配师 -> 推送员 -> 汇总)。

    Returns:
        langgraph.graph.state.CompiledStateGraph: 编译后的可调用图。

    Raises:
        ImportError: langgraph 不可用时抛出(由调用方降级顺序执行)。
    """
    if not LANGUAGE_GRAPH_AVAILABLE:
        raise ImportError(
            f"langgraph 不可用({_langgraph_import_error}); 请 pip install langgraph==1.2.11"
        )

    graph = StateGraph(PipelineState)
    graph.add_node("image_profiling", _image_profiling_node)
    graph.add_node("lead_matching", _lead_matching_node)
    graph.add_node("report_push", _report_push_node)
    graph.add_node("summarize", _summarize_node)

    graph.add_edge(START, "image_profiling")
    graph.add_edge("image_profiling", "lead_matching")
    graph.add_edge("lead_matching", "report_push")
    graph.add_edge("report_push", "summarize")
    graph.add_edge("summarize", END)

    compiled = graph.compile()
    logger.info("LangGraph 状态图编译完成: 分析师 -> 匹配师 -> 推送员 -> 汇总")
    return compiled


def run_pipeline_sequential(
    customers: List[Any],
    chat_map: Dict[str, List[Any]],
    sales: List[Any],
    experiences: List[Any],
    skip_push: bool = False,
) -> dict:
    """降级路径: 不依赖 langgraph, 按顺序直接调用各模块(保证最小可用闭环)。

    Returns:
        dict: 与 run_pipeline_graph 完全同构的状态(含各节点产物与 summary)。
    """
    state = _seed_state(customers, chat_map, sales, experiences, skip_push=skip_push)
    # 1. 分析师: profile_analyzer
    try:
        results = profile_analyzer.analyze_customers_batch(customers, chat_map)
        state["analysis_results"] = results
        state["stats"] = _compute_profile_stats(results)
        state["analyst_note"] = _analyst_llm_note(results)
        state["meta"]["analyst_engine"] = "llm" if _llm_enabled() else "rules"
    except Exception as exc:  # noqa: BLE001
        logger.error("顺序模式-画像分析失败(%s)", exc)
        state["errors"].append(f"image_profiling: {exc}")
    # 2. 匹配师: lead_assigner
    try:
        unassigned = [c for c in customers if not getattr(c, "owner_sales_id", None)]
        assignments = lead_assigner.assign_leads(
            unassigned, sales_list=sales, experiences=experiences,
            analysis_results=state["analysis_results"],
        )
        state["assignments"] = assignments
        state["matcher_note"] = _matcher_llm_note(assignments)
        state["meta"]["matcher_engine"] = "llm" if _llm_enabled() else "rules"
    except Exception as exc:  # noqa: BLE001
        logger.error("顺序模式-线索分配失败(%s)", exc)
        state["errors"].append(f"lead_matching: {exc}")
    # 3. 推送员: dingtalk_notifier(失败不抛, 模块内部已兜底)
    push_partial = _report_push_node(state)
    state["push_reports"] = push_partial["push_reports"]
    state["meta"]["pusher_status"] = push_partial.get("meta", {}).get("pusher_status")
    # 顺序模式不经过 LangGraph 的通道合并, 这里手动把推送错误并入总 errors,
    # 保证最终 summary.errors 能体现推送环节的失败。
    if push_partial.get("errors"):
        state["errors"].extend(push_partial["errors"])
    # 4. 汇总
    state["summary"] = _summarize_node(state)["summary"]
    state["meta"]["engine"] = "sequential-fallback"
    return state


def _seed_state(
    customers: List[Any],
    chat_map: Dict[str, List[Any]],
    sales: List[Any],
    experiences: List[Any],
    skip_push: bool = False,
) -> dict:
    """构造初始状态(带执行元信息)。

    使用深拷贝: 避免 errors / analysis_results / assignments 等可变默认值
    与全局 _STATE_DEFAULTS 共享同一对象(否则顺序降级路径原地 append 会
    污染全局默认状态, 导致错误在多次运行之间残留)。
    """
    state = copy.deepcopy(_STATE_DEFAULTS)
    state.update({
        "customers": list(customers or []),
        "chat_map": dict(chat_map or {}),
        "sales": list(sales or []),
        "experiences": list(experiences or []),
        "skip_push": bool(skip_push),
        "meta": {
            "engine": "langgraph",
            "langgraph_available": LANGUAGE_GRAPH_AVAILABLE,
            "mock_mode": settings.mock_mode,
            "llm_enabled": _llm_enabled(),
        },
    })
    return state


def run_pipeline_graph(
    customers: List[Any],
    chat_map: Dict[str, List[Any]],
    sales: List[Any],
    experiences: List[Any],
    skip_push: bool = False,
) -> dict:
    """多智能体流水线主入口: 优先 LangGraph 状态图, 失败降级顺序直调。

    Args:
        customers: 客户模型列表(data_loader.load_customers 结果)。
        chat_map:  会话记录分组(data_loader.build_chat_map 结果)。
        sales:     销售人员列表(data_loader.load_sales 结果)。
        experiences: 销售经验片段列表(data_loader.load_sales_experiences 结果)。
        skip_push: True 时推送员节点直接跳过(日报/明细/个人通知都不发),
                   数据仍照常落库, 可稍后重推。

    Returns:
        dict: 完整状态, 含
            analysis_results / assignments / push_reports / stats / summary / errors;
            与 run_pipeline_sequential 输出同构, main.py 无须区分路径。
    """
    if not LANGUAGE_GRAPH_AVAILABLE:
        logger.warning("langgraph 不可用, 走顺序直调降级路径")
        return run_pipeline_sequential(customers, chat_map, sales, experiences, skip_push=skip_push)

    state = _seed_state(customers, chat_map, sales, experiences, skip_push=skip_push)
    try:
        compiled = build_pipeline_graph()
        final = compiled.invoke(state)
        final = _ensure_complete_state(final)
        logger.info("LangGraph 多智能体流水线执行完成, engine=%s",
                    final.get("meta", {}).get("engine", "langgraph"))
        return final
    except Exception as exc:  # noqa: BLE001 —— 图编译/调用失败降级顺序执行
        logger.error("LangGraph 执行失败(%s), 降级为顺序直调", exc)
        fallen = run_pipeline_sequential(customers, chat_map, sales, experiences, skip_push=skip_push)
        fallen["errors"] = (
            ["langgraph_invoke: %s" % exc]
            + list(_safe(fallen, "errors") or [])
        )
        return fallen


def _ensure_complete_state(state: dict) -> dict:
    """补齐最终状态里可能缺失的字段与 meta 标记(防御性)。"""
    for key, default in _STATE_DEFAULTS.items():
        if key not in state or state[key] is None:
            state[key] = default
    meta = dict(state.get("meta") or {})
    meta.setdefault("engine", "langgraph")
    meta.setdefault("langgraph_available", LANGUAGE_GRAPH_AVAILABLE)
    meta.setdefault("mock_mode", settings.mock_mode)
    state["meta"] = meta
    # 统计与摘要若未产出(异常路径), 兜底生成
    if not state.get("stats"):
        state["stats"] = _compute_profile_stats(state.get("analysis_results") or {})
    if not state.get("summary"):
        state["summary"] = _summarize_node(state).get("summary", {})
    return state


# ============================================================
# 便捷数据装配(供 main.py / 测试脚本复用)
# ============================================================


def load_pipeline_inputs() -> Dict[str, Any]:
    """加载流水线所需全部输入(数据层装配): 客户/会话/销售/经验语料。

    Returns:
        dict: {customers, chat_map, sales, experiences}。数据文件缺失时抛异常
              (由 main.py 最外层兜底捕获)。
    """
    customers, records, sales = data_loader.load_all()
    chat_map = data_loader.build_chat_map(records)
    experiences = data_loader.load_sales_experiences()
    return {
        "customers": customers,
        "chat_map": chat_map,
        "sales": sales,
        "experiences": experiences,
    }


def build_summary_text(state: dict) -> str:
    """把最终状态渲染成可打印的中文 summary 文本(供 main.py --run-once)。"""
    summary = state.get("summary") or {}
    stats = state.get("stats") or {}
    intent = stats.get("意向") or {}
    churn = stats.get("流失") or {}
    push = state.get("push_reports") or {}
    errors = state.get("errors") or []

    lines: List[str] = []
    lines.append("=" * 56)
    lines.append("销售线索智能分析与分发助手 —— 流水线摘要")
    lines.append("=" * 56)
    lines.append(f"客户总数: {summary.get('customer_count', 0)} 家"
                 f" | 完成画像: {summary.get('analyzed_count', 0)} 家")
    lines.append(
        f"意向分层: 高 {intent.get('高', 0)} / 中 {intent.get('中', 0)}"
        f" / 低 {intent.get('低', 0)}"
    )
    lines.append(
        f"流失风险: 高 {churn.get('高', 0)} / 中 {churn.get('中', 0)}"
        f" / 低 {churn.get('低', 0)}"
    )
    lines.append(
        f"分配结果: {summary.get('assignment_count', 0)} 条"
        f" | 待人工分配 {summary.get('needs_human_count', 0)} 家"
    )
    lines.append(
        f"推送状态: {'成功' if summary.get('push_ok') else '未推送/失败'}"
        f" {summary.get('push_hint', '')}".rstrip()
    )
    engine = (state.get("meta") or {}).get("engine", "unknown")
    lines.append(f"编排引擎: {engine} (mock_mode={settings.mock_mode})")
    if push:
        lines.append(
            "推送明细: 日报=%s, 分配表=%s"
            % (push.get("daily_report"), push.get("assignment_batch"))
        )
    if errors:
        lines.append(f"非致命错误 {len(errors)} 条:")
        for err in errors[:5]:
            lines.append(f"  - {err}")
    lines.append("=" * 56)
    return "\n".join(lines)


# 模块加载时即可用的降级入口别名(与 run_pipeline_graph 一致)
run_pipeline = run_pipeline_graph


if __name__ == "__main__":
    # 独立运行自检: python -m orchestrator.language_graph_flow
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    inputs = load_pipeline_inputs()
    final_state = run_pipeline_graph(
        inputs["customers"], inputs["chat_map"], inputs["sales"], inputs["experiences"]
    )
    print(build_summary_text(final_state))
    print("\n节点产物:")
    for key in ("analysis_results", "assignments", "push_reports"):
        val = final_state.get(key)
        print(f"  {key}: {len(val) if isinstance(val, (list, dict)) else val}")