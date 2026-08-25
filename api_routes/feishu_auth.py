# -*- coding: utf-8 -*-
"""飞书网页免登(Feishu OAuth)路由: 生成授权 URL + code 兑换身份登录。

流程:
1. GET  /api/feishu/oauth-url        -> 返回飞书授权页 URL + 一次性 state;
2. 前端跳转授权页, 用户授权后回调到 /#/feishu-oauth?code=xxx&state=xxx;
3. POST /api/feishu/oauth-login      -> 用 code+state 换飞书身份, 匹配销售/管理员并签发易销 token。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from modules import feishu_oauth, user_auth
from api_routes.common import _find_sales_by_open_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feishu-auth"])


class OAuthLoginRequest(BaseModel):
    """飞书免登登录请求体。"""

    code: str
    state: str


@router.get("/api/feishu/oauth-url")
def feishu_oauth_url() -> Dict[str, Any]:
    """生成飞书一键登录的授权页 URL(含防 CSRF 的一次性 state)。

    Returns:
        dict: {enabled, url, state} 或 {enabled: False, reason}。
    """
    result = feishu_oauth.build_authorize_url()
    return result


@router.post("/api/feishu/oauth-login")
def feishu_oauth_login(req: OAuthLoginRequest) -> Dict[str, Any]:
    """飞书免登核心登录: code + state 换飞书身份, 匹配易销账号并签发 token。

    - 匹配到销售成员(open_id) -> 签发 sales 角色会话;
    - 匹配到超管(open_id 对应 admin, 通过手机号反查) -> 签发 super_admin;
    - 均未匹配 -> 返回需要绑定的引导信息。

    Returns:
        dict: {token, username, role, display_name, sales_id?, sales_mode?}
            或 {authenticated: False, reason, need_bind}。
    """
    auth = feishu_oauth.authenticate_with_code(req.code, req.state)
    if not auth.get("ok"):
        raise HTTPException(status_code=401, detail=auth.get("reason", "飞书授权失败"))

    open_id = auth["open_id"]
    display_name = auth.get("name") or auth.get("en_name") or "飞书用户"

    # 1. 匹配销售成员
    sales = _find_sales_by_open_id(open_id)
    if sales is not None:
        session = user_auth.issue_session(
            username=sales.get("sales_id"),
            role="sales",
            display_name=sales.get("name") or display_name,
        )
        session["sales_id"] = sales.get("sales_id")
        session["sales_name"] = sales.get("name")
        session["sales_mode"] = True
        session["open_id"] = open_id
        return session

    # 2. 匹配超级管理员: 用手机号反查(admin 账号) —— 若飞书用户手机号命中的销售已处理,
    #    这里再兜底: 若飞书 open_id 未匹配销售, 不自动给 super_admin(安全), 提示绑定。
    return {
        "authenticated": False,
        "reason": "该飞书账号尚未关联任何易销销售成员",
        "need_bind": True,
        "open_id": open_id,
        "display_name": display_name,
    }
