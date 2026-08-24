# -*- coding: utf-8 -*-
"""线索分配器 Agent Memory 闭环 pytest 单测(临时 DB 隔离, 不碰真实库)。

覆盖(对照任务 t12 检查点):
① 首次 assign_leads_with_memory(记忆库为空)→ 高置信结果自动写弱记忆;
② submit_feedback 升级 weak → strong(修正 C001 → S001);
③ 再分配受强记忆影响(C001 推荐=修正值 S001, match_reason 含"命中历史强记忆"溯源);
④ 回归: 原 assign_leads(无记忆)分配结果不变。
全部落在 conftest 临时 DB(sales_agent 同库 memory_store 表), 测试间互不污染。
"""

from modules import agent_memory as am
from modules import lead_assigner as la
from modules.data_loader import load_customers, load_sales, load_sales_experiences

# 与 test_lead_assigner 一致的集成验收基线(无记忆回归对照)
EXPECTED_BASELINE = {
    "C001": "S003", "C003": "S002", "C005": "S002", "C007": "S004", "C009": "S003",
}


def _load_unassigned() -> tuple:
    customers = load_customers()
    sales = load_sales()
    experiences = load_sales_experiences()
    unassigned = [c for c in customers if c.owner_sales_id is None]
    return unassigned, sales, experiences


def test_memory_loop_end_to_end(isolated_env):
    """记忆闭环: 写弱记忆 → 升级强记忆 → 影响再分配 → 无记忆回归不变。"""
    unassigned, sales, experiences = _load_unassigned()
    assert len(unassigned) == 5

    # ---- ④ 回归基线(先跑, 独立于记忆库) ----
    plain_baseline = la.assign_leads(unassigned, sales, experiences)
    assert {r.customer_id: r.sales_id for r in plain_baseline} == EXPECTED_BASELINE

    # ---- ① 首次记忆增强分配(记忆库为空)→ 自动写弱记忆 ----
    mem1 = la.assign_leads_with_memory(unassigned, sales, experiences, memory=am)
    assert len(mem1) == 5
    weak = [e for e in am.list_memories(limit=50) if e.source == "weak"]
    assert weak, "高置信结果应自动写入弱记忆"
    assert all(e.source == "weak" and e.confidence == 0.9 and e.decision == "confirm" for e in weak)
    # 弱记忆覆盖 5 个无归属客户(与基线分配一致的高置信结果)
    assert {e.customer_id for e in weak} == {"C001", "C003", "C005", "C007", "C009"}

    # ---- ② 人工反馈: 修正 C001(原推荐 S003 → 人工指定 S001) ----
    upgraded = la.submit_feedback("C001", "S001", note="苏州智能制造由张伟跟进更合适", memory=am)
    assert upgraded is not None
    assert upgraded.source == "strong" and upgraded.decision == "correct"
    assert upgraded.correct_sales_id == "S001"
    # 该客户 weak / strong 并存(弱记忆保留供参考)
    all_entries = am.list_memories(limit=50)
    assert any(e.source == "weak" and e.customer_id == "C001" for e in all_entries)
    assert any(e.source == "strong" and e.customer_id == "C001" for e in all_entries)

    # ---- ③ 再跑记忆增强分配 → C001 受强记忆影响 ----
    mem2 = la.assign_leads_with_memory(unassigned, sales, experiences, memory=am)
    c001 = next(r for r in mem2 if r.customer_id == "C001")
    assert c001.sales_id == "S001", f"C001 应被强记忆修正为 S001, 实得 {c001.sales_id}"
    assert "命中历史强记忆(S001" in c001.match_reason, "match_reason 应含强记忆溯源"

    # ---- ④ 回归: 原 assign_leads(无记忆)结果不变 ----
    plain_after = la.assign_leads(unassigned, sales, experiences)
    assert {r.customer_id: r.sales_id for r in plain_after} == EXPECTED_BASELINE
    assert [(r.customer_id, r.sales_id) for r in plain_after] == \
           [(r.customer_id, r.sales_id) for r in plain_baseline]


def test_memory_none_equals_plain(isolated_env):
    """memory=None 时 assign_leads_with_memory 完全等价原 assign_leads。"""
    unassigned, sales, experiences = _load_unassigned()
    plain = la.assign_leads(unassigned, sales, experiences)
    with_mem_none = la.assign_leads_with_memory(unassigned, sales, experiences, memory=None)
    assert [(r.customer_id, r.sales_id) for r in with_mem_none] == \
           [(r.customer_id, r.sales_id) for r in plain]


def test_memory_db_isolation(isolated_env):
    """隔离验证: 记忆写入落在临时 DB, 不会污染项目 data/sales_agent.db。"""
    unassigned, sales, experiences = _load_unassigned()
    la.assign_leads_with_memory(unassigned, sales, experiences, memory=am)
    entries = am.list_memories(limit=50)
    assert entries, "临时记忆库应可读"
    # 临时库文件存在且与 settings.db_path 一致(指向 pytest tmp 目录)
    from pathlib import Path
    db_file = Path(am._resolve_db_path())
    assert db_file.exists()
    assert "test_sales_agent.db" in db_file.name
    assert "pytest-of-" in str(db_file) or "test_" in str(db_file.parent.name)