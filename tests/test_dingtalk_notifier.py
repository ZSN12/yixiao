# -*- coding: utf-8 -*-
"""钉钉推送器(dingtalk_notifier) pytest 单测: mock 跳过 / 加签向量 / 失败路径 / 文案构造。

覆盖:
- webhook 未配置 → send_daily_report / send_assignment_batch 返回 False 且不抛异常;
- 加签算法固定输入固定输出(内嵌已知正确签名的固定样例, 独立于实现核对);
- 假端口(127.0.0.1:9)失败路径 → 返回 False 不抛;
- 日报/分配明细文案构造(补 0 / 截断 / 字段占位)。
"""

import base64
import hashlib
import hmac
import urllib.parse

from config.settings import settings
from modules import dingtalk_notifier as dn
from modules.lead_assigner import AssignmentResult

# 固定加签样例(独立计算结果, 已核对, 只读比对):
#   secret="SECabc", timestamp=1234567890
#   string_to_sign = "1234567890\nSECabc"
#   sign = quote_plus(base64(hmac_sha256(key=SECabc, msg=string_to_sign)))
KNOWN_SIGN = "BgiIAARG0zIa5lDrfiS7I0fwY3PdIm8sGO4DKEOupWI%3D"


def test_webhook_unconfigured_returns_false_no_raise(isolated_env):
    """webhook 未配置(mock 模式默认): 返回 False 且不抛异常。"""
    assert not settings.dingtalk_webhook_url
    assert dn.send_daily_report("测试日报正文\n- 客户数: 10") is False
    assert dn.send_assignment_batch([]) is False
    # 不抛异常(send_assignment_batch 带非空列表同样跳过)
    dn.send_assignment_batch([_mk_assignment()])


def test_sign_fixed_vector(isolated_env):
    """加签算法: 固定输入(secret/timestamp)→ 固定输出(与已知正确签名一致)。"""
    assert dn._generate_sign("SECabc", 1234567890) == KNOWN_SIGN


def test_signed_url_assembly(isolated_env):
    """签名 URL 拼装: &timestamp / &sign / 无 secret 不加签 / 无 ? 用 ? 拼接。"""
    SEC, TS = "SECabc", 1234567890
    url = dn._build_signed_url("https://oapi.dingtalk.com/robot/send?access_token=abc", SEC, TS)
    assert f"&timestamp={TS}" in url
    assert f"&sign={KNOWN_SIGN}" in url          # 签名值即固定样例(已 URL 编码)
    # 无 secret 不加签
    url2 = dn._build_signed_url("https://oapi.dingtalk.com/robot/send?access_token=abc", "", TS)
    assert url2 == "https://oapi.dingtalk.com/robot/send?access_token=abc"
    # 无 ? 的 webhook 用 ? 拼接
    url3 = dn._build_signed_url("https://example.com/hook", SEC, TS)
    assert url3.startswith("https://example.com/hook?timestamp=")


def _mk_assignment(**overrides) -> AssignmentResult:
    """构造分配结果(测试用)。"""
    fields = dict(
        customer_id="C001", customer_name="苏州精工制造", sales_id="S001",
        sales_name="张伟", match_reason="行业匹配(智能制造) + RAG语义匹配(苏州某工厂)·成单 + 负载均衡(当前负载2), 推荐S001张伟",
        rag_score=0.9, rule_matched=True, needs_human=False,
    )
    fields.update(overrides)
    return AssignmentResult(**fields)


def test_fail_path_fake_port_returns_false(isolated_env, monkeypatch):
    """假端口(快速失败)路径: 网络失败 → 返回 False, 不抛异常。"""
    monkeypatch.setattr(settings, "dingtalk_webhook_url", "http://127.0.0.1:9/fake_hook")
    monkeypatch.setattr(settings, "dingtalk_secret", "SECabc")
    assert dn.send_daily_report("测试失败路径日报") is False
    assert dn.send_assignment_batch([_mk_assignment()]) is False
    # 不抛异常
    dn.send_daily_report("异常测试")
    dn.send_assignment_batch([_mk_assignment()])


def test_daily_report_text_build(isolated_env):
    """日报 markdown 文案: 分层统计/分配摘要/更新时间, 缺失等级键补 0。"""
    report = dn.build_daily_report_text(
        customer_count=10,
        profile_stats={"意向": {"高": 3, "中": 5, "低": 2}, "流失": {"高": 1, "中": 2, "低": 7}},
        assignment_summary={"recommend": "张三(5单)/李四(3单)", "needs_human": "2家待人工分配"},
        updated_at="2024-08-19 08:30:00",
    )
    assert "**10**" in report
    assert "意向等级: 高 **3** / 中 **5** / 低 **2**" in report
    assert "流失风险: 高 **1** / 中 **2** / 低 **7**" in report
    assert "推荐销售: 张三(5单)/李四(3单)" in report
    assert "2024-08-19 08:30:00" in report
    # 缺失等级键补 0
    report2 = dn.build_daily_report_text(5, {"意向": {"高": 2}}, {}, "2024-08-19 09:00:00")
    assert "高 **2** / 中 **0** / 低 **0**" in report2
    assert "流失风险: 高 **0** / 中 **0** / 低 **0**" in report2


def test_assignment_table_text_build(isolated_env):
    """分配明细表格: 表头/分隔行/字段转义与截断/空列表占位。"""
    table = dn.build_assignment_table_text([
        _mk_assignment(),
        _mk_assignment(
            customer_id="C002", customer_name="杭州智联|技术", sales_id="S002",
            sales_name="李四",
            match_reason="城市匹配(杭州) + RAG语义匹配(相似经验: 杭州某软件公司那个很长的一段经验描述需要被截断处理以验证截断逻辑是否正常工作)·成单 + 负载均衡(当前负载1)",
        ),
        _mk_assignment(
            customer_id="C003", customer_name="宁波海纳", sales_id="admin",
            sales_name="默认管理员", match_reason="无匹配销售，待人工二次分配",
            rag_score=0.0, rule_matched=False, needs_human=True,
        ),
    ])
    assert "| 客户 | 推荐销售 | 理由 |" in table
    assert "| --- | --- | --- |" in table
    assert "张伟" in table and "李四" in table and "默认管理员" in table
    assert "杭州智联\\|技术" in table          # | 被转义, 不破坏表格
    assert "…" in table                        # 超长理由被截断
    assert dn.build_assignment_table_text([]) == "本期无分配明细"