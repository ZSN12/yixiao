# -*- coding: utf-8 -*-
"""数据源注册表(data_source_registry): 用户在「数据接入」中心自行选择并添加的数据源配置管理。

背景: 过去数据接入层是硬编码的接口列表(fetch_crm_customers 等空桩), 用户不能选择或添加。
本模块把「数据源」升级为用户可增删改、可启停的可视化注册表:

- 数据源类型(type) 预置 5 种:
    "csv"             CSV 文件导入
    "wework"          企微会话存档 JSON
    "crm"             CRM 客户接口
    "bitable"         飞书多维表 Bitable
    "webhook"         自定义 Webhook/API
- 每个数据源有: name(展示名), type, config(JSON 配置), enabled(启用/停用), builtin(是否系统预置),
  status(状态快照), last_pulled_at(最近拉取时间), created_at。
- 持久化到 SQLite 表 data_sources, 与画像历史等共用 data/sales_agent.db。
- 后端只负责"配置的增删改查与状态展示"; 真正拉取解析由各类 adapter 承接,
  本模块提供 get_enabled_sources(type=None) 供流水线查询可用数据源(当前按需接入)。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import String, Text

from modules.data_loader import Base, Mapped, _get_session, mapped_column

logger = logging.getLogger(__name__)

# 预置的数据源类型定义(供前端下拉选择 + 后端校验)
SOURCE_TYPES: Dict[str, dict] = {
    "csv": {
        "label": "CSV 文件导入",
        "icon": "file",
        "desc": "上传/选择客户 CSV 文件，解析为线索池",
        "fields": [{"key": "file_path", "label": "CSV 文件路径", "placeholder": "data/real/crm_customers.csv", "required": True}],
    },
    "wework": {
        "label": "企微会话存档 JSON",
        "icon": "chat",
        "desc": "对接企业微信会话内容存档 JSON 导出",
        "fields": [{"key": "file_path", "label": "JSON 文件路径", "placeholder": "data/real/wework_chat_export.json", "required": True}],
    },
    "crm": {
        "label": "CRM 客户接口",
        "icon": "database",
        "desc": "对接 CRM fetch_crm_customers / fetch_crm_deals 接口",
        "fields": [
            {"key": "api_base", "label": "CRM API 地址", "placeholder": "https://crm.example.com/api", "required": False},
            {"key": "api_key", "label": "API 凭证", "placeholder": "可选", "required": False},
        ],
    },
    "bitable": {
        "label": "飞书多维表 Bitable",
        "icon": "table",
        "desc": "对接飞书多维表双向同步",
        "fields": [
            {"key": "base_token", "label": "Base Token", "placeholder": "feishu base token", "required": True},
            {"key": "leads_table", "label": "线索表 ID", "placeholder": "tblxxx", "required": False},
        ],
    },
    "webhook": {
        "label": "自定义 Webhook/API",
        "icon": "link",
        "desc": "填任意返回 JSON 的 API 地址接入",
        "fields": [{"key": "endpoint", "label": "接口地址", "placeholder": "https://api.example.com/leads", "required": True}],
    },
    "phone_call": {
        "label": "电话录音",
        "icon": "phone",
        "desc": "对接呼叫中心/CRM 电话录音（ASR + 说话人角色识别）",
        "fields": [
            {"key": "manifest_path", "label": "通话清单 JSON", "placeholder": "data/real/phone_call_manifest.json", "required": True},
            {"key": "asr_provider", "label": "ASR 提供商", "placeholder": "mock|aliyun|tencent", "required": False},
        ],
    },
    "qikebao": {
        "label": "企客宝 CRM",
        "icon": "building",
        "desc": "对接企客宝 OpenAPI，作为客户主数据源（mock/CSV/企微兜底）",
        "fields": [
            {"key": "client_id", "label": "Client ID", "placeholder": "企客宝应用 client_id", "required": True},
            {"key": "client_secret", "label": "Client Secret", "placeholder": "企客宝应用 client_secret", "required": True},
            {"key": "corp_id", "label": "企业 corp_id", "placeholder": "企客宝租户 corp_id", "required": True},
        ],
    },
}

# 系统预置数据源(首次启动写入, 作为可编辑/可启停的起点)
BUILTIN_SOURCES: List[dict] = [
    {"name": "CRM 客户列表", "type": "crm", "config": {"api_base": "https://crm.example.com/api", "api_key": ""}, "enabled": True, "status": "预留", "builtin": True},
    {"name": "企微会话存档", "type": "wework", "config": {"file_path": "data/real/wework_chat_export.json"}, "enabled": True, "status": "预留", "builtin": True},
    {"name": "CRM 成交商机", "type": "crm", "config": {"api_base": "https://crm.example.com/api", "api_key": ""}, "enabled": True, "status": "预留", "builtin": True},
    {"name": "飞书多维表同步", "type": "bitable", "config": {"base_token": "", "leads_table": "tbluJCdsnsMaWYYD"}, "enabled": True, "status": "预留", "builtin": True},
    {"name": "电话录音(演示)", "type": "phone_call", "config": {"manifest_path": "data/real/phone_call_manifest.json", "asr_provider": "mock"}, "enabled": False, "status": "待接入", "builtin": True},
    {"name": "企客宝 CRM", "type": "qikebao", "config": {"client_id": "", "client_secret": "", "corp_id": ""}, "enabled": False, "status": "待接入", "builtin": True},
]


class DataSource(Base):
    """数据源配置注册表。"""

    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32))
    config_json: Mapped[str] = mapped_column(Text, default="{}")   # 配置的 JSON 字符串
    enabled: Mapped[bool] = mapped_column(default=True)            # 启用/停用
    builtin: Mapped[bool] = mapped_column(default=False)           # 是否系统预置
    status: Mapped[str] = mapped_column(String(32), default="待接入")  # 待接入/已接入/运行中/异常/停用
    last_pulled_at: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[str] = mapped_column(String(32))


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def ensure_seed_sources() -> None:
    """首次启动时写入系统预置数据源(已有则跳过)。"""
    sess = _get_session()
    if sess is None:
        logger.warning("数据库未就绪, 跳过数据源种子写入")
        return
    try:
        count = sess.query(DataSource).count()
        if count > 0:
            return
        for item in BUILTIN_SOURCES:
            sess.add(DataSource(
                name=item["name"],
                type=item["type"],
                config_json=json.dumps(item["config"], ensure_ascii=False),
                enabled=item["enabled"],
                builtin=item.get("builtin", False),
                status=item.get("status", "待接入"),
                last_pulled_at="",
                created_at=_now(),
            ))
        sess.commit()
        logger.info("已写入 %d 个系统预置数据源", len(BUILTIN_SOURCES))
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        logger.error("写入预置数据源失败: %s", exc)
    finally:
        sess.close()


def _row_to_dict(row: DataSource) -> Dict[str, Any]:
    """数据源行 -> 对外 JSON 字典(含 source_type 元信息)。"""
    try:
        config = json.loads(row.config_json or "{}")
    except Exception:  # noqa: BLE001
        config = {}
    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "type_label": (SOURCE_TYPES.get(row.type) or {}).get("label", row.type),
        "type_desc": (SOURCE_TYPES.get(row.type) or {}).get("desc", ""),
        "config": config,
        "enabled": bool(row.enabled),
        "builtin": bool(row.builtin),
        "status": row.status,
        "last_pulled_at": row.last_pulled_at,
        "created_at": row.created_at,
    }


def list_sources(include_disabled: bool = True) -> List[Dict[str, Any]]:
    """列出全部数据源(默认含停用)。"""
    sess = _get_session()
    if sess is None:
        return []
    try:
        q = sess.query(DataSource)
        if not include_disabled:
            q = q.filter(DataSource.enabled == True)  # noqa: E712
        rows = q.order_by(DataSource.id.asc()).all()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("列出数据源失败: %s", exc)
        return []
    finally:
        sess.close()


def get_source(source_id: int) -> Optional[Dict[str, Any]]:
    sess = _get_session()
    if sess is None:
        return None
    try:
        row = sess.query(DataSource).filter(DataSource.id == source_id).first()
        return _row_to_dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.error("查询数据源失败: %s", exc)
        return None
    finally:
        sess.close()


def add_source(name: str, source_type: str, config: Dict[str, Any], enabled: bool = True) -> Optional[Dict[str, Any]]:
    """新增一个用户自定义数据源。"""
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"不支持的数据源类型: {source_type}")
    sess = _get_session()
    if sess is None:
        raise RuntimeError("数据库未就绪")
    try:
        row = DataSource(
            name=name or (SOURCE_TYPES[source_type]["label"] + " #" + str(int(time.time()))),
            type=source_type,
            config_json=json.dumps(config or {}, ensure_ascii=False),
            enabled=enabled,
            builtin=False,
            status="已接入" if enabled else "停用",
            last_pulled_at="",
            created_at=_now(),
        )
        sess.add(row)
        sess.commit()
        sess.refresh(row)
        return _row_to_dict(row)
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        logger.error("新增数据源失败: %s", exc)
        raise RuntimeError(f"新增数据源失败: {exc}")
    finally:
        sess.close()


def update_source(source_id: int, *, name: Optional[str] = None, config: Optional[Dict[str, Any]] = None,
                  enabled: Optional[bool] = None, status: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """编辑数据源(name/config/enabled/status)。"""
    sess = _get_session()
    if sess is None:
        raise RuntimeError("数据库未就绪")
    try:
        row = sess.query(DataSource).filter(DataSource.id == source_id).first()
        if row is None:
            return None
        if name is not None:
            row.name = name
        if config is not None:
            row.config_json = json.dumps(config, ensure_ascii=False)
        if enabled is not None:
            row.enabled = enabled
            row.status = "停用" if not enabled else (row.status if row.status not in ("", "停用") else "已接入")
        if status is not None:
            row.status = status
        sess.commit()
        sess.refresh(row)
        return _row_to_dict(row)
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        raise RuntimeError(f"更新数据源失败: {exc}")
    finally:
        sess.close()


def delete_source(source_id: int) -> bool:
    """删除数据源。"""
    sess = _get_session()
    if sess is None:
        raise RuntimeError("数据库未就绪")
    try:
        row = sess.query(DataSource).filter(DataSource.id == source_id).first()
        if row is None:
            return False
        sess.delete(row)
        sess.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        sess.rollback()
        raise RuntimeError(f"删除数据源失败: {exc}")
    finally:
        sess.close()


def toggle_source(source_id: int, enabled: bool) -> Optional[Dict[str, Any]]:
    """启停一个数据源。"""
    return update_source(source_id, enabled=enabled)


def get_enabled_sources(source_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """流水线查询当前启用中的数据源(可按类型过滤)。"""
    return [s for s in list_sources(include_disabled=False)
            if (source_type is None or s["type"] == source_type)]
