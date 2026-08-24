# -*- coding: utf-8 -*-
"""pytest 共享夹具(conftest): 统一临时 DB 隔离 + mock 模式固定 + 自检脚本排除。

设计说明(对应任务要求):
1. 临时 DB 隔离: autouse fixture `isolated_env` 把 settings.db_path 指向 pytest
   自带的 tmp_path 临时目录(绝对路径), 每次测试独立一个 SQLite 文件, 并初始化
   analysis_history(data_loader) 与 memory_store(agent_memory) 两张表;
   测试结束由 tmp_path / monkeypatch 自动清理 —— 保证测试互不污染,
   绝不触碰项目 data/sales_agent.db。
2. mock 模式固定: 同一 fixture 强制 mock_mode=True、钉钉 webhook/secret 为空、
   LLM key 为空、embedding 未配置 —— 全部测试在 不依赖真实 LLM/网络/钉钉
   的条件下运行(规则引擎 + 本地相似度 + 推送静默跳过)。
3. collect_ignore: 项目根 tests/ 下两个历史"自检脚本"(test_*_selfcheck.py,
   独立 `python tests/xxx.py` 运行, 模块级代码含 os.chdir/sys.exit, 不适合
   pytest 收集)通过 conftest 排除, 文件保留不删; pytest 只收集本目录新增的
   test_*.py 单测文件。
"""

import sys
from pathlib import Path

# 项目根加入 sys.path: 所有测试统一 import modules/config/orchestrator/api
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from config.settings import settings

# 历史自检脚本(非 pytest 测试): 排除收集, 保留文件不动
collect_ignore = [
    "test_dingtalk_notifier_selfcheck.py",
    "test_lead_assigner_memory_selfcheck.py",
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """每个测试独立的隔离环境: 临时 DB + mock 模式, 结束后自动还原清理。

    Returns:
        Path: pytest 临时目录(tmp_path), 内含 test_sales_agent.db。
    """
    from modules import agent_memory, data_loader

    tmp_db = str(tmp_path / "test_sales_agent.db")
    # 数据库与外部依赖全部指向隔离/空配置(monkeypatch 测试结束自动还原)
    monkeypatch.setattr(settings, "db_path", tmp_db)
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "dingtalk_webhook_url", "")
    monkeypatch.setattr(settings, "dingtalk_secret", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_api_base", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "kimi_api_key", "")   # 测试走规则引擎, 不触发真实 Kimi 调用
    monkeypatch.setattr(settings, "embedding_api_base", "")
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "embedding_model", "")
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    monkeypatch.setattr(settings, "feishu_webapp_url", "")
    monkeypatch.setattr(settings, "sales_mobile_map", "")
    # 初始化临时 SQLite: analysis_history(data_loader) + memory_store(agent_memory)
    data_loader.init_db()
    agent_memory.init_memory_db()
    return tmp_path