# -*- coding: utf-8 -*-
"""统一 Kimi K2.7 Code 客户端(Anthropic Messages 接口)。

封装与 Kimi coding API 的通信细节, 供画像分析 / 销售分析 / 话术生成等
模块复用, 避免各处重复实现 HTTP 调用、thinking 块过滤、JSON 清洗等逻辑。

接口约定:
    - 端点: {kimi_api_base}/v1/messages (Anthropic Messages 格式);
    - 响应 content 可能含 thinking 块, 只取 type=="text" 的块拼接;
    - 无 OpenAI response_format 参数, JSON 输出靠 prompt 约束 + 调用方清洗。

设计:
    - 延迟读取 settings, 避免循环依赖;
    - 失败抛异常, 由调用方降级(与现有双引擎模式一致)。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.request
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)


def kimi_enabled() -> bool:
    """是否配置了 Kimi key(独立于 mock_mode, 语义任务优先用 Kimi)。"""
    return bool(getattr(settings, "kimi_api_key", "") or "")


def _ssl_context() -> ssl.SSLContext:
    """宽松 SSL 上下文(兼容本地代理/证书环境)。"""
    ctx = ssl.create_default_context()
    try:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    except Exception:  # noqa: BLE001
        pass
    return ctx


def chat(
    system: str,
    user: str,
    max_tokens: int = 3000,
    temperature: float = 0.2,
    thinking_disabled: bool = True,
) -> str:
    """调 Kimi coding API 完成一次对话, 返回纯文本(已过滤 thinking 块)。

    Args:
        system: system 提示词。
        user: user 消息内容。
        max_tokens: 最大输出 token。
        temperature: 采样温度(JSON 结构化任务建议 0.2)。
        thinking_disabled: 是否关闭思考(结构化任务关闭省 token 且更快)。

    Returns:
        str: 助手回复的纯文本内容。

    Raises:
        Exception: 网络/超时/HTTP 错误/空回复, 由调用方降级。
    """
    base = settings.kimi_api_base.rstrip("/")
    url = f"{base}/v1/messages"

    payload: Dict[str, Any] = {
        "model": settings.kimi_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = temperature
    if thinking_disabled:
        payload["thinking"] = {"type": "disabled"}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.kimi_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "KimiCLI/1.5",
        },
    )

    with urllib.request.urlopen(req, context=_ssl_context(), timeout=settings.llm_timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    parts: List[str] = []
    for block in body.get("content", []):
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"].strip())
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        raise RuntimeError(f"Kimi 返回空内容: stop_reason={body.get('stop_reason')}")
    return text


def chat_json(
    system: str,
    user: str,
    max_tokens: int = 3000,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    """调 Kimi 完成一次对话并解析为 JSON 对象。

    清洗 markdown 代码块(```json ... ```), 解析失败抛异常由调用方降级。

    Returns:
        dict: 解析后的 JSON 对象。

    Raises:
        Exception: 非 JSON / 空内容, 由调用方降级。
    """
    raw = chat(system, user, max_tokens=max_tokens, temperature=temperature)
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL)
    # 若仍有残留代码块标记, 尝试提取首个 { 到末个 } 之间的内容
    if not clean.startswith("{"):
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start:end + 1]
    return json.loads(clean)
