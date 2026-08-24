# -*- coding: utf-8 -*-
"""统一通知入口(notify): 按 settings.notifier_channel 转发到 飞书/钉钉 通道。

设计(避免 main/api/orchestrator 里 if/else 散落):
    - 对外只暴露与各通道一致的 send_daily_report / send_assignment_batch;
    - 读取 settings.notifier_channel("feishu" | "dingtalk")决定首选通道;
    - 首选通道 webhook 未配置时, 若另一通道已配置 → 自动切换并记日志
      ("谁配了 webhook 用谁");
    - 两通道都未配置 → 打印提示并返回 False(等价各通道的 mock 行为)。
    - build_daily_report_text / build_assignment_table_text 仍由调用方
      从具体通道模块 import(文案在各通道内保持一致, 文本在飞书/钉钉
      均可直接展示), 本模块只做"发送"转发。

向后兼容:
    - dingtalk_notifier 全部函数与行为不动(既有调用方/orchestrator/pytest 不受影响);
    - 本模块是新增的可选统一入口, main/api 可自行选择直接调具体通道或走本入口。

依赖方向(单向):
    notify → config.settings + dingtalk_notifier + feishu_notifier(均为同级可选),
    不反向依赖调度/数据模块。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from config.settings import settings

# 两通道同步导入: 任一失败不阻塞本模块? 不 —— 二者均为项目内稳定模块,
# 直接导入(若导入失败说明部署不完整, 由调用方暴露)。
from modules import dingtalk_notifier, feishu_notifier
from modules import feishu_app_notifier

logger = logging.getLogger(__name__)

# 通道常量(与 settings.notifier_channel 取值一致)
FEISHU_CHANNEL: str = "feishu"
DINGTALK_CHANNEL: str = "dingtalk"


def _parse_sales_mobile_map() -> dict:
    """解析销售人员手机号映射配置 (形如 'S001:13800000001,S002:13800000002')。"""
    raw = getattr(settings, "sales_mobile_map", "") or ""
    res = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            sid, mobile = part.split(":", 1)
            res[sid.strip()] = mobile.strip()
    return res


def send_personal_assignments(assignments: List, analysis_results: Optional[dict] = None) -> int:
    """向每个分配到新线索的销售发送【聚合分配日报大卡片】(单人单卡汇总, 零刷屏)。

    Args:
        assignments: 分配列表 (AssignmentResult)。
        analysis_results: 客户画像分析结果字典 {customer_id: AnalysisResult}。

    Returns:
        int: 成功发送的个人通知条数 (按销售人数统计)。
    """
    # mock 模式绝不发真实飞书请求(开箱即跑/测试时零网络副作用)
    if settings.mock_mode:
        logger.debug("mock 模式, 跳过飞书个人工作通知推送")
        return 0

    if not (getattr(settings, "feishu_app_id", "") and getattr(settings, "feishu_app_secret", "")):
        logger.debug("未配置飞书 App ID / App Secret, 跳过个人工作通知推送")
        return 0

    mobile_map = _parse_sales_mobile_map()
    analysis_results = analysis_results or {}
    
    # 1. 按销售人员分组汇总 (sales_id -> list of customer dicts)
    from collections import defaultdict
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    sales_name_map: Dict[str, str] = {}

    for a in assignments:
        sales_id = getattr(a, "sales_id", "") or (a.get("sales_id") if isinstance(a, dict) else "")
        cust_id = getattr(a, "customer_id", "") or (a.get("customer_id") if isinstance(a, dict) else "")
        cust_name = getattr(a, "customer_name", "") or (a.get("customer_name") if isinstance(a, dict) else "")
        reason = getattr(a, "match_reason", "") or (a.get("match_reason") if isinstance(a, dict) else "")
        sales_name = getattr(a, "sales_name", "") or (a.get("sales_name") if isinstance(a, dict) else "")

        if not sales_id:
            continue
        sales_name_map[sales_id] = sales_name or sales_id

        # 从 analysis_results 提取诉求与建议
        res_obj = analysis_results.get(cust_id)
        intent = "中"
        churn = "低"
        demands = []
        suggestion = ""
        if res_obj:
            if hasattr(res_obj, "intention_level"):
                intent = getattr(res_obj, "intention_level", "中")
                churn = getattr(res_obj, "churn_risk", "低")
                demands = getattr(res_obj, "core_demands", []) or []
                suggestion = getattr(res_obj, "follow_up_suggestion", "")
            elif isinstance(res_obj, dict):
                r = res_obj.get("result", {}) if "result" in res_obj else res_obj
                intent = r.get("intention_level", "中")
                churn = r.get("churn_risk", "低")
                demands = r.get("core_demands", []) or []
                suggestion = r.get("follow_up_suggestion", "")

        grouped[sales_id].append({
            "customer_id": cust_id,
            "customer_name": cust_name,
            "intention_level": intent,
            "churn_risk": churn,
            "match_reason": reason,
            "core_demands": demands,
            "follow_up_suggestion": suggestion,
        })

    # 2. 为每个销售发送一张聚合卡片
    success_count = 0
    for sales_id, items in grouped.items():
        sales_name = sales_name_map.get(sales_id, sales_id)
        mobile = mobile_map.get(sales_id)
        if not mobile:
            logger.debug("销售 %s (%s) 未配置手机号, 跳过个人工作通知", sales_id, sales_name)
            continue

        open_id = feishu_app_notifier.get_open_id_by_mobile(mobile)
        if not open_id:
            logger.warning("销售 %s 手机号 %s 无法查询到飞书 open_id", sales_id, mobile)
            continue

        ok = feishu_app_notifier.send_aggregated_assignment_card(
            receive_id=open_id,
            sales_name=sales_name,
            sales_id=sales_id,
            items=items,
            receive_id_type="open_id",
        )
        if ok:
            success_count += 1

    logger.info("飞书聚合分配通知发送完成: 成功向 %d 位销售发送卡片 (涵盖 %d 条线索)", success_count, len(assignments))
    return success_count


# ============================================================
# 通道选择
# ============================================================

def _current_channel() -> str:
    """选择实际发送通道: 首选 notifier_channel; 其未配置则切到已配置的另一通道。

    Returns:
        str: "feishu" | "dingtalk"; 两通道都未配置时返回首选通道
             (由 send_* 内部打印提示返回 False)。
    """
    preferred = (settings.notifier_channel or FEISHU_CHANNEL).strip().lower()
    if preferred not in (FEISHU_CHANNEL, DINGTALK_CHANNEL):
        logger.warning("未知 notifier_channel=%r, 回落默认飞书", preferred)
        preferred = FEISHU_CHANNEL

    feishu_ok = bool((settings.feishu_webhook_url or "").strip())
    dingtalk_ok = bool((settings.dingtalk_webhook_url or "").strip())

    if preferred == FEISHU_CHANNEL and not feishu_ok and dingtalk_ok:
        logger.info("notifier_channel=feishu 但飞书 webhook 未配置, 自动切换钉钉(谁配了用谁)")
        return DINGTALK_CHANNEL
    if preferred == DINGTALK_CHANNEL and not dingtalk_ok and feishu_ok:
        logger.info("notifier_channel=dingtalk 但钉钉 webhook 未配置, 自动切换飞书(谁配了用谁)")
        return FEISHU_CHANNEL
    return preferred


def current_channel() -> str:
    """查询当前实际生效的通知通道(供日志/展示)。

    Returns:
        str: "feishu" | "dingtalk"。
    """
    return _current_channel()


# ============================================================
# 统一发送入口(与各通道签名一致)
# ============================================================

def send_daily_report(summary_text: str, title: str = "销售线索智能日报") -> bool:
    """统一日报推送入口: 转发到当前生效通道(飞书/钉钉)。

    Args:
        summary_text: 日报正文文本(可由任一通道的 build_daily_report_text 构造,
                      文本内容两通道通用)。
        title: 消息标题, 默认"销售线索智能日报"。

    Returns:
        bool: 成功 True; 两通道均未配置 / 推送失败 → False(不抛异常)。
    """
    channel = _current_channel()
    if channel == DINGTALK_CHANNEL:
        return dingtalk_notifier.send_daily_report(summary_text, title=title)
    return feishu_notifier.send_daily_report(summary_text, title=title)


def send_assignment_batch(assignments: List) -> bool:
    """统一分配明细推送入口: 转发到当前生效通道(飞书/钉钉)。

    Args:
        assignments: AssignmentResult 列表(可为空)。

    Returns:
        bool: 成功 True; 两通道均未配置 / 推送失败 → False(不抛异常)。
    """
    channel = _current_channel()
    if channel == DINGTALK_CHANNEL:
        return dingtalk_notifier.send_assignment_batch(assignments)
    return feishu_notifier.send_assignment_batch(assignments)