# -*- coding: utf-8 -*-
"""企客宝 OpenAPI 客户端(qikebao_client): Token + 分页拉取客户。

职责: 封装企客宝 OpenAPI 的 HTTP 调用, 业务层(adapter/data_loader)只调
Python 函数, 不接触 HTTP 细节。

实现要点(与全项目风格一致):
- 用标准库 urllib.request(零第三方依赖), 与 kimi_client/dingtalk_notifier 对齐;
- Token 进程内缓存, 遇 401 刷新重试一次;
- 网络错误最多重试 3 次, 失败记日志后向上抛(RuntimeError), 由 adapter 层兜底
  (降级 mock 或返回空列表);
- 参考文档:
    https://qkbdoc.yunshouzhi.net/doc-2155604        (概览)
    https://qkbdoc.yunshouzhi.net/api-419903231       (获取客户列表)

字段名与请求/响应结构为「待核对」初版 —— 拿到真实响应后仅需微调本模块,
不影响 adapter 与调用方。
"""

from __future__ import annotations

import json
import logging
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 进程内 Token 缓存 {token: str, expires_at: float(epoch 秒)}
_token_cache: Dict[str, Any] = {"token": "", "expires_at": 0.0}

_REQUEST_TIMEOUT = 30.0
_MAX_RETRIES = 3


def _ssl_context() -> ssl.SSLContext:
    """构造 SSL 上下文(兼容内网/自签名证书环境)。"""
    from config.settings import settings
    if getattr(settings, "verify_ssl", False):
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _settings():
    """延迟导入 settings(避免循环依赖)。"""
    from config.settings import settings
    return settings


def _api_base() -> str:
    """返回企客宝 API 域名(空则用默认)。"""
    s = _settings()
    base = (s.qikebao_api_base or "").strip()
    if base:
        return base.rstrip("/")
    # 默认域名(与文档 token 域名同源, 待核对)
    return "https://api.yunshouzhi.net"


# ============================================================
# Token
# ============================================================


def get_access_token(force_refresh: bool = False) -> str:
    """获取企客宝访问令牌(client_credentials 模式, 进程内缓存)。

    Args:
        force_refresh: 为 True 时强制重新获取(忽略缓存)。

    Returns:
        str: access_token。

    Raises:
        RuntimeError: 凭证缺失或获取失败。
    """
    if not force_refresh and _token_cache["token"] and _token_cache["expires_at"] > time.time() + 60:
        return _token_cache["token"]

    s = _settings()
    client_id = s.qikebao_client_id
    client_secret = s.qikebao_client_secret
    if not client_id or not client_secret:
        raise RuntimeError("企客宝凭证缺失(qikebao_client_id / qikebao_client_secret), 请在 config/.env 配置")

    url = s.qikebao_token_url.rstrip("/")
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "YixiaoQKB/1.0"},
    )
    with urllib.request.urlopen(req, context=_ssl_context(), timeout=_REQUEST_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    token = body.get("access_token") or body.get("token") or body.get("data", {}).get("access_token")
    if not token:
        raise RuntimeError(f"企客宝 Token 响应缺少 access_token: {list(body.keys())}")
    expires_in = int(body.get("expires_in", 7200) or 7200)
    _token_cache["token"] = str(token)
    _token_cache["expires_at"] = time.time() + expires_in
    logger.info("企客宝 access_token 已获取(有效期 %d 秒)", expires_in)
    return str(token)


def _request_json(method: str, url: str, *, body: Optional[Dict] = None, retries: int = _MAX_RETRIES) -> Dict:
    """带鉴权头的通用 JSON 请求, 遇 401 刷新 token 重试一次。

    Args:
        method: HTTP 方法。
        url: 请求地址。
        body: JSON 请求体(可选)。
        retries: 网络错误重试次数。

    Returns:
        dict: JSON 响应体。

    Raises:
        RuntimeError: 重试耗尽或 HTTP 错误。
    """
    token = get_access_token()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "YixiaoQKB/1.0",
                },
            )
            with urllib.request.urlopen(req, context=_ssl_context(), timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                logger.info("企客宝 token 失效, 刷新重试")
                token = get_access_token(force_refresh=True)
                continue
            raise RuntimeError(f"企客宝 HTTP {exc.code}: {exc.reason}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < retries - 1:
                logger.warning("企客宝请求失败(%s), 第 %d 次重试", exc, attempt + 1)
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(f"企客宝请求失败: {exc}")
    raise RuntimeError("企客宝请求重试耗尽")


# ============================================================
# 租户 / 客户
# ============================================================


def get_tenant_corp_list() -> List[Dict]:
    """获取租户(企业)列表(多企业时用于确定 corp_id)。"""
    url = f"{_api_base()}/tenant/corp/list"
    body = _request_json("GET", url)
    return body.get("data", {}).get("list", []) if isinstance(body.get("data"), dict) else (body.get("data") or [])


def get_customer_list(
    corp_id: str,
    page: int = 1,
    page_size: int = 100,
    **filters: Any,
) -> Dict:
    """分页拉取某企业的一页客户。

    Args:
        corp_id: 企业 ID。
        page: 页码(从 1 起)。
        page_size: 每页条数。
        **filters: 附加过滤参数(如 keyword/industry 等, 透传)。

    Returns:
        dict: 原始响应(结构待核对, 通常含 data.list / data.total)。
    """
    params = {
        "corp_id": corp_id,
        "page": page,
        "page_size": page_size,
    }
    params.update({k: v for k, v in filters.items() if v is not None})
    query = urllib.parse.urlencode(params)
    url = f"{_api_base()}/customer/list?{query}"
    return _request_json("GET", url)


def get_all_customers(corp_id: str, page_size: int = 100) -> List[Dict]:
    """自动分页拉取某企业全量客户。

    Args:
        corp_id: 企业 ID。
        page_size: 每页条数(最大 100)。

    Returns:
        list[dict]: 客户原始 dict 列表(已拍平 data.list)。
    """
    all_rows: List[Dict] = []
    page = 1
    while True:
        resp = get_customer_list(corp_id, page=page, page_size=page_size)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        rows = data.get("list") or data.get("records") or []
        if not rows:
            break
        all_rows.extend(rows)
        total = data.get("total")
        if total is not None and len(all_rows) >= int(total):
            break
        if len(rows) < page_size:
            break
        page += 1
    logger.info("企客宝客户拉取完成: corp=%s, 共 %d 条", corp_id, len(all_rows))
    return all_rows


# ============================================================
# P1 预留: 聊天记录
# ============================================================


def get_chat_messages(corp_id: str, contact_id: str, **kwargs: Any) -> List[Dict]:
    """拉取某联系人的聊天/会话消息(P1 预留, 未开通会话存档时返回空)。

    Args:
        corp_id: 企业 ID。
        contact_id: 联系人 ID。
        **kwargs: 附加参数(分页/时间范围等, 透传)。

    Returns:
        list[dict]: 消息原始 dict 列表(结构待核对)。
    """
    logger.info("get_chat_messages(corp=%s, contact=%s) 被调用 —— 会话存档未启用, 返回空", corp_id, contact_id)
    return []
