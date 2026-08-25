# -*- coding: utf-8 -*-
"""大模型配置 CRUD 路由(仅超级管理员)。

提供在易销界面里添加 / 编辑 / 删除 / 切换 / 测试大模型配置的能力,
支持 DeepSeek / Kimi / 小米 MiMo 等一切 OpenAI 兼容端点。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from modules import llm_config_store

from .common import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["llm-config"], dependencies=[Depends(require_admin)])


class LLMConfigCreateRequest(BaseModel):
    """新增模型配置请求体。"""

    name: str
    api_base: str
    api_key: str
    model: str
    desc: str = ""
    provider: str = "openai"


class LLMConfigUpdateRequest(BaseModel):
    """编辑模型配置请求体(所有字段可选; api_key 传 None 表示不修改)。"""

    name: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    desc: Optional[str] = None
    active: Optional[bool] = None


class LLMConfigTestRequest(BaseModel):
    """测试连通请求体。"""

    api_base: str
    api_key: str
    model: str
    config_id: Optional[int] = None  # 若传入则优先按数据库中已存 key 测试


@router.get("/api/llm-configs/templates")
def llm_templates() -> Dict[str, Any]:
    """返回预置模型模板(前端「添加模型」的快捷选择)。"""
    return {"templates": llm_config_store.PRESET_TEMPLATES}


@router.get("/api/llm-configs")
def list_llm_configs() -> Dict[str, Any]:
    """列出全部模型配置(含 key 掩码)。"""
    try:
        return {"configs": llm_config_store.list_configs()}
    except Exception as exc:  # noqa: BLE001
        logger.error("列出模型配置失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"列出模型配置失败: {exc}")


@router.post("/api/llm-configs", status_code=201)
def create_llm_config(body: LLMConfigCreateRequest) -> Dict[str, Any]:
    """新增一条模型配置。"""
    try:
        row = llm_config_store.add_config(
            name=body.name, api_base=body.api_base, api_key=body.api_key,
            model=body.model, desc=body.desc, provider=body.provider,
        )
        return {"config": row}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("新增模型配置失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"新增模型配置失败: {exc}")


@router.patch("/api/llm-configs/{config_id}")
def update_llm_config(config_id: int, body: LLMConfigUpdateRequest) -> Dict[str, Any]:
    """编辑一条模型配置(部分字段)。"""
    try:
        row = llm_config_store.update_config(
            config_id,
            name=body.name, api_base=body.api_base, api_key=body.api_key,
            model=body.model, desc=body.desc, active=body.active,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        return {"config": row}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("更新模型配置失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"更新模型配置失败: {exc}")


@router.post("/api/llm-configs/{config_id}/activate")
def activate_llm_config(config_id: int) -> Dict[str, Any]:
    """把指定模型配置设为「当前启用」。"""
    try:
        row = llm_config_store.activate_config(config_id)
        if row is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        return {"config": row}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("切换模型配置失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"切换模型配置失败: {exc}")


@router.delete("/api/llm-configs/{config_id}")
def delete_llm_config(config_id: int) -> Dict[str, Any]:
    """删除一条模型配置。"""
    try:
        ok = llm_config_store.delete_config(config_id)
        if not ok:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("删除模型配置失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"删除模型配置失败: {exc}")


@router.post("/api/llm-configs/test")
def test_llm_config(body: LLMConfigTestRequest) -> Dict[str, Any]:
    """测试连通性: 用指定 key/base/model 发一条最小对话。

    若 api_key 为特殊占位符且提供了 config_id, 则从数据库中取出真实已存 key 测试。
    """
    key_to_use = body.api_key
    base_to_use = body.api_base
    model_to_use = body.model

    if (not key_to_use or key_to_use == "USE_STORED") and body.config_id:
        sess = llm_config_store._get_session()
        if sess:
            try:
                row = sess.query(llm_config_store.LLMConfig).filter(llm_config_store.LLMConfig.id == body.config_id).first()
                if row:
                    key_to_use = row.api_key or ""
                    if not base_to_use:
                        base_to_use = row.api_base or ""
                    if not model_to_use:
                        model_to_use = row.model or ""
            finally:
                sess.close()

    if not key_to_use or not key_to_use.strip():
        raise HTTPException(status_code=400, detail="未提供有效的 API Key")

    try:
        import httpx
        from openai import OpenAI

        # 兼容 httpx 0.28+ 废弃 proxies 参数导致的 openai-python 1.51 初始化报错
        http_client = httpx.Client(timeout=30.0)
        client = OpenAI(
            api_key=key_to_use.strip(),
            base_url=base_to_use.strip(),
            http_client=http_client,
        )
        resp = client.chat.completions.create(
            model=model_to_use.strip(),
            messages=[{"role": "user", "content": "你好, 请只回复两个字: 正常"}],
            temperature=0.0,
            max_tokens=16,
        )
        content = (resp.choices[0].message.content or "").strip()
        return {"ok": True, "reply": content}
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型连通测试失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"连通失败: {exc}")
