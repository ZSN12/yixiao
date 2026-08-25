# -*- coding: utf-8 -*-
"""飞书网页免登 OAuth 认证模块(feishu_oauth.py)。

实现「飞书一键登录」—— 用户在飞书工作台/网页应用打开易销登录页，
点击飞书授权后，无需再输入 admin 账号密码即可凭飞书身份登录。

飞书网页免登流程:
1. 前端跳转飞书授权页:
     GET {FEISHU_OAUTH_BASE}/open-apis/authen/v1/index
        ?app_id={app_id}&redirect_uri={redirect_uri}&state={state}
2. 用户同意授权后, 飞书回调 redirect_uri?code=xxx&state=xxx
3. 用 code 换 user_access_token:
     POST https://open.feishu.cn/open-apis/authen/v1/oidc/access_token
        body: {grant_type:"authorization_code", code, client_id, client_secret}
4. 用 user_access_token 查当前用户信息:
     GET https://open.feishu.cn/open-apis/authen/v1/user_info
        header: Authorization: Bearer {user_access_token}
    返回 { open_id, name, en_name, avatar_url, mobile, ... }

设计准则: 与 feishu_app_notifier 一致, 纯标准库 (urllib + json + ssl);
凭证缺失/网络失败时降级返回 None 并记日志, 不抛异常。
"""

from __future__ import annotations

import json
import logging
import secrets
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# 飞书 OAuth 相关端点
FEISHU_BASE = "https://open.feishu.cn"
OAUTH_AUTHORIZE = "https://accounts.feishu.cn/open-apis/authen/v1/index"
OAUTH_TOKEN_URL = FEISHU_BASE + "/open-apis/authen/v1/oidc/access_token"
OAUTH_USERINFO_URL = FEISHU_BASE + "/open-apis/authen/v1/user_info"

# 一次性授权 state 缓存(防 CSRF): state -> expire_ts
_STATE_CACHE: Dict[str, float] = {}
# 一次性授权 code 缓存(防重放): code -> expire_ts
_CODE_CACHE: Dict[str, float] = {}
# state 有效期(秒)
STATE_TTL = 10 * 60
# code 有效期(秒)
CODE_TTL = 5 * 60


def _ssl_context():
    """生成 SSL 上下文(与 feishu_app_notifier 一致, 兼容内网/自签名)。"""
    import ssl
    ctx = ssl.create_default_context()
    if not getattr(settings, "verify_ssl", False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def oauth_enabled() -> bool:
    """是否具备飞书免登条件(已配置 app_id 且具备回调地址)。"""
    return bool((getattr(settings, "feishu_app_id", "") or "").strip()
                and (getattr(settings, "feishu_webapp_url", "") or "").strip())


def build_authorize_url(redirect_uri: Optional[str] = None) -> Dict[str, Any]:
    """构造飞书授权页 URL(含一次性 state)。

    Args:
        redirect_uri: 授权后回调地址; 缺省用 settings.feishu_webapp_url。

    Returns:
        dict: {url, state} 或 {"enabled": False, "reason": "..."}。
    """
    app_id = (getattr(settings, "feishu_app_id", "") or "").strip()
    if not app_id:
        return {"enabled": False, "reason": "未配置飞书应用 App ID"}

    base = (redirect_uri or "").strip() or (getattr(settings, "feishu_webapp_url", "") or "").strip()
    if not base:
        return {"enabled": False, "reason": "未配置飞书网页应用回调地址(feishu_webapp_url)"}

    state = secrets.token_urlsafe(16)
    now = _now_ts()
    _STATE_CACHE[state] = now + STATE_TTL

    # 回调地址统一指向前端登录页(带 oauth 标记), 前端解析 code 后调后端登录
    callback = base.rstrip("/") + "/#/feishu-oauth"
    params = {
        "app_id": app_id,
        "redirect_uri": callback,
        "state": state,
    }
    url = OAUTH_AUTHORIZE + "?" + urllib.parse.urlencode(params)
    return {"enabled": True, "url": url, "state": state, "callback": callback}


def _now_ts() -> float:
    import time
    return time.time()


def _consume_state(state: str) -> bool:
    """校验并消费一次性 state(防 CSRF); 失败返回 False。"""
    state = (state or "").strip()
    if not state or state not in _STATE_CACHE:
        return False
    expire = _STATE_CACHE.pop(state, 0)
    return expire > _now_ts()


def _exchange_code_for_token(code: str) -> Optional[str]:
    """用 authorization code 换取 user_access_token。"""
    app_id = (getattr(settings, "feishu_app_id", "") or "").strip()
    app_secret = (getattr(settings, "feishu_app_secret", "") or "").strip()
    if not app_id or not app_secret:
        logger.error("飞书免登缺少 app_id/app_secret 配置")
        return None

    payload = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app_id,
        "client_secret": app_secret,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") == 0 and data.get("data", {}).get("access_token"):
            return data["data"]["access_token"]
        logger.error("飞书 code 换 token 失败: code=%s, msg=%s",
                     data.get("code"), data.get("msg"))
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("请求飞书 oidc token 接口异常: %s", exc)
        return None


def get_user_info_by_token(access_token: str) -> Optional[Dict[str, Any]]:
    """用 user_access_token 查询当前飞书用户信息。"""
    req = urllib.request.Request(
        OAUTH_USERINFO_URL,
        headers={"Authorization": "Bearer " + access_token},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") == 0 and data.get("data"):
            return data["data"]
        logger.error("飞书查询用户信息失败: code=%s, msg=%s",
                     data.get("code"), data.get("msg"))
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("请求飞书 user_info 接口异常: %s", exc)
        return None


def authenticate_with_code(code: str, state: str) -> Dict[str, Any]:
    """飞书免登核心: 用回调的 code + state 完成身份认证。

    步骤:
    1. 校验并消费一次性 state(防 CSRF);
    2. 用 code 换 user_access_token(同时校验 code 一次性, 防重放);
    3. 查用户信息(open_id / name / avatar / mobile)。

    Returns:
        dict: 成功 {ok: True, open_id, name, avatar_url, mobile, raw} 或
              失败 {ok: False, reason: "..."}。
    """
    # 1. 校验 state
    if not _consume_state(state):
        return {"ok": False, "reason": "授权状态校验失败，请重新发起登录"}

    code = (code or "").strip()
    if not code:
        return {"ok": False, "reason": "缺少授权码 code"}

    # 2. 防重放: 同一 code 只能用一次
    now = _now_ts()
    if code in _CODE_CACHE:
        return {"ok": False, "reason": "授权码已使用，请重新发起登录"}
    _CODE_CACHE[code] = now + CODE_TTL

    # 3. 换 token + 查用户
    token = _exchange_code_for_token(code)
    if not token:
        return {"ok": False, "reason": "飞书授权码换取令牌失败"}

    user = get_user_info_by_token(token)
    if not user:
        return {"ok": False, "reason": "无法获取飞书用户信息"}

    open_id = str(user.get("open_id") or "").strip()
    if not open_id:
        return {"ok": False, "reason": "飞书未返回有效 open_id"}

    return {
        "ok": True,
        "open_id": open_id,
        "name": str(user.get("name") or "").strip(),
        "en_name": str(user.get("en_name") or "").strip(),
        "avatar_url": str(user.get("avatar_url") or "").strip(),
        "mobile": str(user.get("mobile") or "").strip(),
        "raw": user,
    }
