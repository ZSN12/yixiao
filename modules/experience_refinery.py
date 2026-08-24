# -*- coding: utf-8 -*-
"""经验沉淀管道(experience_refinery): 自动获取 → 提炼 → 入库 → RAG 检索 的闭环实现。

背景: data_loader 预留了 fetch_crm_deals(返回 []) 与 generate_sales_experiences(返回 []),
本模块把这条"经验沉淀链路"落地为可运行的最小闭环:
    list_mock_deals(演示商机) → refine_deals_to_experiences(双引擎提炼)
    → persist_refined_experiences(追加入库到 data/refined_experiences.json)
    → rag_retriever.retrieve_top_sales(消费者直接检索, 见模块末尾"检索消费"说明)。

双引擎提炼(渐进降级, 对齐 profile_analyzer 先例):
1. LLM 引擎(配置 llm_api_key 且 mock_mode=False): 每笔商机打包 Prompt,
   强制 response_format={"type":"json_object"}, 抽取「痛点/打法/结果」三要素
   产出自然语言 content(含 企业规模/预算/决策角色/痛点/过程/结果, 与现有
   mock_sales_experience.json 语料风格一致)。
2. 规则模板引擎(默认/mock/LLM 不可用·超时·格式错时自动降级): 确定性模板拼装 content。
铁律: 任何失败都记日志并降级/跳过, 绝不向上抛异常, 单条失败不中断整批。

入库约定:
- 入库文件 data/refined_experiences.json 与手工语料 mock_sales_experience.json 分离,
  不污染现有 10 条语料。
- 按 (sales_id, industry, content 前20字) 去重, 已存在则跳过, 返回新增条数。
- 加载: 当前 data_loader.load_sales_experiences() 只读手工语料(未支持合并),
  本模块提供 load_refined_experiences() 独立读取入库经验;
  将来可在 data_loader.load_sales_experiences() 中合并读取 refined_experiences.json,
  对消费者(retrieve_top_sales)透明。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from config.settings import settings
from modules.data_loader import (
    PROJECT_ROOT,
    SalesExperience,
    fetch_crm_deals,
)

logger = logging.getLogger(__name__)


# ============================================================
# 常量与路径
# ============================================================

# 入库文件: 与手工语料分离的独立经验库(相对项目根)
REFINED_EXPERIENCES_FILE: Path = PROJECT_ROOT / "data" / "refined_experiences.json"

# 默认演示商机条数
DEFAULT_MOCK_DEALS: int = 4

# outcome 映射: 商机 result → SalesExperience.outcome
_RESULT_TO_OUTCOME: Dict[str, str] = {
    "成单": "成单",
    "跟进中": "跟进中",
    "流失": "流失",
}

# 入库去重: content 取前多少字作为指纹
_DEDUP_CONTENT_PREFIX: int = 20

# LLM 系统提示词(提炼「痛点/打法/结果」三要素)
_SYSTEM_PROMPT = """你是资深B端销售业务分析师，请根据给定的商机记录，提炼出一条可复用的销售经验片段。
要求：
1. 只输出一个JSON对象，字段为 {"content": "..."}，不要输出任何额外解释
2. content 必须是用自然语言写成的经验片段，涵盖：企业规模、预算范围、决策角色、核心痛点、跟进过程、最终结果
3. 内容基于给定商机信息，禁止编造；语言精炼，80-150字，与"2024年服务杭州某新能源电池厂(约300人)..."风格一致
"""


# ============================================================
# 演示商机数据
# ============================================================


def list_mock_deals(n: int = DEFAULT_MOCK_DEALS) -> List[dict]:
    """内置演示商机(字段对齐 fetch_crm_deals 出参)。

    Args:
        n: 返回前多少条演示商机, 默认 4; 非法(<=0)或超过内置条数时自动截断。

    Returns:
        list[dict]: 演示商机列表, 每条含 customer_name/industry/scale/budget/
                    deal_amount/decision_maker/pain_points/result/closed_at/sales_id。
                    覆盖 智能制造/新能源/医药制造 等 3 个行业,
                    sales_id 分散给 S001-S004(兼顾未完全覆盖的 S003/S004)。
    """
    deals: List[dict] = [
        {
            "customer_name": "成都华锐数控装备有限公司",
            "industry": "智能制造",
            "scale": "260",
            "budget": "180-220万",
            "deal_amount": "198万",
            "decision_maker": "制造总监",
            "pain_points": "数控机床联网率低、生产数据靠人工抄录",
            "result": "成单",
            "closed_at": "2024-11-20",
            "sales_id": "S003",
        },
        {
            "customer_name": "上海绿洲储能科技有限公司",
            "industry": "新能源",
            "scale": "420",
            "budget": "300-350万",
            "deal_amount": "320万",
            "decision_maker": "CIO",
            "pain_points": "储能电站监控数据孤岛、安全预警不及时",
            "result": "跟进中",
            "closed_at": "2024-12-05",
            "sales_id": "S002",
        },
        {
            "customer_name": "苏州汉鼎智能装备有限公司",
            "industry": "智能制造",
            "scale": "150",
            "budget": "80万以内",
            "deal_amount": "",
            "decision_maker": "老板兼采购经理",
            "pain_points": "小批量订单排产混乱、交期不可控",
            "result": "流失",
            "closed_at": "2024-10-30",
            "sales_id": "S001",
        },
        {
            "customer_name": "北京康禾生物医药有限公司",
            "industry": "医药制造",
            "scale": "350",
            "budget": "400万",
            "deal_amount": "400万",
            "decision_maker": "信息总监",
            "pain_points": "研发数据安全合规、备份体系不达标",
            "result": "成单",
            "closed_at": "2024-12-18",
            "sales_id": "S004",
        },
    ]
    if not isinstance(n, int) or n <= 0:
        n = DEFAULT_MOCK_DEALS
    return deals[:n]


# ============================================================
# 通用工具
# ============================================================


def _map_outcome(result: str) -> str:
    """商机 result → SalesExperience.outcome(成单→成单/跟进中, 流失→流失)。

    Args:
        result: 商机结果(成单/跟进中/流失)。

    Returns:
        str: 标准 outcome; 无法识别时记日志并按"跟进中"兜底。
    """
    outcome = _RESULT_TO_OUTCOME.get(result)
    if outcome is None:
        logger.warning("无法识别的商机结果 %r, 按「跟进中」兜底", result)
        return "跟进中"
    return outcome


def _fmt_scale(scale: str) -> str:
    """格式化企业规模: 数字 → "约N人", 已含"人"则原样返回。

    Args:
        scale: 原始规模字段(如 "260" / "约400人" / "中大型")。

    Returns:
        str: 展示用规模文本。
    """
    scale = (scale or "").strip()
    if not scale:
        return "规模未明确"
    if "人" in scale:
        return scale
    if scale.isdigit():
        return f"约{scale}人"
    return scale


def _year_of(closed_at: str) -> str:
    """从商机关单/时间字段抽取年份; 取不到返回空串。"""
    if closed_at and len(closed_at) >= 4 and closed_at[:4].isdigit():
        return closed_at[:4]
    return ""


def _deal_to_scene(deal: dict) -> str:
    """把商机记录拼成一句场景说明(供规则模板与 LLM 均使用)。"""
    return (
        f"客户:{deal.get('customer_name', '')}; "
        f"行业:{deal.get('industry', '')}; "
        f"规模:{_fmt_scale(deal.get('scale', ''))}; "
        f"预算:{deal.get('budget', '')}; "
        f"成交额:{deal.get('deal_amount', '') or '未成交'}; "
        f"决策人:{deal.get('decision_maker', '')}; "
        f"痛点:{deal.get('pain_points', '')}; "
        f"结果:{deal.get('result', '')}"
    )


def _llm_enabled() -> bool:
    """判断是否启用 LLM 引擎: 配置了 api_key 且未开 mock_mode。"""
    return bool(settings.llm_api_key) and not settings.mock_mode


# ============================================================
# 双引擎提炼
# ============================================================


def _refine_with_llm(deal: dict) -> str:
    """LLM 引擎: 单笔商机提炼 content(「痛点/打法/结果」三要素自然语言)。

    Args:
        deal: 单笔商机字典(字段对齐 fetch_crm_deals 出参)。

    Returns:
        str: 提炼出的经验片段 content。

    Raises:
        Exception: 网络/超时/格式/openai 未安装/校验失败 —— 由调用方降级到规则模板。
    """
    from openai import OpenAI          # 延迟导入: openai 为可选依赖

    client = OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_api_base,
        timeout=settings.llm_timeout,
    )
    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(deal, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    content_text = resp.choices[0].message.content or ""
    data = json.loads(content_text)      # 非 JSON → 抛异常降级
    content = str(data.get("content", "")).strip()
    if not content:
        raise ValueError("LLM 返回的 content 为空")
    return content


def _refine_with_rules(deal: dict) -> str:
    """规则模板引擎: 确定性拼装 content(不调 LLM, 可复现)。

    Args:
        deal: 单笔商机字典。

    Returns:
        str: 拼装出的经验片段 content, 含 规模/预算/决策角色/痛点/结果。
    """
    year = _year_of(deal.get("closed_at", ""))
    year_prefix = f"{year}年" if year else ""
    content = (
        f"{year_prefix}跟进{deal.get('customer_name', '')}"
        f"({_fmt_scale(deal.get('scale', ''))}), "
        f"预算{deal.get('budget', '')}, "
        f"决策人{deal.get('decision_maker', '')}, "
        f"痛点{deal.get('pain_points', '')}, "
        f"最终{deal.get('result', '')}。"
    )
    return content


def refine_deals_to_experiences(
    deal_records: List[dict],
    sales_id: Optional[str] = None,
) -> List[SalesExperience]:
    """双引擎提炼: 商机 → 经验片段(LLM 优先, 失败降级规则模板)。

    Args:
        deal_records: 商机记录列表(字段对齐 fetch_crm_deals 出参)。
        sales_id: 可选的销售 ID 覆盖 —— 提供时所有经验片段统一归属该销售;
                  缺省取每条商机自身的 sales_id。

    Returns:
        list[SalesExperience]: 每条商机对应一条经验片段(industry 取自商机字段,
                outcome 按 result 映射); 单条提炼失败记日志跳过, 不中断整批,
                任何情况下不抛异常(铁律)。
    """
    experiences: List[SalesExperience] = []
    for i, deal in enumerate(deal_records):
        try:
            content: str
            if _llm_enabled():
                try:
                    content = _refine_with_llm(deal)
                except Exception as exc:  # noqa: BLE001 —— LLM 失败降级规则模板
                    logger.error("商机 #%d(%s) LLM 提炼失败(%s), 降级规则模板",
                                 i + 1, deal.get("customer_name", ""), exc)
                    content = _refine_with_rules(deal)
            else:
                content = _refine_with_rules(deal)

            source_sales_id = sales_id or deal.get("sales_id") or ""
            experiences.append(SalesExperience(
                sales_id=source_sales_id,
                content=content,
                industry=deal.get("industry", ""),
                outcome=_map_outcome(deal.get("result", "")),
            ))
        except Exception as exc:  # noqa: BLE001 —— 单条失败跳过, 绝不抛给上层
            logger.error("商机 #%d(%s) 提炼异常(%s), 跳过该条",
                         i + 1, deal.get("customer_name", ""), exc)
    logger.info("双引擎提炼完成: 输入 %d 条商机, 产出 %d 条经验",
                len(deal_records), len(experiences))
    return experiences


# ============================================================
# 持久化(独立入库文件 + 去重)
# ============================================================


def _read_refined_raw() -> List[dict]:
    """读取入库经验文件的原始 dict 列表; 文件不存在/损坏时返回空列表。"""
    if not REFINED_EXPERIENCES_FILE.exists():
        return []
    try:
        with open(REFINED_EXPERIENCES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            logger.error("经验库文件格式错误(应为数组): %s, 按空库处理",
                         REFINED_EXPERIENCES_FILE)
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("读取经验库失败(%s): %s, 按空库处理", REFINED_EXPERIENCES_FILE, exc)
        return []


def _dedup_key(exp: SalesExperience) -> tuple:
    """入库去重键: (sales_id, industry, content 前 N 字)。"""
    return (exp.sales_id, exp.industry, exp.content[:_DEDUP_CONTENT_PREFIX])


def persist_refined_experiences(experiences: List[SalesExperience]) -> int:
    """追加写入经验库文件 data/refined_experiences.json(去重后新增条数)。

    Args:
        experiences: 待入库的经验片段列表。

    Returns:
        int: 本次实际新增入库的条数(按 (sales_id, industry, content 前20字)
                去重, 已存在则跳过); 文件不存在时自动创建父目录与文件。
    """
    existing_raw = _read_refined_raw()
    existing_items: List[dict] = list(existing_raw)
    existing_keys = {
        (item.get("sales_id", ""), item.get("industry", ""), (item.get("content", "") or "")[:_DEDUP_CONTENT_PREFIX])
        for item in existing_items
    }

    added: List[dict] = []
    for exp in experiences:
        key = _dedup_key(exp)
        if key in existing_keys:
            logger.debug("经验已存在, 跳过: sales_id=%s industry=%s content=%s...",
                         exp.sales_id, exp.industry, exp.content[:_DEDUP_CONTENT_PREFIX])
            continue
        existing_keys.add(key)
        added.append(exp.model_dump())

    if not added:
        logger.info("经验入库去重: 无新增(共 %d 条均为已存在)", len(experiences))
        return 0

    try:
        REFINED_EXPERIENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REFINED_EXPERIENCES_FILE, "w", encoding="utf-8") as fh:
            json.dump(existing_items + added, fh, ensure_ascii=False, indent=2)
        logger.info("经验入库成功: 新增 %d 条, 文件 %s", len(added), REFINED_EXPERIENCES_FILE)
    except OSError as exc:
        # 铁律: 入库失败不抛异常, 记日志返回 0(避免上层崩溃)
        logger.error("经验入库失败(%s): %s, 本次新增 %d 条未落盘", REFINED_EXPERIENCES_FILE, exc, len(added))
        return 0
    return len(added)


def load_refined_experiences() -> List[SalesExperience]:
    """读取入库经验(data/refined_experiences.json)为 SalesExperience 列表。

    Returns:
        list[SalesExperience]: 入库经验片段列表; 文件不存在/损坏/字段校验失败
                时返回空列表(不抛异常)。字段非法条目记日志跳过。

    Notes:
        当前 data_loader.load_sales_experiences() 只读手工语料
        (mock_sales_experience.json), 尚未合并本文件 —— 消费者如需全量语料,
        可自行拼接 load_sales_experiences() + load_refined_experiences();
        将来可在 load_sales_experiences() 内合并读取本文件, 对检索器透明。
    """
    raw_items = _read_refined_raw()
    result: List[SalesExperience] = []
    for item in raw_items:
        try:
            result.append(SalesExperience(**item))
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning("经验库中存在非法条目, 已跳过: %s", exc)
    logger.info("读取入库经验: %d 条(文件 %s)", len(result), REFINED_EXPERIENCES_FILE)
    return result


# ============================================================
# 端到端管道
# ============================================================


def run_refinery_pipeline(
    sales_id: Optional[str] = None,
    days: int = 90,
) -> List[SalesExperience]:
    """端到端经验沉淀管道: 获取 → 提炼 → 入库, 返回本次提炼并入库的经验。

    Args:
        sales_id: 可选的销售 ID 过滤/覆盖; 传给 fetch_crm_deals 与提炼覆盖。
        days: 拉取最近 N 天商机(传给 fetch_crm_deals)。

    Returns:
        list[SalesExperience]: 本次提炼并成功入库的经验片段列表
                (被去重跳过的已存在条目不在返回中)。
    """
    # 1) 获取商机: 优先真实/预留接口, 返回空则用内置演示商机
    try:
        deals = fetch_crm_deals(sales_id=sales_id, days=days)
    except Exception as exc:  # noqa: BLE001 —— 获取失败不中断
        logger.error("fetch_crm_deals 调用异常(%s), 改用演示商机", exc)
        deals = []
    if not deals:
        logger.info("fetch_crm_deals 未返回商机, 使用内置演示商机(n=%d)", DEFAULT_MOCK_DEALS)
        deals = list_mock_deals(DEFAULT_MOCK_DEALS)
        if sales_id:
            deals = [d for d in deals if d.get("sales_id") == sales_id]

    # 2) 提炼(双引擎)
    refined = refine_deals_to_experiences(deals, sales_id=sales_id)

    # 3) 入库(去重追加)
    before_keys = {
        (item.get("sales_id", ""), item.get("industry", ""),
         (item.get("content", "") or "")[:_DEDUP_CONTENT_PREFIX])
        for item in _read_refined_raw()
    }
    added_count = persist_refined_experiences(refined)
    persisted = [e for e in refined if _dedup_key(e) not in before_keys]
    logger.info("经验沉淀管道完成: 商机 %d 条 → 提炼 %d 条 → 新增入库 %d 条",
                len(deals), len(refined), added_count)
    return persisted


# ============================================================
# 检索消费(说明)
# ============================================================
# 入库后的新经验通过 load_refined_experiences() 读取, 与
# data_loader.load_sales_experiences() 的手工语料合并后, 即作为
# rag_retriever.retrieve_top_sales 的 experiences 参数参与检索 ——
# 新语料与旧语料同样会进入 粗排(相似度) + 规则加权精排, 并可在
# SalesMatch.matched_experiences 溯源中看到。本模块不直接 import
# rag_retriever, 保持"沉淀方/消费方"单向解耦。
#
# 用法示例(消费者侧):
#   from modules.experience_refinery import load_refined_experiences
#   from modules.data_loader import load_sales_experiences
#   experiences = load_sales_experiences() + load_refined_experiences()
#   matches = retrieve_top_sales(query_text, sales_list, experiences)