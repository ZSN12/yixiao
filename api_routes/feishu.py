# -*- coding: utf-8 -*-
"""飞书卡片按钮交互回调路由 (Card Action Handler)。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request

from modules import data_loader, follow_up_notes, talk_track

from .common import _feishu_card_response, _find_sales_by_open_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feishu"])


@router.post("/feishu/card-action")
async def feishu_card_action(request: Request) -> Dict[str, Any]:
    """接收并处理飞书卡片按钮点击回调事件 (Card Action Handler)。

    支持动作:
    1. accept_lead: 销售点击「✅ 立即接单跟进」 -> 更新客户归属, 实时更新卡片提示已接单;
    2. request_reassign: 销售点击「🔄 申请改派/转交」 -> 标记待人工处理/改派;
    3. log_note: 销售点击「📝 快速录入小记」 -> 记录跟进状态。
    """
    try:
        body = await request.json()
        logger.info("收到飞书卡片交互回调: %s", json.dumps(body, ensure_ascii=False))

        # 飞书 URL 校验 Challenge (配置 Webhook 请求网址时使用)
        if "challenge" in body:
            return {"challenge": body["challenge"]}

        # 飞书卡片回调 schema 2.0: 按钮点击信息在 event.action.value 下;
        # 兼容旧版回调(顶层 action)。两种都取, 以实际存在者为准。
        event_obj = body.get("event", {}) or {}
        action_data = body.get("action", {}) or event_obj.get("action", {}) or {}
        value = action_data.get("value", {}) or {}
        # 输入框(input)的值也会通过 value[输入框name] 一并带过来
        action_type = value.get("action", "")
        customer_id = value.get("customer_id", "")
        customer_name = value.get("customer_name", customer_id)
        operator_open_id = event_obj.get("operator", {}).get("open_id", "") or body.get("open_id", "")

        # 1. 接单动作
        if action_type == "accept_lead":
            # 用回调的 operator_open_id 反查当前销售 ID, 写为客户的真正归属
            operator_sales = _find_sales_by_open_id(operator_open_id) or {}
            owner_sid = operator_sales.get("sales_id") or ""
            # 将客户归属写入/更新
            try:
                customers = data_loader.load_customers()
                matched_cust = next((c for c in customers if c.customer_id == customer_id), None)
                if matched_cust:
                    # 归属写入实际销售 ID(优先按 open_id 反查; 查不到则保留原归属,
                    # 但不再写入状态文本, 避免污染 owner_sales_id)
                    if owner_sid:
                        matched_cust.owner_sales_id = owner_sid
                    elif matched_cust.owner_sales_id in ("已接单", "待改派"):
                        matched_cust.owner_sales_id = None
                    data_loader.save_customers(customers)
            except Exception as e:
                logger.warning("更新客户接单状态异常: %s", e)

            from modules import feishu_app_notifier
            accepted_card = feishu_app_notifier.build_accepted_card(customer_id, customer_name)

            # 卡片回调更新: 通过响应返回 card 让飞书原地替换当前卡片
            # (注: PUT /im/v1/messages 更新接口对卡片消息不支持, 已弃用)
            return _feishu_card_response(
                card=accepted_card,
                toast={"type": "success", "content": f"🎉 您已成功接单「{customer_name}」，请尽快跟进！"},
            )

        # 2. 申请改派 —— 第一步：把原卡片原地更新为「填写原因」表单卡片
        elif action_type == "request_reassign":
            from modules import feishu_app_notifier
            form_card = feishu_app_notifier.build_reassign_form_card(customer_id, customer_name)
            return _feishu_card_response(
                card=form_card,
                toast={"type": "info", "content": "请在上方输入框填写改派原因，然后点击「提交改派申请」"},
            )

        # 2b. 提交改派申请 —— 第二步：读取输入框原因，记录改派并原地更新卡片
        elif action_type == "submit_reassign":
            reason = value.get("reassign_reason", "") or ""
            logger.info("收到改派申请: customer=%s reason=%s operator=%s", customer_id, reason, operator_open_id)
            # 标记客户为待改派: 清除归属(待主管复核后重新分配), 不写入状态文本
            try:
                customers = data_loader.load_customers()
                matched_cust = next((c for c in customers if c.customer_id == customer_id), None)
                if matched_cust:
                    matched_cust.owner_sales_id = None
                    data_loader.save_customers(customers)
            except Exception as e:
                logger.warning("更新客户改派状态异常: %s", e)

            from modules import feishu_app_notifier
            submitted_card = feishu_app_notifier.build_reassign_submitted_card(
                customer_id, customer_name, reason=reason,
            )
            return _feishu_card_response(
                card=submitted_card,
                toast={"type": "success", "content": f"已提交「{customer_name}」的改派申请，主管将收到复核提醒"},
            )

        # 2c. 返回原卡片(取消填写)
        elif action_type == "cancel_reassign":
            from modules import feishu_app_notifier
            back_card = feishu_app_notifier.build_assignment_card(
                customer_id=customer_id,
                customer_name=customer_name,
                intention_level=value.get("intention_level", "中"),
                churn_risk=value.get("churn_risk", "中"),
                match_reason=value.get("match_reason", ""),
            )
            return _feishu_card_response(card=back_card, toast={"type": "info", "content": "已取消改派"})

        # 3. 录入小记 —— 第一步: 把原卡片原地更新为「录入小记」表单卡片
        elif action_type == "log_note":
            from modules import feishu_app_notifier
            note_form_card = feishu_app_notifier.build_note_form_card(customer_id, customer_name)
            return _feishu_card_response(
                card=note_form_card,
                toast={"type": "info", "content": "请在上方输入框填写本次跟进小记，提交后 AI 将重估意向"},
            )

        # 3b. 提交跟进小记 —— 第二步: 读取小记文本, AI 重估意向, 刷新卡片
        elif action_type == "submit_note":
            note_text = value.get("note_text", "") or ""
            logger.info("收到跟进小记: customer=%s note=%s operator=%s", customer_id, note_text[:50], operator_open_id)

            # 反查销售 ID(用于小记归属)
            operator_sales = _find_sales_by_open_id(operator_open_id) or {}
            sales_id = operator_sales.get("sales_id") or ""

            if not note_text.strip():
                return _feishu_card_response(
                    toast={"type": "error", "content": "跟进小记内容为空，请先填写再提交"},
                )

            # AI 动态重估意向等级 + 流失风险 + 跟进策略
            reanalysis = follow_up_notes.reanalyze_with_note(
                customer_id=customer_id,
                customer_name=customer_name,
                note_text=note_text,
                sales_id=sales_id,
                persist=True,
            )

            from modules import feishu_app_notifier
            analyzed_card = feishu_app_notifier.build_note_analyzed_card(
                customer_id, customer_name, note_text, reanalysis,
            )

            # 变化提示文案
            change = reanalysis.get("intention_change", "unchanged")
            if change == "upgrade":
                toast_msg = f"🎉 意向升级: {reanalysis['intention_before']} → {reanalysis['intention_after']}，请优先跟进！"
            elif change == "downgrade":
                toast_msg = f"📉 意向下调: {reanalysis['intention_before']} → {reanalysis['intention_after']}"
            else:
                toast_msg = f"✅ 小记已记录，意向保持 {reanalysis['intention_after']}"

            return _feishu_card_response(card=analyzed_card, toast={"type": "success", "content": toast_msg})

        # 4. 生成破冰话术 —— 调 Kimi K2.7 Code 生成微信/电话话术
        elif action_type == "gen_talk_track":
            track_type = value.get("track_type", "wechat") or "wechat"
            if track_type not in ("wechat", "phone"):
                track_type = "wechat"

            # 反查销售姓名(用于话术署名)
            operator_sales = _find_sales_by_open_id(operator_open_id) or {}
            sales_name = operator_sales.get("name") or operator_sales.get("sales_id") or ""

            from modules import feishu_app_notifier

            result = talk_track.generate_talk_track(customer_id, track_type, sales_name)
            talk_card = feishu_app_notifier.build_talk_track_card(
                customer_id, customer_name, track_type, result["content"], result["engine"],
            )

            if result["engine"] == "llm":
                toast_msg = f"🤖 已用 Kimi K2.7 Code 生成{'微信破冰' if track_type == 'wechat' else '电话话术'}，长按复制即可发送"
            else:
                toast_msg = f"已生成{'微信破冰' if track_type == 'wechat' else '电话话术'}（规则模板）"

            return _feishu_card_response(card=talk_card, toast={"type": "success", "content": toast_msg})

        return _feishu_card_response(toast={"type": "info", "content": "操作已记录"})
    except Exception as exc:
        logger.error("处理飞书卡片交互异常: %s", exc)
        return _feishu_card_response(toast={"type": "error", "content": f"处理失败: {exc}"})
