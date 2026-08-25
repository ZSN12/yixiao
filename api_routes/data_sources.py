# -*- coding: utf-8 -*-
"""数据源接入中心 CRUD 路由。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from modules import data_source_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data-sources"])


class DataSourceCreateRequest(BaseModel):
    """新增数据源请求体。"""

    name: str
    type: str
    config: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class DataSourceUpdateRequest(BaseModel):
    """编辑数据源请求体(所有字段可选)。"""

    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    status: Optional[str] = None


@router.get("/api/data-sources/types")
def data_source_types() -> Dict[str, Any]:
    """返回预置数据源类型定义(前端「添加数据源」弹窗的下拉/表单依据)。"""
    return {"types": data_source_registry.SOURCE_TYPES}


@router.get("/api/data-sources")
def list_data_sources() -> Dict[str, Any]:
    """列出全部数据源(含停用)。"""
    try:
        return {"sources": data_source_registry.list_sources()}
    except Exception as exc:  # noqa: BLE001
        logger.error("列出数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"列出数据源失败: {exc}")


@router.post("/api/data-sources", status_code=201)
def create_data_source(body: DataSourceCreateRequest) -> Dict[str, Any]:
    """新增一个数据源。"""
    try:
        row = data_source_registry.add_source(body.name, body.type, body.config, body.enabled)
        return {"source": row}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("新增数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"新增数据源失败: {exc}")


@router.patch("/api/data-sources/{source_id}")
def update_data_source(source_id: int, body: DataSourceUpdateRequest) -> Dict[str, Any]:
    """编辑/启停一个数据源。"""
    try:
        row = data_source_registry.update_source(
            source_id,
            name=body.name,
            config=body.config,
            enabled=body.enabled,
            status=body.status,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="数据源不存在")
        return {"source": row}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("更新数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"更新数据源失败: {exc}")


@router.delete("/api/data-sources/{source_id}")
def delete_data_source(source_id: int) -> Dict[str, Any]:
    """删除一个数据源。"""
    try:
        ok = data_source_registry.delete_source(source_id)
        if not ok:
            raise HTTPException(status_code=404, detail="数据源不存在")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("删除数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"删除数据源失败: {exc}")
