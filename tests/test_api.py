# -*- coding: utf-8 -*-
"""HTTP API(api.py) pytest 单测: TestClient 覆盖 4 个端点。

覆盖:
- GET  /health                      -> 200 {"status": "ok"};
- POST /pipeline/run                -> 200 summary + 分析结果落库(可查回);
- GET  /history/{customer_id}       -> 200(有记录) / 404(无记录);
- POST /feedback                    -> 200(强记忆写入) / 422(缺字段)。

隔离: conftest 临时 DB —— /pipeline/run 落库与 /feedback 记忆都写入 tmp 库,
不污染项目 data/sales_agent.db。全程 mock 模式, 无真实网络/LLM/钉钉。
"""

import pytest

from modules import agent_memory


@pytest.fixture()
def client(isolated_env):
    """FastAPI TestClient(懒导入 api 模块, 保证 sys.path 就绪)。"""
    from fastapi.testclient import TestClient
    import api
    return TestClient(api.app)


@pytest.fixture()
def auth_headers(client):
    """登录种子管理员, 返回带 Bearer token 的请求头。

    业务接口现已统一要求鉴权, 测试用真实登录流程拿到 token。
    """
    resp = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert resp.status_code == 200
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    """健康检查端点。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_pipeline_run_requires_auth(client):
    """业务接口未登录时返回 401。"""
    resp = client.post("/pipeline/run")
    assert resp.status_code == 401
    assert "登录" in resp.json()["detail"]


def test_pipeline_run_and_history_persist(client, auth_headers):
    """完整流水线: 200 summary + 结果已落库(history 可查回)。"""
    resp = client.post("/pipeline/run", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["customer_count"] == 9
    assert body["analyzed_count"] == 9
    assert body["assignment_count"] == 5
    assert body["saved_records"] == 9          # 每个客户的分析结果已落库
    assert body["intention_stats"]  # {"高":..,"中":..,"低":..}
    # 落库后可查回历史
    history = client.get("/history/C001", headers=auth_headers)
    assert history.status_code == 200
    records = history.json()["records"]
    assert len(records) >= 1
    assert records[0]["customer_id"] == "C001"
    assert records[0]["result"]["intention_level"] in ("高", "中", "低")


def test_history_404_for_unknown_customer(client, auth_headers):
    """无历史记录的客户 → 404 + detail。"""
    resp = client.get("/history/C-NO-EXIST", headers=auth_headers)
    assert resp.status_code == 404
    assert "暂无画像分析历史记录" in resp.json()["detail"]


def test_feedback_200_create_strong_memory(client, auth_headers):
    """反馈端点: 升级/新建强记忆 → 200 + strong 条目。"""
    resp = client.post(
        "/feedback",
        json={"customer_id": "C001", "correct_sales_id": "S001", "note": "人工指定"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["source"] == "strong"
    assert entry["correct_sales_id"] == "S001"
    assert entry["customer_id"] == "C001"
    assert entry["decision"] in ("correct", "confirm")
    # 确实写入了临时记忆库
    strong = [e for e in agent_memory.list_memories(limit=50)
              if e.source == "strong" and e.customer_id == "C001"]
    assert len(strong) == 1


def test_feedback_422_missing_field(client, auth_headers):
    """反馈端点: 缺字段(correct_sales_id 缺失)→ 422 校验错误。"""
    resp = client.post("/feedback", json={"customer_id": "C001"}, headers=auth_headers)
    assert resp.status_code == 422


def test_sync_sales_open_ids(client, auth_headers, monkeypatch):
    """测试通过手机号批量同步飞书 open_id 接口（不污染真实 mock_sales.json）。"""
    from modules import data_loader
    from modules import feishu_app_notifier
    # 用 mock 反查结果 + 拦截落盘，避免测试写坏 data/mock_sales.json
    monkeypatch.setattr(feishu_app_notifier, "get_open_id_by_mobile", lambda mobile: f"ou_mock_{mobile}")
    saved = []
    monkeypatch.setattr(data_loader, "save_sales", lambda lst: saved.append(list(lst)))
    resp = client.post("/sales/sync-open-ids", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "synced_count" in data
    assert "details" in data
    assert data["synced_count"] >= 4
    # 确认真实数据文件未被改动（拦截了 save_sales）
    assert saved != []