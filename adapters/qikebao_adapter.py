# -*- coding: utf-8 -*-
"""企客宝适配器(qikebao_adapter): 企客宝原始 dict → 易销 Pydantic 模型。

职责: 把企客宝 OpenAPI 返回的客户/聊天原始 dict 映射为易销已有的
Customer / ChatRecord 模型, 做到「接企客宝调用方零改动」。

对标 crm_data_adapter / phone_call_adapter 的角色映射约定:
- 企客宝「员工/内部成员」→ ChatMessage.role "销售";
- 企客宝「外部联系人/客户」→ ChatMessage.role "客户"。

字段映射(初版, 拿到真实响应后微调本模块即可, 不影响调用方):
见模块底部 map_customer 的 docstring 对照表。

独立运行(演示):
    python adapters/qikebao_adapter.py
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.data_loader import ChatMessage, ChatRecord, Customer  # noqa: E402

logger = logging.getLogger(__name__)

SAMPLE_FILE = PROJECT_ROOT / "data" / "real" / "qikebao_customers_sample.json"


def _prefix() -> str:
    from config.settings import settings
    return settings.qikebao_customer_id_prefix or "QKB-"


def map_customer(raw: Dict) -> Customer:
    """把企客宝客户原始 dict 映射为 Customer。

    字段映射(待核对, 拿到真实响应后微调):

        | 企客宝字段(待核对)      | 易销 Customer          | 兜底       |
        |------------------------|-----------------------|-----------|
        | id                     | customer_id(QKB-前缀) | —         |
        | name / alias / 备注名   | customer_name         | "未知客户" |
        | industry / 标签        | industry              | "其他"    |
        | province + city        | city                  | "未知"    |
        | scale(若有)            | scale                 | "未知"    |
        | owner_user_id          | owner_sales_id        | None      |
        | created_at             | create_time           | 当天日期   |
        | social_security_count  | social_security_count | None      |

    ⚠️ owner_user_id → owner_sales_id 映射需核对 ID 体系:
    企客宝返回的 owner_user_id 是企客宝侧的内部用户 ID(通常为数字/自增),
    而易销的销售 ID 是飞书/种子数据里的字符串销售编号(如 "S001")。
    若两者 ID 体系不一致, 直接透传会导致: 同步进来的客户 owner_sales_id
    无法匹配任何现有销售, 从而在「销售只看自己客户」的权限过滤下被隐藏。
    上线前请确认(二选一):
      1) 企客宝 owner_user_id 本身就是易销的 Sxxx 编号 → 无需处理;
      2) 否则需在此处做 ID 映射(如通过手机号/企客宝员工表换算出 Sxxx),
         并在配置里补充映射表或查询逻辑。

    Args:
        raw: 企客宝客户原始 dict。

    Returns:
        Customer: 易销客户模型。
    """
    raw_id = str(raw.get("id") or raw.get("customer_id") or "").strip()
    name = (
        str(raw.get("name") or raw.get("alias") or raw.get("customer_name") or raw.get("备注名") or "").strip()
        or "未知客户"
    )
    industry = str(raw.get("industry") or raw.get("标签") or "").strip() or "其他"
    province = str(raw.get("province") or "").strip()
    city = str(raw.get("city") or "").strip()
    city_str = (province + city).strip() or "未知"
    scale = str(raw.get("scale") or raw.get("规模") or "").strip() or "未知"
    owner = str(raw.get("owner_user_id") or raw.get("owner_sales_id") or "").strip() or None
    create_time = str(raw.get("created_at") or raw.get("create_time") or "").strip()
    if not create_time:
        create_time = date.today().isoformat()
    else:
        create_time = create_time[:10]   # 对齐 YYYY-MM-DD

    # raw_id 缺失时用 uuid 兜底, 避免多笔无 ID 记录映射成同一个 customer_id
    # 导致画像分析/会话分组互相覆盖。
    return Customer(
        customer_id=f"{_prefix()}{raw_id}" if raw_id else f"{_prefix()}{uuid.uuid4().hex[:8]}",
        customer_name=name,
        industry=industry,
        city=city_str,
        scale=scale,
        owner_sales_id=owner,
        follow_up_status="待跟进",
        create_time=create_time,
        social_security_count=str(raw.get("social_security_count") or "") or None,
    )


def map_chat_records(
    raw_messages: List[Dict],
    customer_id: str,
    sales_id: Optional[str] = None,
) -> List[ChatRecord]:
    """把企客宝聊天消息原始 list 映射为 ChatRecord 列表(P1 预留)。

    角色映射: 发送方为「员工/内部成员」→ "销售"; 「外部联系人/客户」→ "客户";
    其他发送方跳过。单条消息聚合成一条 ChatRecord(record_id 用序号)。

    Args:
        raw_messages: 消息原始 dict 列表。
        customer_id: 易销客户 ID(已加前缀)。
        sales_id: 关联销售 ID(可空)。

    Returns:
        list[ChatRecord]: 会话记录列表(可能为空)。
    """
    if not raw_messages:
        return []

    messages: List[ChatMessage] = []
    for msg in raw_messages:
        sender = str(msg.get("sender_type") or msg.get("from_type") or msg.get("role") or "").strip()
        if sender in ("员工", "内部成员", "销售", "staff", "member", "1"):
            role = "销售"
        elif sender in ("外部联系人", "客户", "customer", "external", "2"):
            role = "客户"
        else:
            continue  # 未知发送方跳过
        content = str(msg.get("content") or msg.get("text") or "").strip()
        if content:
            messages.append(ChatMessage(role=role, content=content))

    if not messages:
        return []

    # 单条会话(时间取第一条消息时间或当天)
    ts = str(raw_messages[0].get("created_at") or raw_messages[0].get("msg_time") or "").strip()
    if not ts:
        ts = date.today().isoformat()
    return [ChatRecord(
        record_id=f"{customer_id}-CHAT",
        customer_id=customer_id,
        sales_id=sales_id,
        chat_time=ts[:10],
        messages=messages,
    )]


def load_customers_from_qikebao() -> List[Customer]:
    """从企客宝加载客户(mock 模式读 sample JSON, 否则调 OpenAPI)。

    Returns:
        list[Customer]: 映射后的客户列表; 失败返回空列表(不抛)。
    """
    from config.settings import settings

    if settings.qikebao_mock_mode:
        if not SAMPLE_FILE.exists():
            logger.warning("企客宝样例文件不存在: %s", SAMPLE_FILE)
            return []
        try:
            with open(SAMPLE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("读取企客宝样例失败: %s", exc)
            return []
        rows = (data.get("data") or {}).get("list") if isinstance(data.get("data"), dict) else []
        if not rows:
            rows = data.get("data") or [] if isinstance(data.get("data"), list) else []
    else:
        try:
            from adapters import qikebao_client
            corp_id = settings.qikebao_corp_id
            if not corp_id:
                logger.error("企客宝 corp_id 未配置, 无法拉取客户")
                return []
            rows = qikebao_client.get_all_customers(corp_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("企客宝客户拉取失败(%s), 返回空列表", exc)
            return []

    customers: List[Customer] = []
    for raw in rows:
        try:
            customers.append(map_customer(raw))
        except Exception as exc:  # noqa: BLE001 —— 单条映射失败跳过
            logger.warning("企客宝客户映射失败, 跳过: %s", exc)
    logger.info("企客宝客户映射完成: %d 家", len(customers))
    return customers


def load_chat_map_from_qikebao(customers: List[Customer]) -> Dict[str, List[ChatRecord]]:
    """从企客宝加载聊天记录并分组(P1; 未开通会话存档返回空 dict)。

    Args:
        customers: 已映射的客户列表(用于确定 customer_id)。

    Returns:
        dict[str, list[ChatRecord]]: customer_id → 会话记录列表。
    """
    from config.settings import settings
    if not settings.qikebao_sync_chat:
        return {}
    chat_map: Dict[str, List[ChatRecord]] = {}
    for c in customers:
        try:
            from adapters import qikebao_client
            raw = qikebao_client.get_chat_messages(settings.qikebao_corp_id, c.customer_id)
            records = map_chat_records(raw, c.customer_id, c.owner_sales_id)
            if records:
                chat_map[c.customer_id] = records
        except Exception as exc:  # noqa: BLE001
            logger.warning("客户 %s 聊天记录拉取失败, 跳过: %s", c.customer_id, exc)
    return chat_map


def run_qikebao_demo() -> Dict:
    """端到端实证: 企客宝(样例) → Customer → 画像分析。

    流程与 crm_data_adapter.run_real_data_demo 一致, 输出实证摘要。

    Returns:
        dict: {"customer_count", "analyzed", "intention_stats", "data_source"}。
    """
    from modules import profile_analyzer

    customers = load_customers_from_qikebao()
    chat_map = load_chat_map_from_qikebao(customers) or {}
    analysis = profile_analyzer.analyze_customers_batch(customers, chat_map)

    intention_stats: Dict[str, int] = {}
    for r in analysis.values():
        intention_stats[r.intention_level] = intention_stats.get(r.intention_level, 0) + 1

    logger.info("企客宝实证完成: 客户 %d 家, 分析 %d 家, 意向 %s",
                len(customers), len(analysis), intention_stats)
    return {
        "data_source": "qikebao",
        "customer_count": len(customers),
        "analyzed": len(analysis),
        "intention_stats": intention_stats,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = run_qikebao_demo()
    print("\n=== 企客宝适配实证 ===")
    print(f"数据来源: {summary['data_source']}")
    print(f"客户数: {summary['customer_count']}")
    print(f"分析客户数: {summary['analyzed']}")
    print(f"意向分层: {summary['intention_stats']}")
