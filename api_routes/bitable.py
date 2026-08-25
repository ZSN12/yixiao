# -*- coding: utf-8 -*-
"""飞书多维表格(Bitable) <-> 易销 双向同步 路由。"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from modules import bitable_sync

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bitable"])


@router.post("/api/sync/bitable/pull")
def sync_bitable_pull(dry_run: bool = False) -> Dict[str, Any]:
    """反向同步: 飞书 Bitable -> 易销(拉取表格变更回写本地)。

    说明: 飞书 Bitable 无官方变更 Webhook, 采用拉取式全量比对:
    把飞书表格里「跟进状态 / 归属销售 / 新增线索」的差异回写到本地
    mock_customers.json, 实现双向同步的"飞书改 -> 易销感知"。

    Args:
        dry_run: 为 true 时仅预览差异, 不落库。

    Returns:
        dict: {direction, pulled, status_updated, owner_updated, new_customers, changes}。
    """
    try:
        return bitable_sync.pull_from_bitable(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bitable 反向同步失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bitable 反向同步失败: {exc}")


@router.post("/api/sync/bitable/push")
def sync_bitable_push(dry_run: bool = False) -> Dict[str, Any]:
    """正向同步: 易销 -> 飞书 Bitable(把本地客户 + AI 画像推送到表格)。

    Args:
        dry_run: 为 true 时仅预览差异, 不写表格。

    Returns:
        dict: {direction, created, updated, changes}。
    """
    try:
        return bitable_sync.push_to_bitable(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bitable 正向同步失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bitable 正向同步失败: {exc}")


@router.post("/api/sync/bitable")
def sync_bitable_both(dry_run: bool = False) -> Dict[str, Any]:
    """双向同步: 先 pull(飞书->本地) 再 push(本地->飞书), 实现闭环。

    Returns:
        dict: {pull: {...}, push: {...}}。
    """
    try:
        return bitable_sync.sync_both(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bitable 双向同步失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bitable 双向同步失败: {exc}")
