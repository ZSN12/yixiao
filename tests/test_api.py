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


def test_health(client):
    """健康检查端点。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_pipeline_run_and_history_persist(client):
    """完整流水线: 200 summary + 结果已落库(history 可查回)。"""
    resp = client.post("/pipeline/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["customer_count"] == 9
    assert body["analyzed_count"] == 9
    assert body["assignment_count"] == 5
    assert body["saved_records"] == 9          # 每个客户的分析结果已落库
    assert body["intention_stats"]  # {"高":..,"中":..,"低":..}
    # 落库后可查回历史
    history = client.get("/history/C001")
    assert history.status_code == 200
    records = history.json()["records"]
    assert len(records) >= 1
    assert records[0]["customer_id"] == "C001"
    assert records[0]["result"]["intention_level"] in ("高", "中", "低")


def test_history_404_for_unknown_customer(client):
    """无历史记录的客户 → 404 + detail。"""
    resp = client.get("/history/C-NO-EXIST")
    assert resp.status_code == 404
    assert "暂无画像分析历史记录" in resp.json()["detail"]


def test_feedback_200_create_strong_memory(client):
    """反馈端点: 升级/新建强记忆 → 200 + strong 条目。"""
    resp = client.post(
        "/feedback",
        json={"customer_id": "C001", "correct_sales_id": "S001", "note": "人工指定"},
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


def test_feedback_422_missing_field(client):
    """反馈端点: 缺字段(correct_sales_id 缺失)→ 422 校验错误。"""
    resp = client.post("/feedback", json={"customer_id": "C001"})
    assert resp.status_code == 422