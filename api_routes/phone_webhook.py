# -*- coding: utf-8 -*-
"""电话录音 webhook 路由: 接收呼叫中心/CRM 回调 + 低置信度角色复核。

P1 交付:
1. POST /api/webhooks/phone-call —— 接收一通电话录音元数据(带签名鉴权),
   触发 ASR 转写 + 角色判定, 输出 ChatRecord; 低置信度落库待人工复核。
2. GET  /api/phone-reviews —— 列待复核的角色判定记录。
3. POST /api/phone-reviews/{review_id}/confirm —— 人工确认最终角色。

鉴权:
   回调请求须带 X-Signature(请求体 + phone_webhook_secret 的 HMAC-SHA256
   hex 摘要) 与 X-Timestamp(秒级时间戳, 防重放, 容忍 ±300s 偏差)。
   phone_webhook_secret 为空时跳过校验(仅内网/演示环境)。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from modules import data_loader

from .common import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["phone-call"])


# ============================================================
# 请求模型
# ============================================================


class PhoneCallWebhookPayload(BaseModel):
    """电话录音回调请求体(与 phone_call_manifest 同构)。"""

    call_id: str
    customer_id: str
    call_time: str
    sales_id: Optional[str] = None
    direction: Optional[str] = None          # outbound / inbound
    sales_mobile: Optional[str] = None
    customer_mobile: Optional[str] = None
    audio_path: Optional[str] = None
    audio_url: Optional[str] = None
    transcript_demo_id: Optional[str] = None
    asr_provider: Optional[str] = None       # 覆盖 settings.asr_provider


class RoleReviewConfirmRequest(BaseModel):
    """人工确认角色请求体。"""

    resolved_roles: Dict[str, str] = Field(..., description='{"Speaker_0": "销售", "Speaker_1": "客户"}')


# ============================================================
# 签名校验
# ============================================================


def _verify_signature(
    raw_body: bytes,
    signature: Optional[str],
    timestamp: Optional[str],
    secret: str,
) -> bool:
    """校验 webhook 签名(HMAC-SHA256) + 时间戳防重放。

    Args:
        raw_body: 请求原始字节体。
        signature: X-Signature 头(hex 摘要)。
        timestamp: X-Timestamp 头(秒级时间戳)。
        secret: 签名密钥(空则跳过校验, 返回 True)。

    Returns:
        bool: 校验通过 True, 失败 False。
    """
    if not secret:
        # 未配置密钥: 跳过校验(仅内网/演示)
        return True
    if not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    # 防重放: 时间戳偏差超过 ±300 秒拒绝
    if abs(int(time.time()) - ts) > 300:
        logger.warning("webhook 时间戳偏差过大, 拒绝: ts=%s", timestamp)
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ============================================================
# 路由
# ============================================================


@router.post("/api/webhooks/phone-call")
async def phone_call_webhook(
    request: Request,
    x_signature: Optional[str] = Header(default=None, alias="X-Signature"),
    x_timestamp: Optional[str] = Header(default=None, alias="X-Timestamp"),
) -> Dict[str, Any]:
    """接收一通电话录音回调: 验签 → ASR 转写 → 角色判定 → 落库(低置信度复核)。

    请求头:
        X-Signature: HMAC-SHA256(请求体, phone_webhook_secret) 的 hex 摘要。
        X-Timestamp: 秒级时间戳(防重放)。

    Returns:
        dict: {call_id, record(标准 ChatRecord), role_resolution, needs_review}。
    """
    raw_body = await request.body()
    try:
        from config.settings import settings
        secret = settings.phone_webhook_secret
    except Exception:  # noqa: BLE001
        secret = ""

    if not _verify_signature(raw_body, x_signature, x_timestamp, secret):
        raise HTTPException(status_code=401, detail="签名校验失败")

    try:
        payload = PhoneCallWebhookPayload.model_validate_json(raw_body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体非法: {exc}")

    from adapters.phone_call_adapter import load_chat_from_call

    manifest = payload.model_dump()
    try:
        record, resolution = load_chat_from_call(manifest)
    except Exception as exc:  # noqa: BLE001
        logger.error("电话录音处理失败(%s): %s", payload.call_id, exc)
        raise HTTPException(status_code=500, detail=f"电话录音处理失败: {exc}")

    # 低置信度 → 落库待复核
    needs_review = resolution.needs_review()
    review_id = None
    if needs_review:
        transcript = "\n".join(
            f"{m.role}: {m.content}" for m in record.messages
        )
        saved = data_loader.save_role_review(
            call_id=record.record_id,
            customer_id=record.customer_id,
            speaker_roles=resolution.speaker_roles,
            method=resolution.method,
            confidence=resolution.confidence,
            notes=resolution.notes,
            transcript=transcript,
        )
        review_id = saved.get("id") if saved else None

    return {
        "call_id": record.record_id,
        "record": record.model_dump(),
        "role_resolution": {
            "speaker_roles": resolution.speaker_roles,
            "method": resolution.method,
            "confidence": resolution.confidence,
            "notes": resolution.notes,
        },
        "needs_review": needs_review,
        "review_id": review_id,
    }


@router.get("/api/phone-reviews", dependencies=[Depends(require_admin)])
def list_phone_reviews(status: Optional[str] = None) -> Dict[str, Any]:
    """列电话录音角色复核记录(默认全部, 可按 status=pending/resolved 过滤)。

    仅超级管理员可访问。

    Returns:
        dict: {reviews: [ ... ], count: int}。
    """
    reviews = data_loader.list_role_reviews(status=status)
    return {"reviews": reviews, "count": len(reviews)}


@router.post("/api/phone-reviews/{review_id}/confirm", dependencies=[Depends(require_admin)])
def confirm_phone_review(review_id: int, body: RoleReviewConfirmRequest) -> Dict[str, Any]:
    """人工确认某条角色复核记录, 写入最终角色。

    仅超级管理员可访问。

    Args:
        review_id: 复核记录主键。
        body: {"resolved_roles": {"Speaker_0": "销售", ...}}。

    Returns:
        dict: {review_id, resolved, resolved_roles}。
    """
    ok = data_loader.resolve_role_review(review_id, body.resolved_roles)
    if not ok:
        raise HTTPException(status_code=404, detail=f"复核记录不存在或更新失败: {review_id}")
    return {"review_id": review_id, "resolved": True, "resolved_roles": body.resolved_roles}
