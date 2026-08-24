# -*- coding: utf-8 -*-
"""dingtalk_notifier 自检脚本: 覆盖任务要求的所有检查点。"""
import sys
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from modules import dingtalk_notifier as dn
from modules.lead_assigner import AssignmentResult

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


# ---------- 检查点 1: mock 模式(未配置 webhook) ----------
print("\n=== 1. mock 模式: webhook 未配置 ===")
# 确保 settings 里 webhook 为空(mock 模式默认)
assert not dn.settings.dingtalk_webhook_url, "测试前提: webhook 必须为空"

ret = dn.send_daily_report("测试日报正文\n- 客户数: 10")
check("mock send_daily_report 返回 False", ret is False)
ret = dn.send_assignment_batch([])
check("mock send_assignment_batch 返回 False", ret is False)
try:
    dn.send_daily_report("异常测试")
    dn.send_assignment_batch([])
    check("mock 模式不抛异常", True)
except Exception as exc:
    check("mock 模式不抛异常", False, f"抛异常: {exc}")

# ---------- 检查点 2: 加签算法(用钉钉官方示例值验证) ----------
print("\n=== 2. 加签 HMAC-SHA256 算法验证(钉钉官方示例) ===")
# 钉钉官方文档示例: secret=SECxxx, timestamp=时间戳, 用已知向量验证
# 官方示例:
#   secret = "SECabc" (示例), 这里用确定性向量自校验:
#   string_to_sign = "1234567890\nSECabc"
#   sign = quote_plus(base64(hmac_sha256("SECabc", "1234567890\nSECabc")))
import base64, hashlib, hmac, urllib.parse

SEC = "SECabc"
TS = 1234567890
expected = urllib.parse.quote_plus(
    base64.b64encode(
        hmac.new(SEC.encode(), f"{TS}\n{SEC}".encode(), hashlib.sha256).digest()
    )
)
actual = dn._generate_sign(SEC, TS)
check("_generate_sign 输出与独立实现一致", actual == expected, f"sign={actual}")

# URL 拼装检查
url = dn._build_signed_url("https://oapi.dingtalk.com/robot/send?access_token=abc", SEC, TS)
check("signed URL 含 &timestamp 与 &sign", "&timestamp=1234567890" in url and "&sign=" in url, url)
# 无 secret 时不加签
url2 = dn._build_signed_url("https://oapi.dingtalk.com/robot/send?access_token=abc", "", TS)
check("无 secret 不加签", url2 == "https://oapi.dingtalk.com/robot/send?access_token=abc", url2)
# 无 ? 的 webhook
url3 = dn._build_signed_url("https://example.com/hook", SEC, TS)
check("无 ? 的 webhook 用 ? 拼接", url3.startswith("https://example.com/hook?timestamp="), url3)

# ---------- 检查点 3: 假 webhook 快速失败端口(超时/失败路径) ----------
print("\n=== 3. 假 webhook(http://127.0.0.1:9/ 快速失败) ===")
# 临时改 settings 值(不走 .env)
from config import settings as cfg

old_webhook, old_secret = cfg.settings.dingtalk_webhook_url, cfg.settings.dingtalk_secret
cfg.settings.dingtalk_webhook_url = "http://127.0.0.1:9/fake_hook"
cfg.settings.dingtalk_secret = SEC

try:
    ret = dn.send_daily_report("测试失败路径日报")
    check("失败路径 send_daily_report 返回 False", ret is False)
    ret = dn.send_assignment_batch([
        AssignmentResult(
            customer_id="C001", customer_name="测试客户A", sales_id="s1",
            sales_name="张三", match_reason="行业匹配(智能制造) + RAG语义匹配", rag_score=0.9,
            rule_matched=True, needs_human=False,
        )
    ])
    check("失败路径 send_assignment_batch 返回 False", ret is False)
    try:
        dn.send_daily_report("异常测试")
        check("失败路径不抛异常", True)
    except Exception as exc:
        check("失败路径不抛异常", False, f"抛异常: {exc}")
finally:
    cfg.settings.dingtalk_webhook_url = old_webhook
    cfg.settings.dingtalk_secret = old_secret

# ---------- 检查点 4: 分配明细表格文本格式 ----------
print("\n=== 4. 分配明细表格格式(真实 AssignmentResult 列表) ===")
assignments = [
    AssignmentResult(
        customer_id="C001", customer_name="苏州精工制造", sales_id="s1", sales_name="张三",
        match_reason="行业匹配(智能制造) + RAG语义匹配(相似经验: 苏州某工厂成单) + 负载均衡(当前负载2), 推荐s1张三",
        rag_score=0.9234, rule_matched=True, needs_human=False,
    ),
    AssignmentResult(
        customer_id="C002", customer_name="杭州智联科技", sales_id="s2", sales_name="李四",
        match_reason="城市匹配(杭州) + RAG语义匹配(相似经验: 杭州某软件公司·成单) + 负载均衡(当前负载1), 推荐s2李四",
        rag_score=0.8712, rule_matched=True, needs_human=False,
    ),
    AssignmentResult(
        customer_id="C003", customer_name="宁波海纳", sales_id="admin", sales_name="默认管理员",
        match_reason="无匹配销售，待人工二次分配", rag_score=0.0, rule_matched=False, needs_human=True,
    ),
]
table = dn.build_assignment_table_text(assignments)
print(table)
check("表格含表头 '客户|推荐销售|理由'", "| 客户 | 推荐销售 | 理由 |" in table)
check("表格含分隔行", "| --- | --- | --- |" in table)
check("表格行数与明细数一致", table.count("\n") == 4 and table.splitlines()[2:5][2].count("|") == 4)
check("表格含客户名/销售名/理由", all(
    k in table for k in ["苏州精工制造", "杭州智联科技", "宁波海纳", "张三", "李四", "默认管理员"]
))
# 空列表
check("空列表返回占位", dn.build_assignment_table_text([]) == "本期无分配明细")

# ---------- 检查点 5: 日报文本构造 ----------
print("\n=== 5. 日报 markdown 文本构造 ===")
report = dn.build_daily_report_text(
    customer_count=10,
    profile_stats={"意向": {"高": 3, "中": 5, "低": 2}, "流失": {"高": 1, "中": 2, "低": 7}},
    assignment_summary={"recommend": "张三(5单)/李四(3单)", "needs_human": "2家待人工分配"},
    updated_at="2024-08-19 08:30:00",
)
print(report)
check("日报含客户总数", "**10**" in report)
check("日报含意向分层", "意向等级: 高 **3** / 中 **5** / 低 **2**" in report)
check("日报含流失分层", "流失风险: 高 **1** / 中 **2** / 低 **7**" in report)
check("日报含分配摘要", "推荐销售: 张三(5单)/李四(3单)" in report and "2家待人工分配" in report)
check("日报含更新时间", "2024-08-19 08:30:00" in report)
# 缺等级键补 0
report2 = dn.build_daily_report_text(5, {"意向": {"高": 2}}, {}, "2024-08-19 09:00:00")
check("缺失分层键补 0", "高 **2** / 中 **0** / 低 **0**" in report2 and "流失风险: 高 **0** / 中 **0** / 低 **0**" in report2)

print("\n========================================")
print(f"结果: PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("全部自检通过")
