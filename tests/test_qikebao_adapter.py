# -*- coding: utf-8 -*-
"""企客宝适配器(qikebao_adapter) pytest 单测。

覆盖 P0 验收:
1. map_customer: QKB- 前缀、字段映射、name 兜底、industry/city/scale 兜底;
2. map_customer: owner 空 → None; created_at 缺省 → 当天日期; social_security_count 透传;
3. load_customers_from_qikebao(mock 模式): 读 sample JSON → 3 家, 均带 QKB- 前缀;
4. 无凭证(real 模式且未配 corp_id) → 返回空列表, 不抛;
5. map_chat_records: 角色映射(员工→销售/外部→客户), 未知发送方跳过, 空输入 → [];
6. load_chat_map_from_qikebao: sync_chat=False → 空 dict。
"""

from datetime import date
from pathlib import Path

import pytest

from adapters.qikebao_adapter import (
    map_chat_records,
    map_customer,
    load_chat_map_from_qikebao,
    load_customers_from_qikebao,
)
from modules.data_loader import Customer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_FILE = PROJECT_ROOT / "data" / "real" / "qikebao_customers_sample.json"


@pytest.fixture
def mock_mode(monkeypatch):
    """强制企客宝走 mock 模式(读样例 JSON)。"""
    from config.settings import settings
    monkeypatch.setattr(settings, "qikebao_mock_mode", True)
    monkeypatch.setattr(settings, "qikebao_sync_chat", False)
    monkeypatch.setattr(settings, "qikebao_customer_id_prefix", "QKB-")
    return settings


def test_map_customer_qkb_prefix_and_fields():
    raw = {
        "id": "10001",
        "name": "苏州博创智能装备有限公司",
        "alias": "王总",
        "industry": "智能制造",
        "province": "江苏省",
        "city": "苏州市",
        "scale": "中大型",
        "owner_user_id": "S001",
        "social_security_count": "280",
        "created_at": "2026-07-11 10:00:00",
    }
    c = map_customer(raw)
    assert isinstance(c, Customer)
    assert c.customer_id == "QKB-10001"
    assert c.customer_name == "苏州博创智能装备有限公司"
    assert c.industry == "智能制造"
    assert c.city == "江苏省苏州市"
    assert c.scale == "中大型"
    assert c.owner_sales_id == "S001"
    assert c.social_security_count == "280"
    assert c.create_time == "2026-07-11"


def test_map_customer_fallbacks():
    """name/industry/city/scale 全部缺省 → 兜底值; created_at 缺省 → 当天。"""
    raw = {"id": "10099"}
    c = map_customer(raw)
    assert c.customer_id == "QKB-10099"
    assert c.customer_name == "未知客户"
    assert c.industry == "其他"
    assert c.city == "未知"
    assert c.scale == "未知"
    assert c.owner_sales_id is None
    assert c.social_security_count is None
    assert c.create_time == date.today().isoformat()


def test_map_customer_alias_fallback_and_empty_owner():
    """无 name 时用 alias; owner 空串 → None。"""
    raw = {"id": "10003", "alias": "陈经理", "owner_user_id": ""}
    c = map_customer(raw)
    assert c.customer_name == "陈经理"
    assert c.owner_sales_id is None


def test_load_customers_mock_mode(mock_mode):
    """mock 模式读样例 JSON → 3 家, 均带 QKB- 前缀, 无重复 id。"""
    customers = load_customers_from_qikebao()
    assert len(customers) == 3
    ids = [c.customer_id for c in customers]
    assert all(i.startswith("QKB-") for i in ids)
    assert len(set(ids)) == 3


def test_load_customers_real_mode_no_corp_id(mock_mode):
    """real 模式但未配 corp_id → 返回空列表, 不抛。"""
    mock_mode.qikebao_mock_mode = False
    mock_mode.qikebao_corp_id = ""
    assert load_customers_from_qikebao() == []


def test_map_chat_records_role_mapping():
    raw = [
        {"sender_type": "员工", "content": "您好，方案发您了"},
        {"sender_type": "外部联系人", "content": "预算 500 万，能谈吗？"},
        {"sender_type": "未知方", "content": "应被跳过"},
        {"sender_type": "员工", "content": "可以，我们详谈"},
    ]
    records = map_chat_records(raw, "QKB-10001", "S001")
    assert len(records) == 1
    rec = records[0]
    assert rec.customer_id == "QKB-10001"
    assert rec.sales_id == "S001"
    roles = [m.role for m in rec.messages]
    assert roles == ["销售", "客户", "销售"]
    assert all(m.content for m in rec.messages)


def test_map_chat_records_empty_and_all_unknown():
    assert map_chat_records([], "QKB-1") == []
    assert map_chat_records([{"sender_type": "机器人", "content": "自动回复"}], "QKB-1") == []


def test_map_chat_records_empty_content_skipped():
    raw = [
        {"sender_type": "员工", "content": ""},
        {"sender_type": "员工", "content": "有效内容"},
    ]
    records = map_chat_records(raw, "QKB-1")
    assert len(records[0].messages) == 1
    assert records[0].messages[0].content == "有效内容"


def test_load_chat_map_sync_chat_disabled(mock_mode):
    """sync_chat=False → 返回空 dict(不调 OpenAPI)。"""
    mock_mode.qikebao_sync_chat = False
    customers = load_customers_from_qikebao()
    assert load_chat_map_from_qikebao(customers) == {}
