# -*- coding: utf-8 -*-
"""线索分配器(lead_assigner) pytest 单测: 全量分配 / 行业硬约束 / 负载均衡 / admin 兜底 / 溯源。

覆盖:
- assign_leads 对 5 个无归属 mock 客户全部出结果, 且与确定性的集成验收基线一致;
- 行业硬约束优先(即使另一销售负载更低);
- 同分负载均衡(相同行业候选取 current_load 最小);
- admin 兜底(合成"无任何覆盖"的客户 → 默认管理员 + needs_human);
- match_reason 可追溯(含行业/城市命中 + RAG 依据 + 负载均衡, 防拍脑袋分配)。
"""

from modules.data_loader import Customer, Sales, load_customers, load_sales, load_sales_experiences
from modules.lead_assigner import AssignmentResult, FALLBACK_SALES_ID, assign_leads

# 集成验收基线(确定性, 与 mock 数据一一对应)
EXPECTED_BASELINE = {
    "C001": "S003", "C003": "S002", "C005": "S002", "C007": "S004", "C009": "S003",
}


def _mk_sales(sales_id: str, name: str, industries, cities, load: int) -> Sales:
    """构造合成销售(单测专用, 简化字段)。"""
    return Sales(
        sales_id=sales_id, name=name,
        good_at_industries=industries, responsible_cities=cities, current_load=load,
    )


def _mk_customer(customer_id: str, industry: str, city: str) -> Customer:
    """构造合成无归属客户。"""
    return Customer(
        customer_id=customer_id, customer_name=f"{customer_id}公司",
        industry=industry, city=city, scale="中型",
        owner_sales_id=None, create_time="2024-01-01",
    )


def test_assign_leads_all_unassigned_have_results(isolated_env):
    """5 个无归属客户全有分配结果, 与集成验收基线一致(确定性)。"""
    customers = load_customers()
    sales = load_sales()
    experiences = load_sales_experiences()
    unassigned = [c for c in customers if c.owner_sales_id is None]
    assert len(unassigned) == 5

    results = assign_leads(unassigned, sales, experiences)
    assert len(results) == 5
    for r in results:
        assert isinstance(r, AssignmentResult)
        assert r.customer_id and r.customer_name and r.sales_id and r.sales_name
        assert 0.0 <= r.rag_score <= 1.0
        assert not r.needs_human and r.rule_matched   # mock 客户均被规则覆盖
    # 确定性基线(与集成验收一致)
    assert {r.customer_id: r.sales_id for r in results} == EXPECTED_BASELINE
    # 确定性: 重跑结果一致
    results2 = assign_leads(unassigned, sales, experiences)
    assert [(r.customer_id, r.sales_id, r.rag_score) for r in results2] == \
           [(r.customer_id, r.sales_id, r.rag_score) for r in results]


def test_match_reason_traceable(isolated_env):
    """match_reason 可追溯: 含规则依据(行业/城市) + RAG 依据 + 负载均衡 + 推荐。"""
    customers = load_customers()
    sales = load_sales()
    experiences = load_sales_experiences()
    unassigned = [c for c in customers if c.owner_sales_id is None]
    results = assign_leads(unassigned, sales, experiences)
    for r in results:
        assert "行业匹配" in r.match_reason or "城市匹配" in r.match_reason
        assert "负载均衡" in r.match_reason
        assert "推荐" in r.match_reason
        assert r.sales_id in r.match_reason   # 推荐对象可追溯


def test_industry_hard_constraint_preferred(isolated_env):
    """行业硬约束优先: 客户行业有覆盖销售时, 即使该销售负载更高也优先(候选1 行业)。"""
    sales_a = _mk_sales("S_A", "甲", ["智能制造"], ["上海"], load=5)
    sales_b = _mk_sales("S_B", "乙", ["物流"], ["苏州"], load=0)
    cust = _mk_customer("C_X", "智能制造", "上海")
    results = assign_leads([cust], [sales_a, sales_b], [])
    assert len(results) == 1
    r = results[0]
    assert r.sales_id == "S_A", "行业硬约束应优先于负载更低的其他销售"
    assert r.rule_matched and not r.needs_human
    assert "行业匹配(智能制造)" in r.match_reason


def test_load_balancing_tie_break(isolated_env):
    """同分负载均衡: 同一行业的两个候选, 取 current_load 最小者。"""
    sales_a = _mk_sales("S_A", "甲", ["智能制造"], ["苏州"], load=5)
    sales_b = _mk_sales("S_B", "乙", ["智能制造"], ["苏州"], load=0)
    cust = _mk_customer("C_X", "智能制造", "苏州")
    results = assign_leads([cust], [sales_a, sales_b], [])
    r = results[0]
    assert r.sales_id == "S_B", "同行业同分候选应选负载最小者"
    assert "负载均衡(当前负载0)" in r.match_reason


def test_admin_fallback_for_uncovered_customer(isolated_env):
    """admin 兜底: 行业/城市均无覆盖的合成客户 → 默认管理员 + needs_human。"""
    sales = [_mk_sales("S_A", "甲", ["智能制造"], ["苏州"], load=0)]
    cust = _mk_customer("C_ALIEN", "航天军工", "拉萨")
    results = assign_leads([cust], sales, [])
    assert len(results) == 1
    r = results[0]
    assert r.sales_id == FALLBACK_SALES_ID
    assert r.sales_name == "默认管理员"
    assert r.needs_human is True
    assert not r.rule_matched
    assert "待人工" in r.match_reason


def test_owner_assigned_customers_skipped(isolated_env):
    """防御: 已归属客户被跳过, 不产生分配结果。"""
    customers = load_customers()
    sales = load_sales()
    experiences = load_sales_experiences()
    owned = [c for c in customers if c.owner_sales_id is not None]
    assert owned
    results = assign_leads(owned, sales, experiences)
    assert results == []   # 全部被跳过


def test_empty_customers(isolated_env):
    """边界: 空客户列表 → 空结果。"""
    sales = load_sales()
    experiences = load_sales_experiences()
    assert assign_leads([], sales, experiences) == []