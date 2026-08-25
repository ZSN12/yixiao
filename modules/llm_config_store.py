# -*- coding: utf-8 -*-
"""大模型配置存储(llm_config_store): 让用户在易销界面里自行添加/切换模型。

设计目标:
    把「大模型 key / base / model」从写死的 config/.env 里解放出来, 允许
    超级管理员在 Web 界面里添加多个模型配置(DeepSeek / Kimi / 小米 MiMo /
    任意 OpenAI 兼容端点), 并指定其中一个为「当前启用」。

    业务模块仍只依赖 modules.llm_client 的 chat / chat_json / enabled,
    由 llm_client 内部优先读取本存储的「激活配置」; 未在界面配置时回退
    到 config/.env 的静态配置(LLM_API_KEY 等), 保证开箱即跑。

持久化:
    SQLite 表 llm_configs(与 data_sources 共用 data/sales_agent.db)。
    预置 3 个模板(DeepSeek / Kimi / 小米 MiMo), 仅作占位, 需用户填入真实
    key 后启用。

安全:
    - 仅超级管理员可增删改(见 api_routes/llm_config.py 的 require_admin);
    - API key 存 SQLite 明文(演示级, 生产建议加密或接密钥管理服务)。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, Integer, String, Text

from modules.data_loader import Base, Mapped, _get_session, mapped_column

logger = logging.getLogger(__name__)

# 支持的 provider(仅 openai 兼容接口; kimi 原 Anthropic 接口不再作为界面可选,
# 但保留 settings 回退以兼容历史 .env)
SUPPORTED_PROVIDERS: List[str] = ["openai"]


# 预置模板: 名称 / 提供商 / 默认 base / 默认模型 / 说明
PRESET_TEMPLATES: List[Dict[str, str]] = [
    {
        "name": "DeepSeek",
        "provider": "openai",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "desc": "DeepSeek V3 对话模型, OpenAI 兼容, 国内直连稳定",
    },
    {
        "name": "Kimi (Moonshot)",
        "provider": "openai",
        "api_base": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "desc": "月之暗面 Kimi, OpenAI 兼容接口",
    },
    {
        "name": "小米 MiMo",
        "provider": "openai",
        "api_base": "https://api.xiaomi.com/v1",
        "model": "mimo-7b-instruct",
        "desc": "小米 MiMo 大模型, OpenAI 兼容(实际 base/model 以小米开放平台为准)",
    },
]


class LLMConfig(Base):
    """一条大模型配置记录。"""

    __tablename__ = "llm_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(16), default="openai")  # 仅 openai 兼容
    api_base: Mapped[str] = mapped_column(String(256), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    desc: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否为当前启用
    created_at: Mapped[str] = mapped_column(String(32))


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _row_to_dict(row: LLMConfig, *, mask_key: bool = True) -> Dict[str, Any]:
    """行 -> 对外 JSON(默认掩码 key, 避免整串明文回显)。"""
    key = row.api_key or ""
    if mask_key and key:
        # 只显示前 7 位 + 省略号, 保护 key 不整串回显(但仍可编辑覆盖)
        shown = key[:7] + "…" + key[-4:] if len(key) > 12 else "••••••••"
    else:
        shown = key
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "api_base": row.api_base,
        "api_key_masked": shown,
        "has_key": bool(key),
        "model": row.model,
        "desc": row.desc,
        "active": bool(row.active),
        "created_at": row.created_at,
    }


def ensure_seed_llm_configs() -> None:
    """首次启动时写入预置模板(仅当表为空时)。"""
    sess = _get_session()
    if sess is None:
        logger.warning("数据库未就绪, 跳过模型配置种子写入")
        return
    try:
        if sess.query(LLMConfig).count() > 0:
            return
        first = True
        for tpl in PRESET_TEMPLATES:
            sess.add(LLMConfig(
                name=tpl["name"],
                provider=tpl["provider"],
                api_base=tpl["api_base"],
                api_key="",
                model=tpl["model"],
                desc=tpl["desc"],
                active=first,  # 第一个模板默认激活(占位, 未填 key 时 llm_client 仍回退 settings)
                created_at=_now(),
            ))
            first = False
        sess.commit()
        logger.info("已写入 %d 个预置模型模板", len(PRESET_TEMPLATES))
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        logger.error("写入预置模型配置失败: %s", exc)
    finally:
        sess.close()


def list_configs() -> List[Dict[str, Any]]:
    """列出全部模型配置(含 key 掩码)。"""
    sess = _get_session()
    if sess is None:
        return []
    try:
        rows = sess.query(LLMConfig).order_by(LLMConfig.id.asc()).all()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("列出模型配置失败: %s", exc)
        return []
    finally:
        sess.close()


def get_active_config() -> Optional[Dict[str, Any]]:
    """返回当前激活的模型配置(不含 key 掩码, 供 llm_client 内部使用)。

    优先返回 active=True 且已填 key 的配置; 若激活项未填 key, 则返回 None,
    由 llm_client 回退到 config/.env 的静态配置。
    """
    sess = _get_session()
    if sess is None:
        return None
    try:
        row = sess.query(LLMConfig).filter(LLMConfig.active == True).first()  # noqa: E712
        if row is None:
            return None
        if not (row.api_key or "").strip():
            return None
        return {
            "id": row.id,
            "name": row.name,
            "provider": row.provider,
            "api_base": (row.api_base or "").strip(),
            "api_key": (row.api_key or "").strip(),
            "model": (row.model or "").strip(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("读取激活模型配置失败: %s", exc)
        return None
    finally:
        sess.close()


def add_config(name: str, api_base: str, api_key: str, model: str,
               desc: str = "", provider: str = "openai") -> Dict[str, Any]:
    """新增一条模型配置(不自动激活; 激活需显式调用 activate_config)。"""
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"不支持的 provider: {provider}(当前仅支持 openai 兼容)")
    sess = _get_session()
    if sess is None:
        raise RuntimeError("数据库未就绪")
    try:
        row = LLMConfig(
            name=name or "未命名模型",
            provider=provider,
            api_base=api_base or "",
            api_key=api_key or "",
            model=model or "",
            desc=desc or "",
            active=False,
            created_at=_now(),
        )
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return _row_to_dict(row)
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        logger.error("新增模型配置失败: %s", exc)
        raise RuntimeError(f"新增模型配置失败: {exc}")
    finally:
        sess.close()


def update_config(config_id: int, *, name: Optional[str] = None,
                  api_base: Optional[str] = None, api_key: Optional[str] = None,
                  model: Optional[str] = None, desc: Optional[str] = None,
                  active: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """编辑模型配置(仅更新传入的字段; api_key 传 None 表示不修改)。"""
    sess = _get_session()
    if sess is None:
        raise RuntimeError("数据库未就绪")
    try:
        row = sess.query(LLMConfig).filter(LLMConfig.id == config_id).first()
        if row is None:
            return None
        if name is not None:
            row.name = name
        if api_base is not None:
            row.api_base = api_base
        if api_key is not None:
            row.api_key = api_key
        if model is not None:
            row.model = model
        if desc is not None:
            row.desc = desc
        if active is not None:
            row.active = bool(active)
        sess.commit()
        sess.refresh(row)
        return _row_to_dict(row)
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        logger.error("更新模型配置失败: %s", exc)
        raise RuntimeError(f"更新模型配置失败: {exc}")
    finally:
        sess.close()


def activate_config(config_id: int) -> Optional[Dict[str, Any]]:
    """把指定配置设为「当前启用」, 其余全部取消激活(单激活)。"""
    sess = _get_session()
    if sess is None:
        raise RuntimeError("数据库未就绪")
    try:
        target = sess.query(LLMConfig).filter(LLMConfig.id == config_id).first()
        if target is None:
            return None
        # 全部取消激活
        sess.query(LLMConfig).filter(LLMConfig.active == True).update({"active": False})  # noqa: E712
        target.active = True
        sess.commit()
        sess.refresh(target)
        return _row_to_dict(target)
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        logger.error("切换模型配置失败: %s", exc)
        raise RuntimeError(f"切换模型配置失败: {exc}")
    finally:
        sess.close()


def delete_config(config_id: int) -> bool:
    """删除一条模型配置。返回是否删除成功。"""
    sess = _get_session()
    if sess is None:
        return False
    try:
        row = sess.query(LLMConfig).filter(LLMConfig.id == config_id).first()
        if row is None:
            return False
        sess.delete(row)
        sess.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        logger.error("删除模型配置失败: %s", exc)
        raise RuntimeError(f"删除模型配置失败: {exc}")
    finally:
        sess.close()
