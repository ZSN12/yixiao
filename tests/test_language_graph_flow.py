# -*- coding: utf-8 -*-
"""编排层(language_graph_flow) pytest 单测: 状态流转 + LangGraph 不可用降级同构。

覆盖:
- run_pipeline_graph 状态流转: analysis_results(9) / assignments(5) /
  push_reports(非空 dict) / summary / stats 齐备;
- langgraph 不可用时(LANGUAGE_GRAPH_AVAILABLE=False)run_pipeline_graph
  自动降级顺序直调, 输出与图模式同构(assignments 逐条一致);
- run_pipeline_sequential 直接调用同样产出完整状态。
"""

import pytest

from modules import data_loader
from orchestrator import language_graph_flow as lgf


def _inputs() -> tuple:
    """装配真实 mock 数据(与 main.py 一致)。"""
    customers, records, sales = data_loader.load_all()
    chat_map = data_loader.build_chat_map(records)
    experiences = data_loader.load_sales_experiences()
    return customers, chat_map, sales, experiences


def _assignment_map(assignments) -> dict:
    return {(a.customer_id, a.sales_id): round(a.rag_score, 6) for a in assignments}


def test_run_pipeline_graph_state_flow(isolated_env):
    """图模式状态流转: 分析 9 / 分配 5 / 推送报表非空 / summary 完整。"""
    state = lgf.run_pipeline_graph(*_inputs())
    assert state["analysis_results"], "分析结果非空"
    assert len(state["analysis_results"]) == 9
    assert len(state["assignments"]) == 5
    assert isinstance(state["push_reports"], dict) and state["push_reports"], "推送报表非空"
    # webhook 未配置(mock)→ 推送返回 False 但已被安全记录, 不抛
    assert state["push_reports"]["daily_report"] is False
    assert state["push_reports"]["assignment_batch"] is False
    assert state["push_reports"]["report_text"]
    assert state["summary"]["assignment_count"] == 5
    assert state["summary"]["customer_count"] == 9
    assert state["stats"]["意向"] and state["stats"]["流失"]
    assert state["meta"]["mock_mode"] is True


def test_sequential_fallback_output_isomorphic(isolated_env):
    """顺序直调与图模式输出同构: 分析/分配/push_reports/summary 关键字段一致。"""
    graph_state = lgf.run_pipeline_graph(*_inputs())
    seq_state = lgf.run_pipeline_sequential(*_inputs())

    # 关键通道齐备
    for key in ("analysis_results", "assignments", "push_reports", "summary", "stats", "errors", "meta"):
        assert key in seq_state
    assert len(seq_state["analysis_results"]) == len(graph_state["analysis_results"])
    assert len(seq_state["assignments"]) == len(graph_state["assignments"])
    # 分配结果逐条一致(同一模块、同一种子数据 → 确定性)
    assert _assignment_map(seq_state["assignments"]) == _assignment_map(graph_state["assignments"])
    assert seq_state["summary"]["customer_count"] == graph_state["summary"]["customer_count"]
    assert seq_state["summary"]["assignment_count"] == graph_state["summary"]["assignment_count"]
    # 顺序模式 meta 标记
    assert seq_state["meta"]["engine"] == "sequential-fallback"


def test_langgraph_unavailable_falls_back(isolated_env, monkeypatch):
    """langgraph 不可用(模拟导入失败)→ run_pipeline_graph 走顺序直调且不抛。"""
    monkeypatch.setattr(lgf, "LANGUAGE_GRAPH_AVAILABLE", False)
    state = lgf.run_pipeline_graph(*_inputs())
    assert len(state["analysis_results"]) == 9
    assert len(state["assignments"]) == 5
    assert state["push_reports"]
    assert state["summary"]["assignment_count"] == 5
    # 降级路径不阻塞(与顺序直调同构)
    assert state["meta"]["engine"] == "sequential-fallback"


def test_build_graph_raises_when_unavailable(isolated_env, monkeypatch):
    """build_pipeline_graph 在 langgraph 不可用时抛出 ImportError(由 run_pipeline_graph 捕获降级)。"""
    monkeypatch.setattr(lgf, "LANGUAGE_GRAPH_AVAILABLE", False)
    with pytest.raises(ImportError):
        lgf.build_pipeline_graph()