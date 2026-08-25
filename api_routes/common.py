# -*- coding: utf-8 -*-
"""API 公共工具: 全局异常处理 / 统一 JSON 响应 / 飞书卡片响应 / 客户列表公共逻辑。

被各路由模块共享, 避免重复代码。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from modules import data_loader  # noqa: E402
from modules import sla_monitor  # noqa: E402

logger = logging.getLogger(__name__)


# ============================================================
# 全局异常处理 —— 统一返回 {"detail": ...}
# ============================================================


async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """全局兜底: 任何未捕获异常都转成 {"detail": ...} JSON, 不裸抛堆栈。"""
    logger.error("未捕获异常(%s): %s", type(exc).__name__, exc)
    return _json_response(500, "服务内部错误: %s" % exc)


def _json_response(status_code: int, detail: Any) -> JSONResponse:
    """构造统一格式的 JSON 响应(成功/失败都走这里)。"""
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _feishu_card_response(
    card: Optional[Dict[str, Any]] = None,
    toast: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """构造飞书卡片回调的标准响应(用于卡片按钮点击后原地更新卡片)。

    飞书卡片回调(交互回调, card.action.trigger)更新卡片: 响应体直接返回 JSON,
    含 card(替换当前卡片) 与/或 toast(弹提示)。

    关键: 使用原始 JSON 卡片(raw)时, card 字段必须包装成
    {"type": "raw", "data": {卡片本体}}, 缺少 type/data 包装会触发飞书错误码 200672。
    """
    payload: Dict[str, Any] = {}
    if card:
        payload["card"] = {"type": "raw", "data": card}
    if toast:
        payload["toast"] = toast
    return payload


# ============================================================
# 销售 / 客户列表公共逻辑
# ============================================================


def _find_sales_by_open_id(open_id: str) -> Optional[Dict[str, Any]]:
    """根据飞书 open_id 定位当前销售(反查销售列表的 open_id 字段)。"""
    if not open_id:
        return None
    try:
        sales = data_loader.load_sales()
    except Exception:  # noqa: BLE001
        return None
    for s in sales:
        d = s.model_dump() if hasattr(s, "model_dump") else dict(s)
        if d.get("open_id") == open_id:
            return d
    return None


def _level_map_from_history() -> Dict[str, Dict[str, str]]:
    """从 analysis_history 最新批次读取每个客户的意向/流失等级。

    Returns:
        dict: {customer_id: {"intention_level": ..., "churn_risk": ...}}。
    """
    level_map: Dict[str, Dict[str, str]] = {}
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is not None:
            try:
                from modules.data_loader import AnalysisHistory

                rows = (
                    session.query(AnalysisHistory)
                    .order_by(AnalysisHistory.created_at.desc())
                    .all()
                )
                seen: set = set()
                for row in rows:
                    if row.customer_id in seen:
                        continue
                    seen.add(row.customer_id)
                    try:
                        r = json.loads(row.result_json)
                    except (json.JSONDecodeError, TypeError):
                        r = {}
                    level_map[row.customer_id] = {
                        "intention_level": r.get("intention_level"),
                        "churn_risk": r.get("churn_risk"),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("join 意向/流失等级失败, 仅返回基础资料: %s", exc)
            finally:
                session.close()
    except Exception:  # noqa: BLE001
        pass
    return level_map


def _customers_with_levels() -> List[Dict[str, Any]]:
    """返回全量客户列表, 并从 analysis_history 最新批次 join 意向/流失等级。

    抽取自 /customers 的公共逻辑, 供移动端"我的客户"复用。
    注意: 客户列表已合并飞书多维表格同步状态(跟进状态/归属销售) + SLA 超时流转
    (超时释放回公海的客户 owner_sales_id 覆盖为 None)。
    """
    customers = data_loader.apply_bitable_sync_state(data_loader.load_customers())
    # 合并 SLA 超时流转(释放归属回公海)
    customers = sla_monitor.apply_sla_overlay(customers)
    result: List[Dict[str, Any]] = [
        c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in customers
    ]
    level_map = _level_map_from_history()
    for item in result:
        meta = level_map.get(item["customer_id"], {})
        item["intention_level"] = meta.get("intention_level")
        item["churn_risk"] = meta.get("churn_risk")
    return result


def _serialize_assignments(state: Dict[str, Any], analysis_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把流水线分配结果序列化成看板可渲染的简表(含客户意向等级)。

    Args:
        state: 流水线最终状态(含 assignments 列表)。
        analysis_results: {customer_id: AnalysisResult}, 用于补意向等级。

    Returns:
        list[dict]: 每条含 customer_id / customer_name / intention_level /
            sales_id / sales_name / match_reason / needs_human。
    """
    briefs: List[Dict[str, Any]] = []
    for a in state.get("assignments") or []:
        ar = analysis_results.get(getattr(a, "customer_id", ""))
        intention = getattr(ar, "intention_level", None) if ar is not None else None
        briefs.append({
            "customer_id": getattr(a, "customer_id", ""),
            "customer_name": getattr(a, "customer_name", ""),
            "intention_level": intention,
            "sales_id": getattr(a, "sales_id", ""),
            "sales_name": getattr(a, "sales_name", ""),
            "match_reason": getattr(a, "match_reason", ""),
            "needs_human": bool(getattr(a, "needs_human", False)),
        })
    return briefs
