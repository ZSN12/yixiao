# -*- coding: utf-8 -*-
"""电话录音适配器(phone_call_adapter) pytest 单测。

覆盖方案验收标准:
1. mock manifest → ChatRecord: messages 非空, role 仅 销售/客户;
2. 客户说预算、销售说报价: 角色正确分离;
3. Tier-1 元数据绑定(outbound): Speaker_0=销售;
4. 角色对调/低置信度: confidence < 0.7 或 notes 含 review;
5. 接入 profile_analyzer: 客户预算 → 价格信号; 销售提预算 → 不计入;
6. 确定性: 同 manifest 跑两次, RoleResolution 一致。
"""

import json
from pathlib import Path

import pytest

from adapters.asr_client import AsrSegment, load_mock_transcript
from adapters.phone_call_adapter import (
    load_chat_from_call,
    load_chats_from_manifest_file,
    run_phone_call_demo,
    segments_to_messages,
)
from adapters.speaker_role_resolver import RoleResolution, resolve

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "data" / "real" / "phone_call_manifest.json"


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)[0]


def _mock_segments() -> list:
    """构造一段 outbound 通话转写: Speaker_0 先开口(销售), Speaker_1 是客户。"""
    return [
        AsrSegment(speaker="Speaker_0", text="王总您好，我是易销科技的小张，方案发您邮箱了。", start_ms=0, end_ms=5000),
        AsrSegment(speaker="Speaker_1", text="收到了，我们预算大概500万，价格能谈下来就尽快立项。", start_ms=6000, end_ms=12000),
        AsrSegment(speaker="Speaker_0", text="好的，报价我明天发您。", start_ms=13000, end_ms=16000),
    ]


# ============================================================
# 1. mock manifest → ChatRecord
# ============================================================


def test_manifest_to_chat_record():
    """manifest → ChatRecord: messages 非空, role 仅 销售/客户。"""
    manifest = _load_manifest()
    record, resolution = load_chat_from_call(manifest)
    assert record.record_id == "CALL001"
    assert record.customer_id == "C001"
    assert record.sales_id == "S001"
    assert record.chat_time == "2026-08-23"   # call_time 截取前 10 位
    assert len(record.messages) >= 2
    assert all(m.role in ("销售", "客户") for m in record.messages)
    assert isinstance(resolution, RoleResolution)


def test_load_chats_from_manifest_file():
    """批量加载 manifest 文件。"""
    results = load_chats_from_manifest_file(str(MANIFEST_PATH))
    assert len(results) >= 1
    record, resolution = results[0]
    assert record.record_id == "CALL001"
    assert resolution.confidence > 0


# ============================================================
# 2. 客户说预算、销售说报价
# ============================================================


def test_customer_budget_vs_sales_quote():
    """客户报预算的 content 在「客户」消息, 销售报价在「销售」消息。"""
    manifest = _load_manifest()
    record, _ = load_chat_from_call(manifest)
    customer_msgs = [m for m in record.messages if m.role == "客户"]
    sales_msgs = [m for m in record.messages if m.role == "销售"]
    assert any("500万" in m.content for m in customer_msgs)   # 客户报预算
    assert all("报价" not in m.content or "500万" not in m.content for m in customer_msgs)
    assert sales_msgs   # 销售说话存在


# ============================================================
# 3. Tier-1 元数据绑定(outbound)
# ============================================================


def test_tier1_outbound_metadata():
    """outbound 通话 + 主被叫 → Speaker_0(先开口)=销售, confidence >= 0.95。"""
    manifest = {
        "call_id": "CALL_X", "customer_id": "C001", "sales_id": "S001",
        "call_time": "2026-08-23T14:30:00", "direction": "outbound",
        "sales_mobile": "13800001111", "customer_mobile": "13900002222",
    }
    resolution = resolve(_mock_segments(), manifest)
    assert resolution.speaker_roles["Speaker_0"] == "销售"
    assert resolution.speaker_roles["Speaker_1"] == "客户"
    assert resolution.method == "metadata"
    assert resolution.confidence >= 0.95


def test_tier1_inbound_reversed():
    """inbound 通话 → 主叫(客户)先开口, Speaker_0=客户。"""
    manifest = {
        "call_id": "CALL_Y", "customer_id": "C001",
        "call_time": "2026-08-23T14:30:00", "direction": "inbound",
        "sales_mobile": "13800001111", "customer_mobile": "13900002222",
    }
    resolution = resolve(_mock_segments(), manifest)
    assert resolution.speaker_roles["Speaker_0"] == "客户"
    assert resolution.speaker_roles["Speaker_1"] == "销售"


# ============================================================
# 4. 角色对调/低置信度
# ============================================================


def test_low_confidence_marks_review():
    """无明显特征词的转写 → 低置信度, 标记 review(不随机)。"""
    segments = [
        AsrSegment(speaker="Speaker_0", text="嗯。", start_ms=0, end_ms=1000),
        AsrSegment(speaker="Speaker_1", text="哦。", start_ms=1500, end_ms=2500),
    ]
    manifest = {"call_id": "CALL_Z", "customer_id": "C001", "call_time": "2026-08-23"}
    resolution = resolve(segments, manifest)
    # 无元数据、无特征词 → heuristic 低置信度, needs_review 为 True
    assert resolution.needs_review() is True or resolution.confidence < 0.7


def test_role_resolution_deterministic():
    """同 manifest 跑两次, RoleResolution 一致(确定性)。"""
    manifest = _load_manifest()
    _, r1 = load_chat_from_call(manifest)
    _, r2 = load_chat_from_call(manifest)
    assert r1.speaker_roles == r2.speaker_roles
    assert r1.method == r2.method
    assert r1.confidence == r2.confidence


# ============================================================
# 5. 接入 profile_analyzer(说话者区分 + 价格衰减)
# ============================================================


def test_profile_analyzer_consumes_phone_call(isolated_env):
    """电话录音 → 画像: 客户预算进价格信号, 销售报价不进。"""
    from modules import data_loader
    from modules import profile_analyzer as pa

    results = load_chats_from_manifest_file(str(MANIFEST_PATH))
    chat_records = [rec for rec, _ in results]
    chat_map = data_loader.build_chat_map(chat_records)
    customers = data_loader.load_customers()
    c001 = next(c for c in customers if c.customer_id == "C001")

    # 价格信号提取: 只认客户说的话
    signals = pa._extract_price_signal(chat_map.get("C001", []))
    assert signals, "客户报的 500万 预算应被提取为价格信号"
    assert any("500万" in s["amount"] for s in signals)

    # 画像: 预算范围标注最新有效(2 天前, 7 天内)
    result = pa._fallback_to_rules(c001, chat_map.get("C001", []))
    assert "500万" in result.customer_profile
    assert "最新有效" in result.customer_profile or "7天内有效" in result.customer_profile


def test_sales_only_budget_not_counted(isolated_env):
    """仅销售提「预算500万」→ 不产生客户价格信号(说话者区分核心)。"""
    from modules import profile_analyzer as pa
    segments = [
        AsrSegment(speaker="Speaker_0", text="我们预算方案大概500万，报价发您了。", start_ms=0, end_ms=3000),
    ]
    roles = {"Speaker_0": "销售"}
    messages = segments_to_messages(segments, roles)
    assert len(messages) == 1
    assert messages[0].role == "销售"
    # 把这条销售消息包装成 ChatRecord, 验证不提取价格信号
    from modules.data_loader import ChatRecord
    rec = ChatRecord(
        record_id="SALES_ONLY", customer_id="C001", chat_time="2026-08-23", messages=messages,
    )
    signals = pa._extract_price_signal([rec])
    assert signals == [], "销售提预算不应计入客户价格信号"


# ============================================================
# 6. 端到端 demo
# ============================================================


def test_run_phone_call_demo():
    """run_phone_call_demo 输出通话数、角色判定、意向分层。"""
    summary = run_phone_call_demo(str(MANIFEST_PATH))
    assert summary["call_count"] >= 1
    assert summary["analyzed"] >= 1
    assert isinstance(summary["intention_stats"], dict)
    assert len(summary["role_resolutions"]) >= 1
    rr = summary["role_resolutions"][0]
    assert "method" in rr and "confidence" in rr


def test_mock_transcript_loading():
    """mock ASR 转写文件加载正常, 含 2 个说话人。"""
    segments = load_mock_transcript("CALL001")
    assert len(segments) >= 2
    speakers = {s.speaker for s in segments}
    assert speakers == {"Speaker_0", "Speaker_1"}
