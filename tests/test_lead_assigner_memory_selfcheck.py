# -*- coding: utf-8 -*-
"""lead_assigner Agent Memory 闭环自检脚本(任务 t12 检查点 ①-④)。

运行: cd <项目根> && python3 tests/test_lead_assigner_memory_selfcheck.py

演示链路:
① 真实 9 客户跑 assign_leads_with_memory 首次(记忆库为空) → 高置信结果自动写弱记忆;
② 人工反馈 submit_feedback("C001", "S001") 修正(原推荐 S003) → 升级 strong(decision=correct);
③ 再跑 assign_leads_with_memory → C001 分配被强记忆影响(推荐=修正值 S001),
   match_reason 含"命中历史强记忆"溯源;
④ 回归: 原 assign_leads(不带记忆)与集成验收基线一致(5/5 分配)。

注意: 本脚本会清空 memory_store 表(仅 memory 表, 不动 analysis_history)。
"""
import os
import sqlite3
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from modules import agent_memory as am
from modules.data_loader import (
    load_customers, load_sales, load_sales_experiences,
)
import modules.lead_assigner as la

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def clear_memory_table():
    """清空 memory_store 表(保证演示确定性与可重复)。"""
    db_file = am._resolve_db_path()
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(f"DELETE FROM {am.MEMORY_TABLE}")
        conn.commit()
        print(f"已清空记忆表: {db_file}({am.MEMORY_TABLE})")
    finally:
        conn.close()


# ---------- 准备 ----------
clear_memory_table()
am.init_memory_db()
customers = load_customers()
sales_list = load_sales()
experiences = load_sales_experiences()
unassigned = [c for c in customers if c.owner_sales_id is None]
print(f"客户 {len(customers)} 家, 销售 {len(sales_list)} 名, 经验 {len(experiences)} 条, "
      f"无归属客户 {len(unassigned)} 家")

# ============================================================
# ④ 回归(先跑, 独立于记忆链路): 原 assign_leads 无记忆行为与基线一致
# ============================================================
print("\n=== ④ 回归: assign_leads 无记忆行为 ===")
results_plain = la.assign_leads(unassigned, sales_list, experiences, analysis_results=None)
for r in results_plain:
    print(f"  {r.customer_id} -> {r.sales_id} {r.sales_name} | {r.match_reason.split(', 推荐')[0]}")
check("④ 原 assign_leads 5/5 全部有结果", len(results_plain) == 5)
check("④ 全部规则命中且非 admin",
      all(r.sales_id != "admin" and r.rule_matched for r in results_plain))
# 与集成验收基线比对(固定期望分配)
expected = {"C001": "S003", "C003": "S002", "C005": "S002", "C007": "S004", "C009": "S003"}
check("④ 分配结果与集成验收基线一致",
      {r.customer_id: r.sales_id for r in results_plain} == expected)

# ============================================================
# ① 首次记忆增强分配(记忆库为空) → 自动写弱记忆
# ============================================================
print("\n=== ① 首次 assign_leads_with_memory(记忆库空) → 自动写弱记忆 ===")
results_mem1 = la.assign_leads_with_memory(
    unassigned, sales_list, experiences,
    analysis_results=None, top_k=5, memory=am,
)
for r in results_mem1:
    print(f"  {r.customer_id} -> {r.sales_id} {r.sales_name} | rag={r.rag_score:.4f}")
check("① 首次记忆分配 5/5 有结果", len(results_mem1) == 5)
check("① 弱记忆已自动写入(高置信=规则与RAG Top1一致)",
      len(am.list_memories(limit=50)) == 5,
      f"(当前 {len(am.list_memories(limit=50))} 条)")
weak_mems = [e for e in am.list_memories(limit=50) if e.source == "weak"]
check("① 写入的均为 weak 且 confidence=0.9/dcision=confirm",
      len(weak_mems) == 5 and all(
          e.source == "weak" and e.confidence == 0.9 and e.decision == "confirm"
          for e in weak_mems))

# ============================================================
# ② 人工反馈: 修正 C001(原推荐 S003 → 人工指定 S001)
# ============================================================
print("\n=== ② submit_feedback: 人工修正 C001 → S001 ===")
upgraded = la.submit_feedback("C001", "S001", note="苏州智能制造装备商由张伟跟进更合适", memory=am)
print(f"  返回: source={upgraded.source} decision={upgraded.decision} "
      f"correct={upgraded.correct_sales_id} sales={upgraded.sales_id} note={upgraded.feedback_note}")
check("② 升级为 strong", upgraded is not None and upgraded.source == "strong")
check("② decision=correct(修正)且 correct_sales_id=S001",
      upgraded.decision == "correct" and upgraded.correct_sales_id == "S001")
check("② 对应弱记忆仍在(两者并存)",
      any(e.source == "weak" and e.customer_id == "C001" for e in am.list_memories(limit=50)))

# 确认反馈场景: 对 C003 提交与推荐一致的销售(S002)→ confirm
upgraded2 = la.submit_feedback("C003", "S002", note="确认李娜", memory=am)
print(f"  确认场景返回: source={upgraded2.source} decision={upgraded2.decision} correct={upgraded2.correct_sales_id}")
check("② 一致时 decision=confirm", upgraded2 is not None and upgraded2.decision == "confirm")

# ============================================================
# ③ 再跑记忆增强分配 → C001 受强记忆影响
# ============================================================
print("\n=== ③ 再跑 assign_leads_with_memory → C001 受强记忆影响 ===")
results_mem2 = la.assign_leads_with_memory(
    unassigned, sales_list, experiences,
    analysis_results=None, top_k=5, memory=am,
)
for r in results_mem2:
    print(f"  {r.customer_id} -> {r.sales_id} {r.sales_name} | {r.match_reason}")

c001 = next(r for r in results_mem2 if r.customer_id == "C001")
c003 = next(r for r in results_mem2 if r.customer_id == "C003")
c009 = next(r for r in results_mem2 if r.customer_id == "C009")
check("③ C001 被修正为 S001(人工反馈生效)", c001.sales_id == "S001")
check("③ C001 match_reason 含强记忆溯源'命中历史强记忆(S001'",
      "命中历史强记忆(S001" in c001.match_reason)
check("③ C003 确认场景维持 S002 且含记忆溯源",
      c003.sales_id == "S002" and "命中历史强记忆(S002" in c003.match_reason)
# 高相似客户(S001 强记忆的 query 与 C009 智能制造客户高度相似)受强记忆影响,
# 且 match_reason 带"命中历史强记忆"溯源 —— 符合任务 ③"被修正客户(或高相似客户)
# 的分配结果受 strong 记忆影响(推荐就是修正值)"。
check("③ 高相似智能制客户 C009 受强记忆影响 → S001 且含溯源",
      c009.sales_id == "S001" and "命中历史强记忆(S001" in c009.match_reason)
# 不相似客户(C005 新能源 / C007 医药)不被记忆干扰, 维持基线分配
check("③ 不相似客户不受影响(与基线一致)",
      {r.customer_id: r.sales_id for r in results_mem2 if r.customer_id in ("C005", "C007")}
      == {"C005": "S002", "C007": "S004"})

# ============================================================
# ⑤ 补充: memory=None 等价原 assign_leads; 记忆库异常不崩溃
# ============================================================
print("\n=== ⑤ 补充健壮性 ===")
results_none = la.assign_leads_with_memory(unassigned, sales_list, experiences, memory=None)
check("⑤ memory=None 完全等价原 assign_leads",
      [(r.customer_id, r.sales_id) for r in results_none]
      == [(r.customer_id, r.sales_id) for r in results_plain])

# 模拟 agent_memory 导入失败: 用一个缺接口的假模块
class FakeMemory:
    pass

results_fake = la.assign_leads_with_memory(
    unassigned, sales_list, experiences, memory=FakeMemory(),
)
check("⑤ 假 memory 对象降级无记忆不崩溃", len(results_fake) == 5)

fb_none = la.submit_feedback("C999", "S001", note="x", memory=FakeMemory())
check("⑤ memory 不可用时 submit_feedback 返回 None 不崩溃", fb_none is None)

print("\n========================================")
print(f"结果: PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("全部自检通过")