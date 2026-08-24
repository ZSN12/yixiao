# -*- coding: utf-8 -*-
"""用户账号认证模块(用户隔离基础)。

职责:
1. 定义用户账号模型(User 表), 与 data_loader 共用同一 SQLite 引擎;
2. 登录认证: 账号密码校验 + 签发会话 token;
3. 种子账号: 首次启动自动创建超级管理员 admin / 123456;
4. token 校验: 供后续用户隔离的接口鉴权复用。

安全说明(演示级):
    - 密码采用 sha256 加盐哈希存储(非明文);
    - token 用 secrets.token_hex 生成, 进程内内存缓存(重启后需重新登录);
    - 属演示级安全, 生产环境应改用 JWT + 数据库会话表 + 刷新令牌等。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import datetime
from typing import Any, Dict, Optional

from modules import data_loader
from modules.data_loader import Base, Mapped, String, mapped_column

logger = logging.getLogger(__name__)


# ============================================================
# 用户模型
# ============================================================

class User(Base):
    """系统登录账号表。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))   # sha256(盐 + 密码) 十六进制
    password_salt: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(32), default="super_admin")   # super_admin / sales / viewer
    display_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(32))


# 进程内 token 会话缓存: token -> {username, role, display_name, expire_ts}
_SESSIONS: Dict[str, Dict[str, Any]] = {}
# token 有效期(秒): 默认 24 小时
TOKEN_TTL_SECONDS: int = 24 * 3600


# ============================================================
# 密码哈希
# ============================================================

def _hash_password(password: str, salt: str) -> str:
    """sha256(盐 + 密码) 十六进制。"""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def _make_salt() -> str:
    return secrets.token_hex(16)


# ============================================================
# 用户 CRUD + 种子账号
# ============================================================

def _get_user_by_username(username: str) -> Optional[User]:
    """按用户名查询用户。"""
    data_loader.init_db()
    session = data_loader._get_session()
    if session is None:
        return None
    try:
        return session.query(User).filter(User.username == username).first()
    except Exception as exc:  # noqa: BLE001
        logger.error("查询用户失败: %s", exc)
        return None
    finally:
        session.close()


def ensure_seed_admin() -> None:
    """首次启动自动创建超级管理员 admin / 123456(若不存在)。"""
    data_loader.init_db()
    session = data_loader._get_session()
    if session is None:
        logger.warning("数据库不可用, 跳过种子管理员创建")
        return
    try:
        exists = session.query(User).filter(User.username == "admin").first()
        if exists:
            return
        salt = _make_salt()
        user = User(
            username="admin",
            password_hash=_hash_password("123456", salt),
            password_salt=salt,
            role="super_admin",
            display_name="超级管理员",
            created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        )
        session.add(user)
        session.commit()
        logger.info("已创建超级管理员账号: admin(角色 super_admin)")
    except Exception as exc:  # noqa: BLE001
        logger.error("创建种子管理员失败: %s", exc)
        session.rollback()
    finally:
        session.close()


# ============================================================
# 登录 / 登出 / 校验
# ============================================================

def login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验账号密码, 成功签发 token 并返回会话信息。

    Returns:
        dict | None: {token, username, role, display_name} 或 None(失败)。
    """
    username = (username or "").strip()
    user = _get_user_by_username(username)
    if user is None:
        return None
    if _hash_password(password, user.password_salt) != user.password_hash:
        return None

    token = secrets.token_hex(32)
    _SESSIONS[token] = {
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name or user.username,
        "expire_ts": time.time() + TOKEN_TTL_SECONDS,
    }
    logger.info("用户登录成功: %s(角色 %s)", user.username, user.role)
    return {
        "token": token,
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name or user.username,
    }


def logout(token: str) -> bool:
    """注销会话(清除 token)。"""
    token = (token or "").strip()
    if token in _SESSIONS:
        del _SESSIONS[token]
        return True
    return False


def get_session(token: str) -> Optional[Dict[str, Any]]:
    """校验 token, 返回会话信息(过期自动清理)。

    Returns:
        dict | None: {username, role, display_name} 或 None(无效/过期)。
    """
    token = (token or "").strip()
    sess = _SESSIONS.get(token)
    if sess is None:
        return None
    if sess.get("expire_ts", 0) < time.time():
        del _SESSIONS[token]
        return None
    return {
        "username": sess.get("username"),
        "role": sess.get("role"),
        "display_name": sess.get("display_name"),
    }


def list_users() -> list:
    """列出全部账号(不含密码哈希), 供管理后台展示。"""
    data_loader.init_db()
    session = data_loader._get_session()
    if session is None:
        return []
    try:
        rows = session.query(User).all()
        return [
            {
                "username": u.username,
                "role": u.role,
                "display_name": u.display_name,
                "created_at": u.created_at,
            }
            for u in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.error("列出用户失败: %s", exc)
        return []
    finally:
        session.close()
