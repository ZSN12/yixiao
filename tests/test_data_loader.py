# -*- coding: utf-8 -*-
"""数据层(data_loader) pytest 单测: mock 数据加载 / 会话分组 / 经验语料 / SQLite 历史读写。

覆盖: load_all / build_chat_map / load_sales_experiences /
      init_db / save_analysis_record / get_analysis_history(写一条→查回→清空)。
数据库全部落在 conftest 的临时 DB 上(isolated_env autouse), 不污染项目 data/sales_agent.db。
"""

import sqlite3

from modules import data_loader


def test_load_all_full_mock(isolated_env):
    """三份 mock 数据齐全: 9 客户 / 9 会话记录 / 5 销售。"""
    customers, records, sales = data_loader.load_all()
    assert len(customers) == 9
    assert len(records) == 9
    assert len(sales) == 5
    # 字段契约抽查
    first = customers[0]
    assert first.customer_id and first.customer_name and first.industry
    assert first.city and first.scale and first.create_time
    assert all(r.record_id and r.customer_id and r.messages for r in records)
    # admin 是设计使然的兜底角色(分配器明确"排除 admin"): 行业/城市为空列表是设计而非脏数据。
    # 因此断言拆成两部分 —— ① 非 admin 销售字段必须齐全; ② admin 存在且恰为兜底角色。
    non_admin = [s for s in sales if s.sales_id != "admin"]
    assert len(non_admin) == 4
    assert all(s.sales_id and s.name
               and s.good_at_industries and s.responsible_cities
               for s in non_admin)
    admins = [s for s in sales if s.sales_id == "admin"]
    assert len(admins) == 1
    assert admins[0].name == "默认管理员"
    assert admins[0].good_at_industries == [] and admins[0].responsible_cities == []


def test_build_chat_map_groups_by_customer(isolated_env):
    """会话按 customer_id 分组, 组内元素为 ChatRecord 列表。"""
    _customers, records, _sales = data_loader.load_all()
    chat_map = data_loader.build_chat_map(records)
    assert set(chat_map.keys()) <= {c.customer_id for c in _customers}
    for cid, recs in chat_map.items():
        assert all(r.customer_id == cid for r in recs)
    # 无会话的客户不出现在结果中
    assert len(chat_map) <= 9


def test_load_sales_experiences(isolated_env):
    """经验语料 10 条, 字段契约完整(供 RAG 检索)。"""
    experiences = data_loader.load_sales_experiences()
    assert len(experiences) == 10
    for exp in experiences:
        assert exp.sales_id and exp.content
        assert exp.industry
    assert {e.sales_id for e in experiences} <= {"S001", "S002", "S003", "S004"}


def test_init_db_and_history_write_query_clear(isolated_env):
    """写一条分析记录 → 查回 → 清空: 往返一致且可清理(临时 DB)。"""
    data_loader.init_db()
    # 写
    data_loader.save_analysis_record(
        "C-TEST-1", "单测客户",
        {"customer_profile": "某新能源客户", "intention_level": "高", "core_demands": ["a", "b"]},
    )
    # 查回
    history = data_loader.get_analysis_history("C-TEST-1")
    assert len(history) == 1
    record = history[0]
    assert record["customer_id"] == "C-TEST-1"
    assert record["customer_name"] == "单测客户"
    assert record["result"]["intention_level"] == "高"
    assert record["result"]["core_demands"] == ["a", "b"]
    assert record["created_at"]
    # 无记录客户 → 空列表
    assert data_loader.get_analysis_history("C-NO-EXIST") == []
    # 清空(直接操作临时库, 验证可清理)
    conn = sqlite3.connect(str(isolated_env / "test_sales_agent.db"))
    try:
        conn.execute("DELETE FROM analysis_history WHERE customer_id = ?", ("C-TEST-1",))
        conn.commit()
    finally:
        conn.close()
    assert data_loader.get_analysis_history("C-TEST-1") == []