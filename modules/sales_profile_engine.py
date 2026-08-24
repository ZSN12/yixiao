# -*- coding: utf-8 -*-
"""销售画像 AI 生成与分析引擎 (sales_profile_engine.py)。

核心能力:
1. 抓取/读取销售人员在 CRM 中的全部历史成单、流失与跟进商机 (fetch_crm_deals)。
2. 通过大模型 (LLM) 深度分析商机特征，提炼销售的:
   - 擅长行业 (根据成交概率与成单金额客观打标)
   - 核心优势与战术标签 (如: 擅长POC快速验证、擅长标杆参访攻坚、擅长合规招投标)
   - 擅长客单价区间与决策人攻坚风格
   - 综合能力雷达维度 (行业匹配度、大单攻坚力、决策人破冰力、成单周期效率)
   - AI 能力总结摘要
3. 双引擎支持: LLM 深度模式 (OpenAI 协议兼容) + 智能规则兜底模式 (零 API 成本)。
4. 自动反哺: 分析出的擅长行业与标签自动同步回销售人员模型并落库。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from modules import data_loader

logger = logging.getLogger(__name__)

# LLM Prompt
_SALES_PROFILE_SYSTEM_PROMPT = """你是一名资深销售效能与胜任力分析专家。
你会收到某位销售在 CRM 系统中的全部历史商机与成单记录（包含客户名称、行业、规模、预算、成交结果、痛点及战术复盘）。

请严格按照以下 JSON 格式输出该销售的能力画像（只输出纯 JSON，禁止任何额外解释或 Markdown 包裹）：
{
  "sales_id": "销售工号",
  "recommended_industries": ["根据成单率与成交金额客观提炼的擅长行业，按匹配度排序，1-3个"],
  "tactical_tags": ["战术优势标签，如'方案POC专家'、'标杆参访攻坚'、'合规白皮书制胜'等，3-5个短标签"],
  "deal_stats": {
    "total_deals": 总商机数,
    "won_deals": 成功成单数,
    "lost_deals": 流失数,
    "win_rate_percent": 成单胜率百分比整数
  },
  "preferred_ticket_size": "擅长客单价区间 (如: 100万-300万)",
  "decision_maker_affinity": "擅长攻坚的决策角色 (如: 技术总监/CTO/采购总监)",
  "ai_summary": "一段100字左右的高管级销售画像总结，概括其打法特点、优势场景与适合分配的线索类型",
  "radar_scores": {
    "industry_depth": 行业专业度评分(60-95整数),
    "enterprise_closing": 大单攻关力(60-95整数),
    "decision_maker_breakthrough": 关键决策人破冰力(60-95整数),
    "cycle_efficiency": 成单周期与推进效率(60-95整数)
  }
}
"""


def _llm_enabled() -> bool:
    """是否启用了真实 LLM(当前 provider 已配置 key 即启用)。"""
    from modules import llm_client
    return llm_client.enabled()


def _llm_chat(system: str, user: str) -> str:
    """调用统一 LLM 网关(按 LLM_PROVIDER 分派 Kimi / OpenAI 兼容)。"""
    from modules import llm_client
    return llm_client.chat(system, user, max_tokens=2000, temperature=0.2)


def _rule_based_profile(sales_id: str, deals: List[dict], sales_info: Optional[dict] = None) -> Dict[str, Any]:
    """规则引擎生成销售画像 (当未配置 LLM 或 LLM 失败时的兜底算法)。"""
    sales_name = (sales_info.get("name") if sales_info else "") or sales_id
    total = len(deals)
    won = len([d for d in deals if d.get("result") in ("成单", "赢单")])
    lost = len([d for d in deals if d.get("result") in ("流失", "输单")])
    win_rate = int(round((won / total * 100))) if total > 0 else 70

    # 行业聚合统计 (成单加权 3 分，跟进加权 1 分)
    ind_scores: Counter = Counter()
    for d in deals:
        ind = d.get("industry", "").strip()
        if not ind:
            continue
        if d.get("result") in ("成单", "赢单"):
            ind_scores[ind] += 3
        else:
            ind_scores[ind] += 1

    # 若无商机记录，取原有静态擅长行业
    if not ind_scores and sales_info:
        for ind in sales_info.get("good_at_industries", []):
            ind_scores[ind] += 2

    recommended_industries = [ind for ind, _ in ind_scores.most_common(3)] or ["智能制造", "软件服务"]

    # 战术标签提取
    tactical_tags = []
    tactics_text = " ".join([d.get("tactic_summary", "") + " " + " ".join(d.get("pain_points", [])) for d in deals])
    if re.search(r"POC|验证|体验|看板", tactics_text):
        tactical_tags.append("敏捷POC验证")
    if re.search(r"参访|标杆|案例|工厂", tactics_text):
        tactical_tags.append("标杆参访攻坚")
    if re.search(r"白皮书|合规|资质|专家|审计", tactics_text):
        tactical_tags.append("合规方案制胜")
    if re.search(r"ROI|测算|收益|省", tactics_text):
        tactical_tags.append("精准ROI模型")
    if re.search(r"高可用|架构|定制|自动化", tactics_text):
        tactical_tags.append("技术深度协同")
    if not tactical_tags:
        tactical_tags = ["方案型销售", "商务关系推进", "需求精准对齐"]

    # 决策人偏好
    dms = [d.get("decision_maker", "") for d in deals if d.get("decision_maker")]
    dm_counter = Counter(dms)
    preferred_dm = dm_counter.most_common(1)[0][0] if dm_counter else "技术总监/采购负责人"

    # 客单价
    amounts = [d.get("budget", "") for d in deals if d.get("budget")]
    preferred_ticket = amounts[0] if amounts else "100万-300万"

    # 概要总结
    summary_text = (
        f"{sales_name}（{sales_id}）历史主攻 {('、'.join(recommended_industries[:2]))} 领域，"
        f"CRM 累计沉淀 {total} 笔商机，成单胜率约 {win_rate}%。擅长通过"
        f"{('与'.join(tactical_tags[:2]))}突破关键决策人（{preferred_dm}），"
        f"在 {preferred_ticket} 客单价区间具备较强打赢能力。"
    )

    # 雷达分
    radar = {
        "industry_depth": min(95, 75 + len(recommended_industries) * 5),
        "enterprise_closing": min(95, 65 + won * 8),
        "decision_maker_breakthrough": min(95, 70 + (10 if "技术" in preferred_dm or "总监" in preferred_dm else 5)),
        "cycle_efficiency": min(95, 60 + win_rate // 3),
    }

    return {
        "sales_id": sales_id,
        "recommended_industries": recommended_industries,
        "tactical_tags": tactical_tags[:4],
        "deal_stats": {
            "total_deals": total,
            "won_deals": won,
            "lost_deals": lost,
            "win_rate_percent": win_rate,
        },
        "preferred_ticket_size": preferred_ticket,
        "decision_maker_affinity": preferred_dm,
        "ai_summary": summary_text,
        "radar_scores": radar,
    }


def analyze_sales_profile(sales_id: str, auto_sync_to_model: bool = True) -> Dict[str, Any]:
    """对单名销售人员执行全量 CRM 商机扫描与 AI 画像提炼。

    Args:
        sales_id: 销售人员工号 (如 S001)。
        auto_sync_to_model: 是否将 AI 提炼的擅长行业自动反哺回 sales_list 并落库。

    Returns:
        dict: 结构化销售能力画像。
    """
    sales_list = data_loader.load_sales()
    sales_obj = next((s for s in sales_list if s.sales_id == sales_id), None)
    sales_dict = sales_obj.model_dump() if sales_obj else {"sales_id": sales_id, "name": sales_id}

    # 1. 抓取该销售在 CRM 的全部商机
    deals = data_loader.load_crm_deals(sales_id=sales_id)

    profile_result = None

    # 2. 尝试 LLM 分析
    if _llm_enabled():
        try:
            user_content = json.dumps({
                "sales_info": sales_dict,
                "crm_deals": deals,
            }, ensure_ascii=False, indent=2)
            raw_res = _llm_chat(
                _SALES_PROFILE_SYSTEM_PROMPT,
                f"请分析以下销售人员在 CRM 中的历史商机数据并生成画像:\n{user_content}",
            )
            # 清洗 markdown 代码块
            clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_res.strip(), flags=re.DOTALL)
            parsed = json.loads(clean_json)
            if isinstance(parsed, dict) and "recommended_industries" in parsed:
                profile_result = parsed
                logger.info("销售 %s AI 画像 LLM 分析成功", sales_id)
        except Exception as exc:
            logger.warning("销售 %s AI 画像 LLM 分析失败(%s), 降级为规则引擎分析", sales_id, exc)

    # 3. 规则兜底
    if not profile_result:
        profile_result = _rule_based_profile(sales_id, deals, sales_dict)

    # 4. 自动反哺同步回 Sales 模型 (更新擅长行业与能力标签)
    if auto_sync_to_model and sales_obj and profile_result.get("recommended_industries"):
        try:
            new_industries = profile_result["recommended_industries"]
            sales_obj.good_at_industries = new_industries
            data_loader.save_sales(sales_list)
            logger.info("已自动反哺销售 %s 的擅长行业为: %s", sales_id, new_industries)
        except Exception as exc:
            logger.warning("反哺销售模型失败: %s", exc)

    return profile_result


def analyze_all_sales() -> Dict[str, Dict[str, Any]]:
    """批量对全量销售成员执行 AI 画像生成并反哺。"""
    sales_list = data_loader.load_sales()
    results = {}
    for s in sales_list:
        if s.sales_id == "admin":
            continue
        res = analyze_sales_profile(s.sales_id, auto_sync_to_model=True)
        results[s.sales_id] = res
    return results
