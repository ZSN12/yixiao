# -*- coding: utf-8 -*-
"""飞书企业自建应用通知模块(feishu_app_notifier.py)。

支持能力:
1. 凭 App ID / App Secret 自动获取并缓存 tenant_access_token (7200秒有效)。
2. 通过销售手机号批量查询 open_id (contact/v3/users/batch_get_id)。
3. 以应用机器人身份向指定销售发送「单客户智能分配卡片」工作通知 (im/v1/messages, interactive 卡片)。
4. 向指定销售发送「销售线索智能日报卡片」。

设计准则:
- 纯标准库实现 (urllib + json + ssl), 无额外三方依赖;
- SSL 证书容错处理 (兼容自签名/企业内网代理);
- 凭证缺失或推送失败时降级记录日志, 不阻断主流水线。
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Token 内存缓存: {"token": "...", "expires_at": 1234567890}
_token_cache: Dict[str, Any] = {"token": "", "expires_at": 0}

# 用户 open_id 内存缓存: {"mobile": "open_id"}
_user_id_cache: Dict[str, str] = {}


def _get_ssl_context() -> ssl.SSLContext:
    """生成 SSL 上下文: settings.verify_ssl=False 时宽松, 兼容内网代理/自签名证书环境。"""
    ctx = ssl.create_default_context()
    if not getattr(settings, "verify_ssl", False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_tenant_access_token(app_id: Optional[str] = None, app_secret: Optional[str] = None) -> Optional[str]:
    """获取飞书自建应用的 tenant_access_token (带本地过期缓存)。

    Args:
        app_id: 飞书 App ID, 默认取 settings.feishu_app_id。
        app_secret: 飞书 App Secret, 默认取 settings.feishu_app_secret。

    Returns:
        str | None: 成功返回 token 字符串, 失败返回 None。
    """
    global _token_cache
    aid = (app_id or getattr(settings, "feishu_app_id", "") or "").strip()
    asec = (app_secret or getattr(settings, "feishu_app_secret", "") or "").strip()

    if not aid or not asec:
        logger.debug("未配置 feishu_app_id 或 feishu_app_secret, 跳过飞书应用 API 调用")
        return None

    # 检查缓存 (提前 300 秒刷新)
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 300:
        return _token_cache["token"]

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": aid, "app_secret": asec}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    try:
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == 0 and "tenant_access_token" in data:
                token = data["tenant_access_token"]
                expire = int(data.get("expire", 7200))
                _token_cache = {"token": token, "expires_at": now + expire}
                logger.info("成功获取/刷新飞书 tenant_access_token (有效期 %d 秒)", expire)
                return token
            logger.error("获取飞书 token 失败: code=%s, msg=%s", data.get("code"), data.get("msg"))
            return None
    except Exception as exc:
        logger.error("请求飞书 token 接口网络异常: %s", exc)
        return None


def get_open_id_by_mobile(mobile: str, token: Optional[str] = None) -> Optional[str]:
    """通过员工手机号查询对应的飞书 open_id (带缓存)。

    Args:
        mobile: 11位中国大陆手机号。
        token: tenant_access_token, 为空时自动调用 get_tenant_access_token()。

    Returns:
        str | None: 飞书 open_id (如 ou_xxx), 查无此人或失败返回 None。
    """
    mobile = str(mobile).strip()
    if not mobile:
        return None

    if mobile in _user_id_cache:
        return _user_id_cache[mobile]

    tok = token or get_tenant_access_token()
    if not tok:
        return None

    url = "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id"
    payload = json.dumps({"mobiles": [mobile]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {tok}",
        },
    )

    try:
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == 0:
                user_list = data.get("data", {}).get("user_list", [])
                if user_list and user_list[0].get("user_id"):
                    open_id = user_list[0]["user_id"]
                    _user_id_cache[mobile] = open_id
                    return open_id
            logger.warning("通过手机号 %s 查询飞书 open_id 无结果: %s", mobile, data.get("msg"))
            return None
    except Exception as exc:
        logger.error("查询飞书用户 open_id 网络异常 (mobile=%s): %s", mobile, exc)
        return None


def _mobile_entry_url(open_id: Optional[str] = None) -> str:
    """构造易销客户画像入口地址(卡片「进入易销」按钮跳转用)。

    销售点击后直接进入易销「客户画像」工作台, 并自动识别销售身份只呈现分配给他的专属线索。

    Args:
        open_id: 接收人飞书 open_id(传入则在 URL 里带上, 使工作台能自动识别身份)。

    Returns:
        str: 易销客户画像 URL, 形如 https://<公网>/?open_id=ou_xxx#/customers。
    """
    base = getattr(settings, "feishu_webapp_url", "").rstrip("/")
    if not base or "/m" in base:
        base = "https://educational-starts-pearl-node.trycloudflare.com"
    if open_id:
        from urllib.parse import urlencode
        return f"{base}/?{urlencode({'open_id': open_id})}#/customers"
    return f"{base}/#/customers"


def build_aggregated_assignment_card(
    sales_name: str,
    sales_id: str,
    items: List[Dict[str, Any]],
    open_id: str = "",
) -> Dict[str, Any]:
    """构建销售人员今日线索聚合日报大卡片 (解决消息刷屏，单人单卡汇总)。

    Args:
        sales_name: 销售姓名 (如 "张伟")。
        sales_id: 销售工号 (如 "S001")。
        items: 今日分配给该销售的线索列表，每项含 customer_id, customer_name,
               intention_level, churn_risk, match_reason, follow_up_suggestion 等。
        open_id: 接收人 open_id (用于底部跳转进入易销工作台)。

    Returns:
        dict: 飞书卡片 JSON (schema 2.0)。
    """
    total_count = len(items)
    hi_count = sum(1 for x in items if x.get("intention_level") == "高")
    mid_count = sum(1 for x in items if x.get("intention_level") == "中")
    low_risk_count = sum(1 for x in items if x.get("churn_risk") == "低")

    # 1. 顶部统计概述
    summary_text = (
        f"**📊 今日线索总览**\n"
        f"为您智能匹配 **{total_count}** 家新客户线索：\n"
        f"• 🔥 **高意向**: {hi_count} 家（建议 2 小时内优先触达）\n"
        f"• ⚡ **中意向**: {mid_count} 家 | 🛡️ **低风险客户**: {low_risk_count} 家"
    )

    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": summary_text,
            },
        },
        {"tag": "hr"},
    ]

    # 2. 挑选出 Top 3 重点客户展示详细画像 (按意向等级 高 > 中 > 低 排序)
    sorted_items = sorted(
        items,
        key=lambda x: (0 if x.get("intention_level") == "高" else (1 if x.get("intention_level") == "中" else 2)),
    )
    top_items = sorted_items[:3]

    if top_items:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🎯 今日重点跟进客户推荐（Top 3）**",
            },
        })

        for i, c in enumerate(top_items):
            cid = c.get("customer_id", "")
            cname = c.get("customer_name", cid)
            intent = c.get("intention_level", "中")
            intent_badge = "🔴 高意向" if intent == "高" else ("🟡 中意向" if intent == "中" else "🟢 低意向")
            suggestion = c.get("follow_up_suggestion") or c.get("match_reason") or "建议尽快安排初步意向沟通"

            # 客户小卡片
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{i+1}. {cname}** (`{cid}`)\n"
                               f"• 评级: **{intent_badge}**\n"
                               f"• 💡 建议: {suggestion}",
                },
            })

            # 单个接单按钮
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"✅ 立即接单「{cname[:8]}」"},
                        "type": "primary" if intent == "高" else "default",
                        "value": {
                            "action": "accept_lead",
                            "customer_id": cid,
                            "customer_name": cname,
                            "sales_id": sales_id,
                        },
                    },
                ],
            })
            if i < len(top_items) - 1:
                elements.append({"tag": "hr"})

    # 3. 底部大按钮: 直达易销工作台查看全部 N 家客户
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"📱 进入易销 · 查看名下全部 {total_count} 家客户画像"},
                    "type": "primary",
                    "url": _mobile_entry_url(open_id=open_id or None),
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"「易销」智能线索分发中心 · 今日共分发 {total_count} 条线索 · 零刷屏汇总"}
            ],
        },
    ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📌 今日线索分配早报 · {sales_name} ({sales_id})"},
            "template": "blue" if hi_count == 0 else "red",
        },
        "elements": elements,
    }


def send_aggregated_assignment_card(
    receive_id: str,
    sales_name: str,
    sales_id: str,
    items: List[Dict[str, Any]],
    receive_id_type: str = "open_id",
) -> bool:
    """向指定销售个人发送一张【聚合分配日报大卡片】。"""
    token = get_tenant_access_token()
    if not token:
        logger.warning("获取飞书 token 失败, 无法发送聚合工作通知")
        return False

    card = build_aggregated_assignment_card(
        sales_name=sales_name,
        sales_id=sales_id,
        items=items,
        open_id=receive_id if receive_id_type == "open_id" else "",
    )

    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == 0:
                logger.info("已向销售 %s 发送聚合分配卡片 (含 %d 家客户)", sales_name, len(items))
                return True
            else:
                logger.warning("飞书聚合卡片发送失败: code=%s msg=%s", data.get("code"), data.get("msg"))
                return False
    except Exception as exc:
        logger.error("发送飞书聚合卡片网络异常: %s", exc)
        return False


def build_assignment_card(
    customer_id: str,
    customer_name: str,
    intention_level: str,
    churn_risk: str,
    match_reason: str,
    core_demands: Optional[List[str]] = None,
    follow_up_suggestion: str = "",
    sales_name: str = "",
    open_id: str = "",
) -> Dict[str, Any]:
    """构建单客户智能分配交互式卡片 (Feishu Interactive Card)。"""
    core_demands = core_demands or []
    demand_lines = "\n".join([f"{i+1}. {d}" for i, d in enumerate(core_demands[:3])]) if core_demands else "暂无明确痛点记录"

    intent_badge = "🔴 **高意向**" if intention_level == "高" else ("🟡 **中意向**" if intention_level == "中" else "🟢 **低意向**")
    churn_badge = "🔴 高风险" if churn_risk == "高" else ("🟡 中风险" if churn_risk == "中" else "🟢 低风险")
    header_color = "red" if intention_level == "高" else ("orange" if intention_level == "中" else "blue")

    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**客户名称**: {customer_name} (`{customer_id}`)\n"
                           f"**意向等级**: {intent_badge} | **流失风险**: {churn_badge}\n"
                           f"**分配理由**: {match_reason or '系统综合匹配'}",
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**🎯 客户核心诉求**:\n{demand_lines}",
            },
        },
    ]

    if follow_up_suggestion:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**💡 AI 跟进建议**:\n{follow_up_suggestion}",
            },
        })

    # 交互动作按钮栏 (Action Buttons)
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✅ 立即接单跟进"},
                    "type": "primary",
                    "value": {
                        "action": "accept_lead",
                        "customer_id": customer_id,
                        "customer_name": customer_name,
                        "sales_id": getattr(settings, "current_sales_id", "S001"),
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 申请改派/转交"},
                    "type": "default",
                    "value": {
                        "action": "request_reassign",
                        "customer_id": customer_id,
                        "customer_name": customer_name,
                        "intention_level": intention_level,
                        "churn_risk": churn_risk,
                        "match_reason": match_reason,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📝 快速录入小记"},
                    "type": "default",
                    "value": {
                        "action": "log_note",
                        "customer_id": customer_id,
                        "customer_name": customer_name,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "💬 生成破冰话术"},
                    "type": "default",
                    "value": {
                        "action": "gen_talk_track",
                        "customer_id": customer_id,
                        "customer_name": customer_name,
                    },
                },
            ],
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📱 进入易销 · 查看我的全部客户"},
                    "type": "default",
                    "url": _mobile_entry_url(open_id=open_id or None),
                },
            ],
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "来自「易销」销售线索智能分析与分发助手 · 支持飞书卡片直接交互"}
            ],
        },
    ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📌 新线索分配通知 · {customer_name[:12]}"},
            "template": header_color,
        },
        "elements": elements,
    }


def send_personal_assignment_card(
    receive_id: str,
    customer_id: str,
    customer_name: str,
    intention_level: str,
    churn_risk: str,
    match_reason: str,
    core_demands: Optional[List[str]] = None,
    follow_up_suggestion: str = "",
    receive_id_type: str = "open_id",
) -> bool:
    """向指定销售个人发送一条新线索分配卡片工作通知。

    Args:
        receive_id: 接收人 ID (默认 open_id, 如 ou_xxx; 也支持 user_id)。
        customer_id: 客户 ID (如 C001)。
        customer_name: 客户名称。
        intention_level: 意向等级 (高/中/低)。
        churn_risk: 流失风险 (高/中/低)。
        match_reason: 匹配理由。
        core_demands: 客户核心诉求列表。
        follow_up_suggestion: AI 跟进建议。
        receive_id_type: 接收人 ID 类型, "open_id" | "user_id"。

    Returns:
        bool: 发送成功返回 True, 失败返回 False。
    """
    token = get_tenant_access_token()
    if not token:
        logger.warning("获取飞书 token 失败, 无法发送个人工作通知")
        return False

    card = build_assignment_card(
        customer_id=customer_id,
        customer_name=customer_name,
        intention_level=intention_level,
        churn_risk=churn_risk,
        match_reason=match_reason,
        core_demands=core_demands,
        follow_up_suggestion=follow_up_suggestion,
        open_id=receive_id if receive_id_type == "open_id" else "",
    )

    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    payload = json.dumps({
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("code") == 0:
                logger.info("成功向销售 (receive_id=%s) 发送客户 %s 分配卡片", receive_id, customer_id)
                return True
            logger.error("飞书发送个人卡片失败: code=%s, msg=%s", res.get("code"), res.get("msg"))
            return False
    except Exception as exc:
        logger.error("飞书发送个人卡片网络异常: %s", exc)
        return False


def update_card_message(message_id: str, card: Dict[str, Any]) -> bool:
    """主动更新一条飞书交互卡片消息 (PUT /im/v1/messages/{message_id})。

    用于卡片按钮回调后，把原卡片原地刷新为新状态 (如已接单)。

    Args:
        message_id: 目标消息 ID (open_message_id, 如 om_xxx)。
        card: 新的完整交互卡片 dict。

    Returns:
        bool: 更新成功返回 True。
    """
    token = get_tenant_access_token()
    if not token or not message_id:
        logger.warning("更新飞书卡片缺少 token 或 message_id")
        return False

    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
    payload = json.dumps({
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        method="PUT",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("code") == 0:
                logger.info("成功更新飞书卡片消息 %s", message_id)
                return True
            logger.error("更新飞书卡片失败: code=%s, msg=%s", res.get("code"), res.get("msg"))
            return False
    except Exception as exc:
        logger.error("更新飞书卡片网络异常: %s", exc)
        return False


def build_reassign_form_card(customer_id: str, customer_name: str, sales_name: str = "销售") -> Dict[str, Any]:
    """构建「申请改派/转交」表单卡片：内嵌原因输入框 + 提交按钮。

    用户在输入框填写转交原因后点击「提交改派申请」，回调会携带
    action.value["reassign_reason"] = 用户填写的内容。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔄 申请改派 · {customer_name[:12]}"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**客户**: {customer_name} (`{customer_id}`)\n"
                               f"**申请人**: {sales_name}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**请填写改派/转交原因**（如客户需求与个人能力不匹配、近期负荷过重等）：",
                },
            },
            {
                "tag": "input",
                "name": "reassign_reason",
                "placeholder": "请在此输入改派原因…",
                "multiline": True,
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📨 提交改派申请"},
                        "type": "primary",
                        "value": {
                            "action": "submit_reassign",
                            "customer_id": customer_id,
                            "customer_name": customer_name,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "↩️ 返回原卡片"},
                        "type": "default",
                        "value": {
                            "action": "cancel_reassign",
                            "customer_id": customer_id,
                            "customer_name": customer_name,
                        },
                    },
                ],
            },
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "提交后主管将收到复核提醒"}]},
        ],
    }


def build_reassign_submitted_card(customer_id: str, customer_name: str, reason: str = "", operator_name: str = "销售") -> Dict[str, Any]:
    """构建「改派申请已提交」状态的飞书交互卡片（提交后原地刷新用）。"""
    from datetime import datetime
    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    reason_text = reason.strip() or "（未填写原因）"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔄 改派申请已提交 · {customer_name[:12]}"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**客户名称**: {customer_name} (`{customer_id}`)\n"
                               f"**申请人**: {operator_name}\n"
                               f"**改派原因**: {reason_text}\n"
                               f"**提交时间**: {submitted_at}\n"
                               f"**状态**: ⏳ 待主管复核分配",
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "「易销」智能分发助手 · 改派申请已通知主管"}],
            },
        ],
    }


def build_accepted_card(customer_id: str, customer_name: str, operator_name: str = "销售") -> Dict[str, Any]:
    """构建「已接单」状态的飞书交互卡片 (接单回调后刷新用)。"""
    from datetime import datetime
    accepted_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ 已接单跟进 · {customer_name[:12]}"},
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**客户名称**: {customer_name} (`{customer_id}`)\n"
                               f"**当前状态**: 🟢 **{operator_name} 已接单跟进中**\n"
                               f"**接单时间**: {accepted_at}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "「易销」智能分发助手 · 接单记录已同步至后台"}
                ],
            },
        ],
    }


def build_note_form_card(customer_id: str, customer_name: str) -> Dict[str, Any]:
    """构建「录入跟进小记」输入表单卡片：内嵌多行输入框 + 提交按钮。

    销售在输入框填写跟进内容(如"今天和李总聊了, 下月有 50 万预算, 但担心
    交付周期")后点击「提交小记并重估意向」，回调会携带
    action.value["note_text"] = 用户填写的内容。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📝 录入跟进小记 · {customer_name[:12]}"},
            "template": "wathet",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**客户**: {customer_name} (`{customer_id}`)\n"
                               f"请描述本次跟进的关键信息，AI 将据此**动态重估意向等级**并刷新跟进策略。",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "💡 **建议包含**: 预算、决策人、时间节点、竞品情况、客户顾虑等关键词",
                },
            },
            {
                "tag": "input",
                "name": "note_text",
                "placeholder": "例如：今天和李总聊了，他们下个月有50万预算，但担心交付周期…",
                "multiline": True,
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🤖 提交小记并重估意向"},
                        "type": "primary",
                        "value": {
                            "action": "submit_note",
                            "customer_id": customer_id,
                            "customer_name": customer_name,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "↩️ 返回原卡片"},
                        "type": "default",
                        "value": {
                            "action": "cancel_reassign",
                            "customer_id": customer_id,
                            "customer_name": customer_name,
                        },
                    },
                ],
            },
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "提交后 AI 将自动重估意向等级并同步多维表格"}]},
        ],
    }


def build_note_analyzed_card(customer_id: str, customer_name: str, note_text: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """构建「跟进小记已分析」结果卡片(展示意向等级变化 + 新跟进策略)。"""
    intention_before = result.get("intention_before", "中")
    intention_after = result.get("intention_after", "中")
    churn_before = result.get("churn_before", "低")
    churn_after = result.get("churn_after", "低")
    change = result.get("intention_change", "unchanged")

    change_label = {
        "upgrade": "🔺 意向升级",
        "downgrade": "🔻 意向下调",
        "unchanged": "➡️ 意向持平",
    }.get(change, "➡️ 意向持平")

    if change == "upgrade":
        template = "red"
    elif change == "downgrade":
        template = "grey"
    else:
        template = "green"

    suggestion = result.get("new_follow_up_suggestion", "")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{change_label} · {customer_name[:12]}"},
            "template": template,
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**客户**: {customer_name} (`{customer_id}`)\n"
                               f"**意向等级**: {intention_before} → **{intention_after}**  {change_label}\n"
                               f"**流失风险**: {churn_before} → {churn_after}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📝 跟进小记**\n{note_text[:200]}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**💡 AI 更新跟进策略**\n{suggestion}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "意向重估结果已同步至易销后台与飞书多维表格"}],
            },
        ],
    }


def build_talk_track_card(
    customer_id: str,
    customer_name: str,
    track_type: str,
    content: str,
    engine: str = "llm",
) -> Dict[str, Any]:
    """构建「话术生成结果」卡片：展示微信破冰 / 电话话术 + 一键复制。

    飞书卡片按钮 value 支持 "copy" 动作前端 JS 无法直接系统剪贴板,
    故采用「话术全文展示 + 按钮返回/换类型」; 复制由销售长按文本选择复制。
    同时提供切换按钮(微信 <-> 电话)重新生成另一种话术。
    """
    type_label = "💬 微信破冰草稿" if track_type == "wechat" else "📞 首通电话话术"
    other_type = "phone" if track_type == "wechat" else "wechat"
    other_label = "📞 生成电话话术" if track_type == "wechat" else "💬 生成微信破冰"
    engine_badge = "🤖 Kimi K2.7 Code 生成" if engine == "llm" else "📋 规则模板生成"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{type_label} · {customer_name[:12]}"},
            "template": "purple",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**客户**: {customer_name} (`{customer_id}`)\n**生成引擎**: {engine_badge}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{type_label}**（长按复制）\n\n{content}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": other_label},
                        "type": "primary",
                        "value": {
                            "action": "gen_talk_track",
                            "customer_id": customer_id,
                            "customer_name": customer_name,
                            "track_type": other_type,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📝 录入跟进小记"},
                        "type": "default",
                        "value": {
                            "action": "log_note",
                            "customer_id": customer_id,
                            "customer_name": customer_name,
                        },
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "长按上方话术文本即可复制到剪贴板，直接粘贴发微信"}],
            },
        ],
    }


def send_sla_alert_card(
    receive_id: str,
    customer_name: str,
    sla_status: str,
    elapsed_hours: float,
    overdue_hours: int = 72,
    receive_id_type: str = "open_id",
) -> bool:
    """向销售/主管发送 SLA 超时预警卡片(提醒尽快跟进 / 已流转公海)。

    Args:
        receive_id: 接收人 ID(open_id 或 user_id)。
        customer_name: 客户名称。
        sla_status: "warning"(预警) | "overdue"(超时流转)。
        elapsed_hours: 接单后已过小时数。
        overdue_hours: 超时阈值(小时)。
        receive_id_type: "open_id" | "user_id"。

    Returns:
        bool: 发送成功 True。
    """
    token = get_tenant_access_token()
    if not token:
        logger.warning("获取飞书 token 失败, 无法发送 SLA 预警卡片")
        return False

    if sla_status == "overdue":
        title = f"🚨 SLA 超时 · {customer_name[:12]} 已流转公海"
        template = "red"
        body = (
            f"**客户**: {customer_name}\n"
            f"**状态**: 🔴 接单后 {elapsed_hours:.0f} 小时未跟进, 已超过 {overdue_hours}h SLA 时限\n"
            f"**处理**: 该线索已自动释放归属、流转回公海, 将重新进入待分配池。\n"
            f"请知悉并关注线索重新分配。"
        )
    else:
        title = f"⏰ SLA 预警 · {customer_name[:12]} 请尽快跟进"
        template = "orange"
        body = (
            f"**客户**: {customer_name}\n"
            f"**状态**: 🟡 接单后 {elapsed_hours:.0f} 小时尚未跟进\n"
            f"**提醒**: 若在 {overdue_hours}h 内仍未跟进, 该线索将自动流转回公海。\n"
            f"请尽快完成首次跟进并录入小记。"
        )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "「易销」SLA 超时预警 · 自动流转公海"}]},
        ],
    }

    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    payload = json.dumps({
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urllib.request.urlopen(req, context=_get_ssl_context(), timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("code") == 0:
                logger.info("成功发送 SLA 预警卡片: %s -> %s", customer_name, receive_id)
                return True
            logger.error("飞书 SLA 预警卡片失败: code=%s, msg=%s", res.get("code"), res.get("msg"))
            return False
    except Exception as exc:
        logger.error("飞书 SLA 预警卡片网络异常: %s", exc)
        return False
