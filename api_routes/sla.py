# -*- coding: utf-8 -*-
"""SLA 超时预警 + 自动流转公海 路由。"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from modules import data_loader, sla_monitor

from .common import require_admin

router = APIRouter(tags=["sla"], dependencies=[Depends(require_admin)])


@router.get("/sla/status")
def sla_status() -> Dict[str, Any]:
    """查询当前所有已归属客户的 SLA 状态(只读, 不触发流转)。"""
    result = sla_monitor.check_sla(apply_changes=False)
    return {
        "warning": result["warning"],
        "overdue": result["overdue"],
        "ok": result["ok"],
        "summary": {
            "warning_count": len(result["warning"]),
            "overdue_count": len(result["overdue"]),
            "ok_count": len(result["ok"]),
        },
    }


@router.post("/sla/check")
def sla_check() -> Dict[str, Any]:
    """触发一次 SLA 检测: 超时客户自动流转公海(释放归属), 返回预警/超时名单。

    检测到预警/超时后, 会向相关销售发送飞书卡片提醒(需配置飞书应用凭证)。
    """
    result = sla_monitor.check_sla(apply_changes=True)

    # 发送飞书预警通知(尽力而为, 失败不影响检测结果)
    notifier = None
    try:
        from modules import feishu_app_notifier as notifier_mod
        notifier = notifier_mod
    except Exception:  # noqa: BLE001
        pass

    notified: List[str] = []
    if notifier is not None:
        try:
            sales = data_loader.load_sales()
            sales_by_id = {s.sales_id: s for s in sales}
        except Exception:  # noqa: BLE001
            sales_by_id = {}

        # 预警提醒 + 超时通知
        for item in result["warning"] + result["overdue"]:
            sid = item.get("owner_sales_id", "")
            sales = sales_by_id.get(sid)
            open_id = getattr(sales, "open_id", "") if sales else ""
            if not open_id:
                continue
            status = item.get("sla_status", "warning")
            ok_sent = notifier.send_sla_alert_card(
                receive_id=open_id,
                customer_name=item.get("customer_name", ""),
                sla_status=status,
                elapsed_hours=item.get("elapsed_hours", 0),
                overdue_hours=sla_monitor.DEFAULT_OVERDUE_HOURS,
            )
            if ok_sent:
                notified.append(item.get("customer_name", ""))

    return {
        "warning": result["warning"],
        "overdue": result["overdue"],
        "ok": result["ok"],
        "summary": {
            "warning_count": len(result["warning"]),
            "overdue_count": len(result["overdue"]),
            "ok_count": len(result["ok"]),
            "released_to_pool": [o["customer_name"] for o in result["overdue"]],
            "notified_sales": notified,
        },
    }
