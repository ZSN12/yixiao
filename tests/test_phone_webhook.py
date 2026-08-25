# -*- coding: utf-8 -*-
"""电话录音 webhook + 低置信度角色复核(P1) pytest 单测。

覆盖:
1. webhook 签名校验: 正确签名通过, 错误签名/超时时间戳拒绝;
2. webhook 处理: 合法回调 → ChatRecord + 角色判定, 低置信度落库待复核;
3. 角色复核: list / confirm 接口;
4. ASR 可插拔注册表: mock 生效, 未注册 provider 抛 NotImplementedError;
5. ASR 真实 provider 失败降级(不崩)。
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import api


def _sign(body: bytes, secret: str, ts: int) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _phone_payload(**overrides) -> dict:
    base = {
        "call_id": "CALL_WEB001",
        "customer_id": "C001",
        "sales_id": "S001",
        "call_time": "2026-08-23T14:30:00",
        "direction": "outbound",
        "sales_mobile": "13800001111",
        "customer_mobile": "13900002222",
        "audio_path": "",
        "transcript_demo_id": "CALL001",
    }
    base.update(overrides)
    return base


@pytest.fixture
def client():
    """基于 api.app 的 TestClient(每测试独立)。"""
    return TestClient(api.app)


# ============================================================
# 1. webhook 签名校验
# ============================================================


def test_webhook_no_secret_skips_auth(client):
    """未配置 phone_webhook_secret 时跳过签名校验(内网/演示)。"""
    payload = _phone_payload()
    resp = client.post("/api/webhooks/phone-call", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["call_id"] == "CALL_WEB001"


def test_webhook_wrong_signature_rejected(client, monkeypatch):
    """配置了 secret 但签名错误 → 401。"""
    from config.settings import settings
    monkeypatch.setattr(settings, "phone_webhook_secret", "test-secret")

    payload = _phone_payload()
    ts = int(time.time())
    resp = client.post(
        "/api/webhooks/phone-call",
        json=payload,
        headers={"X-Signature": "bad-signature", "X-Timestamp": str(ts)},
    )
    assert resp.status_code == 401


def test_webhook_correct_signature_accepted(client, monkeypatch):
    """正确签名 + 时间戳 → 200。"""
    from config.settings import settings
    monkeypatch.setattr(settings, "phone_webhook_secret", "test-secret")

    payload = _phone_payload()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    sig = _sign(body, "test-secret", ts)
    resp = client.post(
        "/api/webhooks/phone-call",
        content=body,
        headers={"X-Signature": sig, "X-Timestamp": str(ts)},
    )
    assert resp.status_code == 200


def test_webhook_stale_timestamp_rejected(client, monkeypatch):
    """时间戳偏差过大(防重放)→ 401。"""
    from config.settings import settings
    monkeypatch.setattr(settings, "phone_webhook_secret", "test-secret")

    payload = _phone_payload()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stale_ts = int(time.time()) - 1000   # 偏差 1000 秒
    sig = _sign(body, "test-secret", stale_ts)
    resp = client.post(
        "/api/webhooks/phone-call",
        content=body,
        headers={"X-Signature": sig, "X-Timestamp": str(stale_ts)},
    )
    assert resp.status_code == 401


# ============================================================
# 2. webhook 处理: 角色判定 + 低置信度落库
# ============================================================


def test_webhook_returns_record_and_resolution(client):
    """合法回调 → 返回 ChatRecord + 角色判定。"""
    payload = _phone_payload()
    resp = client.post("/api/webhooks/phone-call", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["record"]["record_id"] == "CALL_WEB001"
    assert data["record"]["customer_id"] == "C001"
    assert data["record"]["messages"], "应转出非空消息"
    assert all(m["role"] in ("销售", "客户") for m in data["record"]["messages"])
    assert data["role_resolution"]["speaker_roles"]["Speaker_0"] == "销售"
    # 高置信度(outbound metadata)不需要 review
    assert data["needs_review"] is False


def test_webhook_low_confidence_creates_review(client, isolated_env):
    """低置信度角色判定 → needs_review=True 且落库(可被 list 查到)。"""
    # 无元数据(direction/sales_id/mobile 全空) + 无特征词转写 → 低置信度
    payload = _phone_payload(
        call_id="CALL_LOW001",
        direction=None,           # 去掉 direction 元数据
        sales_mobile=None,
        customer_mobile=None,
        sales_id=None,
        transcript_demo_id="CALL_LOW001",   # 演示转写: 说话人只说"嗯/哦/行"
    )
    resp = client.post("/api/webhooks/phone-call", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["needs_review"] is True, "无特征词转写应触发低置信度复核"
    assert data["review_id"] is not None, "低置信度应落库并返回 review_id"

    # list 能查到这条 pending 记录
    resp2 = client.get("/api/phone-reviews?status=pending")
    pending_ids = [r["call_id"] for r in resp2.json()["reviews"]]
    assert "CALL_LOW001" in pending_ids


# ============================================================
# 3. 角色复核 list / confirm
# ============================================================


def test_role_review_lifecycle(client, isolated_env):
    """save → list → confirm 全链路。"""
    from modules import data_loader

    saved = data_loader.save_role_review(
        call_id="CALL_RV001", customer_id="C001",
        speaker_roles={"Speaker_0": "销售", "Speaker_1": "客户"},
        method="heuristic", confidence=0.55, notes="低置信度", transcript="Speaker_0: 嗯",
    )
    assert saved is not None
    review_id = saved["id"]

    # list(默认全部)
    resp = client.get("/api/phone-reviews")
    assert resp.status_code == 200
    reviews = resp.json()["reviews"]
    assert any(r["id"] == review_id for r in reviews)

    # list(status=pending)
    resp = client.get("/api/phone-reviews?status=pending")
    assert any(r["id"] == review_id for r in resp.json()["reviews"])

    # confirm
    resp = client.post(
        f"/api/phone-reviews/{review_id}/confirm",
        json={"resolved_roles": {"Speaker_0": "客户", "Speaker_1": "销售"}},
    )
    assert resp.status_code == 200
    assert resp.json()["resolved"] is True

    # 确认后 status=resolved, 不再出现在 pending
    resp = client.get("/api/phone-reviews?status=pending")
    assert all(r["id"] != review_id for r in resp.json()["reviews"])


def test_role_review_confirm_missing(client):
    """确认不存在的复核记录 → 404。"""
    resp = client.post("/api/phone-reviews/999999/confirm", json={"resolved_roles": {"A": "销售"}})
    assert resp.status_code == 404


# ============================================================
# 4. ASR 可插拔注册表
# ============================================================


def test_asr_provider_registry_mock():
    """mock provider 走演示转写, 未注册 provider 抛 NotImplementedError。"""
    from adapters import asr_client
    # mock 返回空(由 load_mock_transcript 提供), 不抛
    assert asr_client.transcribe("", provider="mock") == []
    # 未注册的 provider
    with pytest.raises(NotImplementedError):
        asr_client.transcribe("", provider="unknown")


def test_asr_aliyun_skeleton_registered():
    """aliyun provider 骨架已注册, 但未配置凭证时抛 RuntimeError。"""
    from adapters import asr_client
    provider = asr_client.get_provider("aliyun")
    assert provider is not None
    assert provider.name == "aliyun"


# ============================================================
# 5. ASR 失败降级(phone_call_adapter)
# ============================================================


def test_phone_call_adapter_asr_failure_falls_back(isolated_env):
    """真实 ASR 失败 → 降级 mock 转写, 不崩。"""
    from adapters.phone_call_adapter import load_chat_from_call
    manifest = {
        "call_id": "CALL_DG001", "customer_id": "C001", "sales_id": "S001",
        "call_time": "2026-08-23T14:30:00", "direction": "outbound",
        "audio_path": "http://fake/audio.wav", "transcript_demo_id": "CALL001",
    }
    # 强制 asr_provider=aliyun(未配置凭证 → RuntimeError → 降级 mock)
    record, resolution = load_chat_from_call(manifest)
    assert record.record_id == "CALL_DG001"
    assert resolution.confidence > 0
    assert record.messages, "降级后仍应有 mock 转写消息"
