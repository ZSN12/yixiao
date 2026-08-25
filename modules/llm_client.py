# -*- coding: utf-8 -*-
"""统一可插拔 LLM 客户端(llm_client): Kimi / OpenAI 兼容 双后端网关。

设计目标:
    业务模块(画像分析 / 销售画像 / 话术生成 / 编排 LLM 总结)只依赖本模块的
    `chat` / `chat_json` / `enabled` 三个接口, 不关心底层是 Kimi 还是
    OpenAI 兼容接口。切换大模型只需改配置 `LLM_PROVIDER`, 业务代码零改动。

后端:
    1. kimi    (默认): Anthropic Messages 接口(api.kimi.com/coding 等),
       复用 modules.kimi_client 的 thinking 过滤与 JSON 清洗逻辑。
    2. openai  (预留): OpenAI 兼容 Chat Completions 接口, 可接 OpenAI /
       DeepSeek / 通义千问 / Moonshot / 本地 vLLM 等一切兼容端点。

配置(见 config/settings.py / config/.env.example):
    LLM_PROVIDER     -> "kimi" | "openai", 默认 "kimi"
    KIMI_API_BASE / KIMI_API_KEY / KIMI_MODEL
    LLM_API_BASE  / LLM_API_KEY  / LLM_MODEL (OpenAI 兼容端点)

约定:
    - chat:     返回纯文本, 供话术生成 / LLM 总结使用;
    - chat_json: 返回解析后的 dict, 供结构化画像分析使用;
    - enabled:   判断当前 provider 是否已配置 key(未配置时业务走规则引擎);
    - 所有失败抛异常, 由调用方双引擎降级(与全项目一致)。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from config.settings import settings

logger = logging.getLogger(__name__)


# ============================================================
# Provider 选择
# ============================================================

def _provider() -> str:
    """返回当前生效的 provider 名称("kimi" | "openai"), 非法值回落 kimi。

    优先判断界面配置(activate 的 openai 配置), 其次回退 settings 的静态配置。
    """
    cfg = _active_store_config()
    if cfg:
        return cfg["provider"]
    raw = (getattr(settings, "llm_provider", "") or "kimi").strip().lower()
    if raw not in ("kimi", "openai"):
        logger.warning("未知 LLM_PROVIDER=%r, 回落默认 kimi", raw)
        return "kimi"
    return raw


def _active_store_config() -> dict:
    """读取界面激活的模型配置; 未配置或未填 key 时返回 None。

    延迟导入 llm_config_store, 避免与 data_loader 建表顺序产生循环依赖。
    """
    try:
        from modules import llm_config_store
        return llm_config_store.get_active_config()
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取界面模型配置失败, 回退 settings: %s", exc)
        return None


def enabled() -> bool:
    """当前 provider 是否已配置 key(未配置时业务应走规则引擎)。"""
    cfg = _active_store_config()
    if cfg:
        return bool(cfg.get("api_key", "").strip())
    if _provider() == "openai":
        return bool((getattr(settings, "llm_api_key", "") or "").strip())
    return bool((getattr(settings, "kimi_api_key", "") or "").strip())


def _chat_kimi(system: str, user: str, max_tokens: int, temperature: float) -> str:
    """Kimi 后端: 复用 modules.kimi_client(Anthropic Messages 格式)。"""
    from modules import kimi_client
    return kimi_client.chat(system, user, max_tokens=max_tokens, temperature=temperature)


def _chat_openai(
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    json_mode: bool = False,
) -> str:
    """OpenAI 兼容后端: Chat Completions 接口(openai SDK, 可选依赖)。

    json_mode=True 时请求 response_format={"type": "json_object"}。
    优先用界面激活的模型配置(api_key/base/model), 否则回退 settings 静态配置。
    """
    from openai import OpenAI  # 延迟导入: openai 为可选依赖

    cfg = _active_store_config()
    if cfg:
        api_key = cfg["api_key"]
        api_base = cfg["api_base"]
        model = cfg["model"]
    else:
        api_key = settings.llm_api_key
        api_base = settings.llm_api_base
        model = settings.llm_model

    client = OpenAI(
        api_key=api_key,
        base_url=api_base,
        timeout=settings.llm_timeout,
    )
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def chat(
    system: str,
    user: str,
    max_tokens: int = 3000,
    temperature: float = 0.2,
) -> str:
    """完成一次对话, 返回纯文本。

    按 LLM_PROVIDER 分派到 Kimi / OpenAI 兼容后端; 失败抛异常由调用方降级。
    """
    if _provider() == "openai":
        return _chat_openai(system, user, max_tokens=max_tokens, temperature=temperature)
    return _chat_kimi(system, user, max_tokens=max_tokens, temperature=temperature)


def _clean_json_text(raw: str) -> str:
    """清洗 LLM 输出中的 markdown 代码块, 尽量提取 JSON 文本。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    if text.startswith("{"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def chat_json(
    system: str,
    user: str,
    max_tokens: int = 3000,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    """完成一次对话并解析为 JSON 对象。

    按 LLM_PROVIDER 分派; 解析失败抛异常由调用方降级。
    """
    if _provider() == "openai":
        raw = _chat_openai(
            system, user, max_tokens=max_tokens, temperature=temperature, json_mode=True
        )
    else:
        raw = chat(system, user, max_tokens=max_tokens, temperature=temperature)
    return json.loads(_clean_json_text(raw))
