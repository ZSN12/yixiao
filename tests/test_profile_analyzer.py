# -*- coding: utf-8 -*-
"""画像分析器(profile_analyzer) pytest 单测: 规则引擎确定性 + 枚举合法性 + LLM 失败降级。

覆盖:
- 同输入同输出(规则引擎确定性, 可复现);
- intention_level / churn_risk 是合法枚举(高/中/低);
- core_demands 落在 3-5 条契约范围;
- LLM 失败(注入假客户端抛异常)→ 自动降级规则引擎, 结果与规则引擎一致。
"""

import pytest

from config.settings import settings
from modules.data_loader import ChatMessage, ChatRecord, Customer
from modules import profile_analyzer

# 合法枚举
_VALID_LEVELS = ("高", "中", "低")


def _make_customer(customer_id: str = "U1") -> Customer:
    """构造单测客户(智能制造/苏州, 与 mock 风格一致)。"""
    return Customer(
        customer_id=customer_id,
        customer_name="单测智能装备有限公司",
        industry="智能制造",
        city="苏州",
        scale="中型",
        owner_sales_id=None,
        create_time="2024-01-01",
    )


def _make_chat_records() -> list:
    """构造含正/负/流失三类信号的聊天记录。"""
    return [
        ChatRecord(
            record_id="R1", customer_id="U1", chat_time="2024-01-02",
            messages=[
                ChatMessage(role="客户", content="王总您好, 我们有500万预算, 需要尽快采购, 还在和领导商量"),
                ChatMessage(role="客户", content="之前看过竞品方案, 你们价格太贵了, 还有和我们预算紧张的情况"),
            ],
        ),
        ChatRecord(
            record_id="R2", customer_id="U1", chat_time="2024-01-05",
            messages=[
                ChatMessage(role="销售", content="好的, 我们尽快出方案和报价"),
            ],
        ),
    ]


def test_rule_engine_deterministic(isolated_env):
    """同输入同输出: 规则引擎两次分析结果完全一致(确定性可复现)。"""
    customer = _make_customer()
    records = _make_chat_records()
    r1 = profile_analyzer.analyze_customer(customer, records)
    r2 = profile_analyzer.analyze_customer(customer, records)
    assert r1.model_dump() == r2.model_dump()
    # 直接调规则引擎仍是同一结果(双引擎契约: 未配置 LLM 时走规则)
    r3 = profile_analyzer._fallback_to_rules(customer, records)
    assert r1.model_dump() == r3.model_dump()


def test_legal_enums_and_demands(isolated_env):
    """意向/流失为合法枚举, core_demands 3-5 条, 画像文本非空。"""
    customer = _make_customer()
    records = _make_chat_records()
    result = profile_analyzer.analyze_customer(customer, records)
    assert result.intention_level in _VALID_LEVELS
    assert result.churn_risk in _VALID_LEVELS
    assert 3 <= len(result.core_demands) <= 5
    assert result.core_demands == list(dict.fromkeys(result.core_demands))  # 去重
    assert result.customer_profile.strip()
    assert result.intention_reason and result.churn_reason
    assert result.follow_up_suggestion.strip()


def test_mock_customers_all_legal(isolated_env):
    """对全部 mock 客户批量分析: 每条结果枚举合法、需求数合规、画像含基础信息。"""
    customers = profile_analyzer._fallback_to_rules  # noqa: F841 —— 防误用
    from modules.data_loader import load_all, load_chat_records, load_customers, build_chat_map
    customers = load_customers()
    records = load_chat_records()
    chat_map = build_chat_map(records)
    results = profile_analyzer.analyze_customers_batch(customers, chat_map)
    assert len(results) == len(customers)
    for cid, result in results.items():
        assert result.intention_level in _VALID_LEVELS
        assert result.churn_risk in _VALID_LEVELS
        assert 3 <= len(result.core_demands) <= 5
        assert cid in result.customer_profile or "企业" in result.customer_profile


def test_llm_failure_falls_back_to_rules(isolated_env, monkeypatch):
    """LLM 引擎失败(注入模拟异常)→ 自动降级规则引擎, 结果与规则引擎一致且不抛。"""
    customer = _make_customer()
    records = _make_chat_records()
    expected = profile_analyzer._fallback_to_rules(customer, records)

    # 模拟"配置了 LLM 但调用必然失败"
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "llm_api_key", "fake-key")

    def _boom(_customer, _records):
        raise RuntimeError("模拟 LLM 网络/超时/格式失败")

    monkeypatch.setattr(profile_analyzer, "_analyze_with_llm", _boom)
    monkeypatch.setattr(profile_analyzer, "_llm_enabled", lambda: True)

    result = profile_analyzer.analyze_customer(customer, records)
    assert result.model_dump() == expected.model_dump()   # 降级结果与规则引擎一致


def test_llm_enabled_but_openai_missing_never_raises(isolated_env, monkeypatch):
    """即使走 LLM 分支(openai 未安装/异常)也不向上抛: analyze_customers_batch 全量出结果。"""
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "llm_api_key", "fake-key")

    # 模拟 openai 不可用: _analyze_with_llm 内部 from openai import 抛 ImportError
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("模拟 openai 未安装")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    customer = _make_customer()
    results = profile_analyzer.analyze_customers_batch([customer], {customer.customer_id: _make_chat_records()})
    assert customer.customer_id in results
    assert results[customer.customer_id].intention_level in _VALID_LEVELS


def test_intention_penalized_by_negative_and_churn(isolated_env):
    """损失信号同时压降意向: 全是异议/流失信号的客户意向应为低。"""
    customer = _make_customer("U2")
    records = [
        ChatRecord(
            record_id="R3", customer_id="U2", chat_time="2024-02-01",
            messages=[
                ChatMessage(role="客户", content="太贵了, 预算不足, 已经看了竞品, 暂时搁置吧"),
            ],
        ),
    ]
    result = profile_analyzer.analyze_customer(customer, records)
    assert result.intention_level == "低"
    assert result.churn_risk == "高"