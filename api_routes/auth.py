# -*- coding: utf-8 -*-
"""用户认证路由: 登录 / 登出 / 当前用户。"""

from __future__ import annotations

from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from modules import user_auth

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str
    password: str


@router.post("/api/login")
def api_login(req: LoginRequest) -> Dict[str, Any]:
    """用户登录: 校验账号密码, 成功返回 token + 用户信息。

    Args:
        req: {username, password}。

    Returns:
        dict: {token, username, role, display_name}。

    Raises:
        HTTPException(401): 账号或密码错误。
    """
    result = user_auth.login(req.username, req.password)
    if result is None:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return result


@router.post("/api/logout")
def api_logout(request: Request) -> Dict[str, Any]:
    """用户登出: 清除会话 token。"""
    token = (request.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
    user_auth.logout(token)
    return {"status": "ok"}


@router.get("/api/me")
def api_me(request: Request) -> Dict[str, Any]:
    """获取当前登录用户信息(校验 token)。

    Returns:
        dict: {username, role, display_name} 或 {authenticated: False}。
    """
    token = (request.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
    sess = user_auth.get_session(token)
    if sess is None:
        return {"authenticated": False}
    return {"authenticated": True, **sess}
