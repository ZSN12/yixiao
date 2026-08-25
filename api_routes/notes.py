# -*- coding: utf-8 -*-
"""跟进小记 / AI 话术 / 人工复核反馈 路由。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from modules import agent_memory, data_loader, follow_up_notes, lead_assigner, talk_track

from .common import require_admin, require_auth

router = APIRouter(tags=["notes-talktrack-feedback"], dependencies=[Depends(require_auth)])


class FeedbackRequest(BaseModel):
    """人工复核反馈请求体。

    Attributes:
        customer_id: 客户 ID。
        correct_sales_id: 人工确认/修正后的正确销售 ID。
        note: 人工备注(可选)。
    """

    customer_id: str
    correct_sales_id: str
    note: str = ""


class NoteReanalyzeRequest(BaseModel):
    """跟进小记再分析请求体。"""

    customer_id: str
    note_text: str
    sales_id: str = ""


# ============================================================
# 跟进小记
# ============================================================


@router.get("/follow-up-notes")
def list_follow_up_notes(customer_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """查询跟进小记列表(可选按客户过滤, 按时间倒序)。

    Args:
        customer_id: 可选, 按客户 ID 过滤。

    Returns:
        list[dict]: 每条含 customer_id / customer_name / sales_id / note_text /
            intention_before / intention_after / churn_before / churn_after /
            created_at。
    """
    return follow_up_notes.list_notes(customer_id=customer_id)


@router.post("/follow-up-notes/reanalyze")
def reanalyze_note(req: NoteReanalyzeRequest) -> Dict[str, Any]:
    """直接提交一条跟进小记, 触发 AI 动态重估意向(HTTP 入口)。

    供手机端/电脑端工作台录入小记使用, 与飞书卡片 submit_note 共用同一引擎。

    Args:
        req: {customer_id, note_text, sales_id?}。

    Returns:
        dict: 再分析结果(意向/流失等级变化 + 新跟进策略)。
    """
    customer = data_loader.load_customers()
    matched = next((c for c in customer if c.customer_id == req.customer_id), None)
    customer_name = matched.customer_name if matched else req.customer_id
    return follow_up_notes.reanalyze_with_note(
        customer_id=req.customer_id,
        customer_name=customer_name,
        note_text=req.note_text,
        sales_id=req.sales_id,
        persist=True,
    )


# ============================================================
# AI 话术
# ============================================================


@router.get("/talk-track/{customer_id}")
def get_talk_track(customer_id: str, track_type: str = "wechat", sales_id: str = "") -> Dict[str, Any]:
    """AI 一键生成跟进话术(微信破冰 / 电话话术)。

    供手机端/电脑端工作台调用, 与飞书卡片 gen_talk_track 共用同一引擎。

    Args:
        customer_id: 客户 ID(路径参数)。
        track_type: "wechat"(微信破冰) | "phone"(电话话术), 默认 wechat。
        sales_id: 销售 ID(用于话术署名), 可选。

    Returns:
        dict: {customer_id, customer_name, track_type, content, engine}。
            engine 为 "llm"(Kimi K2.7 Code) | "rules"(规则模板)。
    """
    # 反查销售姓名(用于话术署名)
    sales_name = ""
    if sales_id:
        try:
            sales = data_loader.load_sales()
            matched = next((s for s in sales if s.sales_id == sales_id), None)
            if matched:
                sales_name = matched.name
        except Exception:  # noqa: BLE001
            sales_name = sales_id

    return talk_track.generate_talk_track(customer_id, track_type, sales_name)


# ============================================================
# 人工复核反馈(记忆升级)
# ============================================================


@router.post("/feedback", dependencies=[Depends(require_admin)])
def submit_feedback(req: FeedbackRequest) -> Dict[str, Any]:
    """人工复核反馈: 把该客户的记忆升级/新建为强记忆(影响后续分配)。

    仅超级管理员可调用(涉及记忆强写入, 影响后续分配)。

    Args:
        req: FeedbackRequest {customer_id, correct_sales_id, note?}。

    Returns:
        dict: 升级后的强记忆条目(MemoryEntry 的 dict 表示), 含 memory_id /
            customer_id / query_text / sales_id / decision / correct_sales_id /
            confidence / source="strong" / feedback_note / created_at。

    Raises:
        HTTPException(400): agent_memory 不可用(记忆功能未启用)时返回 400。
    """
    entry = lead_assigner.submit_feedback(
        customer_id=req.customer_id,
        correct_sales_id=req.correct_sales_id,
        note=req.note,
        memory=agent_memory,
    )
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail="agent_memory 记忆模块不可用 —— 人工复核反馈无法记录。",
        )
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    return dict(entry)
