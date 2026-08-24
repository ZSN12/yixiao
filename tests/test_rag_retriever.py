# -*- coding: utf-8 -*-
"""RAG 检索器(rag_retriever) pytest 单测: 典型画像 Top1 命中 / 分数范围 / 溯源 / 本地兜底。

覆盖:
- 新能源电池厂+MES 画像 → Top1 命中 S001(真实成单经验), Top2 S002;
- score 落在 (0,1]、similarity/rule_bonus 数值合法、matched_experiences 溯源非空;
- 未配置 embedding(默认)→ match_method="local_similarity";
- 便捷封装 / 边界输入(空画像、无销售、无经验)。
"""

from modules.data_loader import load_sales, load_sales_experiences
from modules.rag_retriever import (
    SalesMatch,
    cosine_similarity,
    embed_texts,
    local_similarity,
    match_customer_to_sales,
    retrieve_top_sales,
)

# 典型画像: 新能源电池厂 + MES 选型(与 S001 真实成单经验高度重合)
QUERY_NEW_ENERGY = (
    "某新能源电池厂, 300人, 预算150-200万, 痛点是产线数据采集杂乱, 需要MES选型"
)


def _load() -> tuple:
    return load_sales(), load_sales_experiences()


def test_top1_hits_expected_sales(isolated_env):
    """新能源电池厂+MES 画像 → Top1 应为有该成单经验的 S001。"""
    sales, exps = _load()
    matches = retrieve_top_sales(QUERY_NEW_ENERGY, sales, exps, top_k=5)
    assert matches, "应有匹配结果"
    top = matches[0]
    assert isinstance(top, SalesMatch)
    assert top.sales_id == "S001", f"Top1 应为 S001, 实得 {top.sales_id}"
    assert top.sales_name == "张伟"
    # 溯源片段包含新能源电池厂经验(防画像幻觉: 理由必须有据可查)
    assert any("新能源电池厂" in c for c in top.matched_experiences)


def test_score_range_and_fields(isolated_env):
    """综合分/相似度/规则分合法, matched_experiences 溯源非空, 按 score 降序。"""
    sales, exps = _load()
    matches = retrieve_top_sales(QUERY_NEW_ENERGY, sales, exps, top_k=5)
    assert matches
    for m in matches:
        assert 0.0 < m.score <= 1.0
        assert 0.0 <= m.similarity <= 1.0
        assert 0.0 <= m.rule_bonus <= 0.30
        assert m.matched_experiences, "溯源片段不得为空"
        assert m.match_method in ("embedding", "local_similarity")
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_embedding_not_configured_uses_local(isolated_env):
    """未配置 embedding(默认)→ match_method='local_similarity'。"""
    sales, exps = _load()
    matches = retrieve_top_sales(QUERY_NEW_ENERGY, sales, exps)
    assert matches and matches[0].match_method == "local_similarity"


def test_match_customer_to_sales_wrapper(isolated_env):
    """便捷封装: 直接吃画像文本, 结果与 retrieve_top_sales 一致。"""
    sales, exps = _load()
    wrapped = match_customer_to_sales(QUERY_NEW_ENERGY, sales, exps, top_k=3)
    direct = retrieve_top_sales(QUERY_NEW_ENERGY, sales, exps, top_k=3)
    assert [(m.sales_id, m.score) for m in wrapped] == [(m.sales_id, m.score) for m in direct]


def test_software_profile_prefers_software_sales(isolated_env):
    """对照: 软件服务图谱(续约率下滑) → Top 应来自 S002/S004(擅长软件服务)。"""
    sales, exps = _load()
    query2 = "某软件服务SaaS公司, 150人, 预算100万, 痛点客户续约率下滑, 需要客户成功数字化"
    matches = retrieve_top_sales(query2, sales, exps, top_k=5)
    assert matches
    assert matches[0].sales_id in ("S002", "S004")


def test_empty_inputs_return_empty(isolated_env):
    """边界: 空画像 / 无销售 / 无经验 → 空列表, 不抛异常。"""
    sales, exps = _load()
    assert retrieve_top_sales("", sales, exps) == []
    assert retrieve_top_sales("   ", sales, exps) == []
    assert retrieve_top_sales(QUERY_NEW_ENERGY, [], exps) == []
    assert retrieve_top_sales(QUERY_NEW_ENERGY, sales, []) == []


def test_unit_similarity_and_embedding(isolated_env):
    """底层单测: local_similarity / cosine_similarity / embed_texts。"""
    assert local_similarity("MES选型", "MES选型") == 1.0
    assert local_similarity("MES选型", "MES选型犹豫") > 0.0
    assert local_similarity("", "abc") == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_similarity([], [1.0]) == 0.0
    vecs = embed_texts(["新能源电池厂", "软件服务公司"])
    assert len(vecs) == 2 and len(vecs[0]) == len(vecs[1]) > 0