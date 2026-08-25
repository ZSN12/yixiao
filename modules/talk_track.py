# -*- coding: utf-8 -*-
"""AI 辅助话术生成模块: 一键生成「微信破冰草稿」+「首通电话话术」。

业务价值:
    销售拿到画像里的「💡 AI 跟进建议」后, 往往还要自己绞尽脑汁写微信开场白
    或电话话术。本模块根据客户画像(行业/城市/规模/核心诉求/意向等级/跟进
    建议/跟进小记), 一键生成针对该客户的定制化沟通话术, 销售点击即可复制。

双引擎(与 profile_analyzer 同构):
    - 规则模板引擎(mock_mode 或无 LLM key): 基于客户画像字段 + 意向等级 +
      核心诉求, 确定性拼装话术模板 —— 开箱即跑、可复现;
    - LLM 引擎(配置 llm_api_key 且非 mock_mode): 调用大模型语义生成, 更自然。

话术类型:
    - wechat: 微信破冰开场白(适合发微信/企业微信的简短消息);
    - phone: 首通电话话术(结构化: 开场/需求确认/价值呈现/下一步行动)。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from modules import data_loader

logger = logging.getLogger(__name__)


# ============================================================
# 规则模板引擎(确定性, mock_mode 开箱即跑)
# ============================================================

# 行业 -> 痛点切入话术(破冰引用)
_INDUSTRY_PAIN_HOOK: Dict[str, str] = {
    "智能制造": "工厂生产效率与质检良率",
    "医疗器械": "产品合规与供应链可追溯",
    "软件服务": "研发交付效率与客户续约",
    "物流": "运力调度与仓储周转成本",
    "新能源": "产能扩张与充电网络运营",
    "医药制造": "GMP 合规与供应链数字化",
    "默认": "降本增效与业务流程优化",
}


def _latest_profile(customer_id: str) -> Dict[str, Any]:
    """读取客户最新画像结果 + 最新跟进小记。"""
    profile: Dict[str, Any] = {}
    session = None
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is not None:
            from modules.data_loader import AnalysisHistory
            rows = (
                session.query(AnalysisHistory)
                .filter(AnalysisHistory.customer_id == customer_id)
                .order_by(AnalysisHistory.created_at.desc())
                .limit(1)
                .all()
            )
            if rows:
                try:
                    profile = json.loads(rows[0].result_json)
                except (json.JSONDecodeError, TypeError):
                    profile = {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取画像失败(%s): %s", customer_id, exc)
    finally:
        if session is not None:
            session.close()

    # 追加最新跟进小记(若有)
    try:
        from modules import follow_up_notes
        notes = follow_up_notes.list_notes(customer_id=customer_id, limit=1)
        if notes:
            profile["_latest_note"] = notes[0].get("note_text", "")
    except Exception:  # noqa: BLE001
        pass
    return profile


def _customer_brief(customer_id: str) -> Dict[str, Any]:
    """组装话术生成所需的客户简报。"""
    customers = data_loader.load_customers()
    customer = next((c for c in customers if c.customer_id == customer_id), None)
    if customer is None:
        return {"customer_name": customer_id, "industry": "默认", "city": "", "scale": "中型"}
    profile = _latest_profile(customer_id)
    return {
        "customer_id": customer_id,
        "customer_name": customer.customer_name,
        "industry": customer.industry or "默认",
        "city": customer.city or "",
        "scale": customer.scale or "中型",
        "intention_level": profile.get("intention_level", "中"),
        "core_demands": profile.get("core_demands", []) or [],
        "follow_up_suggestion": profile.get("follow_up_suggestion", ""),
        "latest_note": profile.get("_latest_note", ""),
    }


def _build_wechat_rules(brief: Dict[str, Any], sales_name: str = "") -> str:
    """规则引擎: 生成微信破冰开场白。"""
    name = brief["customer_name"]
    industry = brief["industry"]
    hook = _INDUSTRY_PAIN_HOOK.get(industry, _INDUSTRY_PAIN_HOOK["默认"])
    intent = brief["intention_level"]
    sales_name = sales_name or "小易"

    # 开场问候(带决策人通用称呼)
    opening = f"您好，我是「易销」的 {sales_name}，负责为贵司提供智能化解决方案。"

    # 痛点切入(行业相关)
    pain_hook = f"了解到贵司（{name}）在{industry}领域深耕，我们特别关注到企业在{hook}方面常见的挑战。"

    # 价值点(结合核心诉求)
    demands = brief["core_demands"]
    if demands:
        value = f"针对贵司当前关注的「{demands[0][:20]}」，我们有一套成熟方案可快速落地。"
    else:
        value = "我们有一套成熟方案，可帮助贵司在关键环节提质增效。"

    # 意向分级的话术收尾
    if intent == "高":
        close = "方便约个 15 分钟简短沟通，我们结合贵司实际场景给您一个初步评估吗？"
    elif intent == "中":
        close = "我整理了一份同行实践案例，方便发给您先参考一下吗？"
    else:
        close = "后续有相关需求时，也欢迎随时联系我，我会持续分享行业干货。"

    return f"{opening}\n\n{pain_hook}\n\n{value}\n\n{close}"


def _build_phone_rules(brief: Dict[str, Any], sales_name: str = "") -> str:
    """规则引擎: 生成首通电话话术(结构化四段式)。"""
    name = brief["customer_name"]
    industry = brief["industry"]
    intent = brief["intention_level"]
    sales_name = sales_name or "小易"

    # 1. 开场(10 秒建立身份与来意)
    opening = f"您好，请问是 {name} 的相关负责人吗？我是「易销」的 {sales_name}，冒昧来电，占用您 2 分钟。"

    # 2. 需求确认(结合核心诉求)
    demands = brief["core_demands"]
    if demands:
        need_check = f"了解到贵司在{industry}领域，最近比较关注「{demands[0][:16]}」这块，想跟您确认下目前是不是有相关的规划？"
    else:
        need_check = f"了解到贵司在{industry}领域发展很快，想了解下目前在业务流程数字化方面有没有一些新的想法？"

    # 3. 价值呈现(结合意向)
    if intent == "高":
        value = "我们正好服务过不少同行业客户，最快 3 天就能出个针对性方案。"
    else:
        value = "我们可以先安排一次免费的需求诊断，帮您理清现状和优先级。"

    # 4. 下一步行动
    action = "您看这周四或周五，哪个时间段方便，我安排个 20 分钟的线上沟通？"

    return (
        f"【开场】{opening}\n\n"
        f"【需求确认】{need_check}\n\n"
        f"【价值呈现】{value}\n\n"
        f"【下一步】{action}"
    )


# ============================================================
# LLM 引擎(Kimi K2.7 Code, Anthropic Messages 接口)
# ============================================================

def _llm_enabled() -> bool:
    """是否启用话术生成(当前 provider 已配置 key 即启用, 独立于 mock_mode)。"""
    from modules import llm_client
    return llm_client.enabled()


def _generate_with_llm(brief: Dict[str, Any], track_type: str, sales_name: str) -> str:
    """LLM 引擎: 调 Kimi K2.7 Code 生成话术(复用统一 kimi_client)。

    失败则抛异常, 由上层降级规则模板。
    """
    from modules import llm_client

    type_label = (
        "微信破冰开场白(简短、自然、有温度、可一键复制发送)"
        if track_type == "wechat"
        else "首通电话话术(结构化: 开场/需求确认/价值呈现/下一步)"
    )
    system_prompt = "你是资深的 B2B 销售话术专家, 善于根据客户画像写出专业真诚、不油腻的中文沟通话术。"
    prompt = (
        f"请为以下客户生成一段定制化的{type_label}。\n\n"
        f"【客户简报】\n"
        f"- 企业名称: {brief['customer_name']}\n"
        f"- 行业: {brief['industry']}\n"
        f"- 城市: {brief['city']}\n"
        f"- 规模: {brief['scale']}\n"
        f"- 意向等级: {brief['intention_level']}\n"
        f"- 核心诉求: {'、'.join(brief['core_demands']) if brief['core_demands'] else '暂无'}\n"
        f"- 跟进建议: {brief['follow_up_suggestion']}\n"
        f"- 最近跟进小记: {brief['latest_note'] or '无'}\n"
        f"- 销售署名: {sales_name or '小易'}\n\n"
        f"要求: 1) 紧扣客户痛点与意向等级; 2) 语气专业真诚、不油腻; "
        f"3) 中文输出; 4) 直接输出话术正文, 不要任何解释说明或前缀标题。"
    )

    return llm_client.chat(system_prompt, prompt, max_tokens=2000, temperature=0.7)


# ============================================================
# 对外统一入口
# ============================================================

def generate_talk_track(
    customer_id: str,
    track_type: str = "wechat",
    sales_name: str = "",
) -> Dict[str, Any]:
    """生成客户跟进话术(微信破冰 / 电话话术)。

    Args:
        customer_id: 客户 ID。
        track_type: 话术类型 "wechat"(微信破冰) | "phone"(首通电话)。
        sales_name: 销售署名(可选, 缺省用通用称呼)。

    Returns:
        dict: {customer_id, customer_name, track_type, content, engine}。
            engine 为 "llm" | "rules", 标识实际生成引擎。
    """
    if track_type not in ("wechat", "phone"):
        track_type = "wechat"

    brief = _customer_brief(customer_id)

    # LLM 优先, 失败/未启用降级规则模板
    if _llm_enabled():
        try:
            content = _generate_with_llm(brief, track_type, sales_name)
            if content:
                return {
                    "customer_id": customer_id,
                    "customer_name": brief["customer_name"],
                    "track_type": track_type,
                    "content": content,
                    "engine": "llm",
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 话术生成失败, 降级规则模板: %s", exc)

    if track_type == "wechat":
        content = _build_wechat_rules(brief, sales_name)
    else:
        content = _build_phone_rules(brief, sales_name)

    return {
        "customer_id": customer_id,
        "customer_name": brief["customer_name"],
        "track_type": track_type,
        "content": content,
        "engine": "rules",
    }


def generate_both(customer_id: str, sales_name: str = "") -> Dict[str, Any]:
    """一次性生成微信破冰 + 电话话术两种话术。"""
    wechat = generate_talk_track(customer_id, "wechat", sales_name)
    phone = generate_talk_track(customer_id, "phone", sales_name)
    return {
        "customer_id": customer_id,
        "customer_name": wechat["customer_name"],
        "wechat": wechat,
        "phone": phone,
    }
