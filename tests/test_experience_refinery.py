# -*- coding: utf-8 -*-
"""经验沉淀管道(experience_refinery) pytest 单测: 提炼 → 入库 → 去重 → 读回 → 检索增益。

覆盖:
- 规则模板提炼是确定性输出(同输入同输出);
- 入库: 首次新增 N 条, 重跑 0 新增(去重生效);
- 加载读回: load_refined_experiences 与入库内容一致;
- 新语料提升检索命中: 入库新经验后, retrieve_top_sales 的溯源片段中出现新经验。

隔离: REFINED_EXPERIENCES_FILE 用 monkeypatch 指向 pytest 临时目录,
绝不写项目 data/refined_experiences.json。
"""

from modules import experience_refinery as er
from modules.data_loader import load_sales, load_sales_experiences
from modules.rag_retriever import retrieve_top_sales


def _redirect_file(monkeypatch, tmp_path):
    """把入库文件重定向到 pytest 临时目录, 返回目标路径。"""
    target = tmp_path / "refined_experiences.json"
    monkeypatch.setattr(er, "REFINED_EXPERIENCES_FILE", target)
    return target


def test_refine_rules_deterministic(monkeypatch, tmp_path, isolated_env):
    """规则模板提炼: 4 条演示商机 → 4 条经验, 重跑输出完全一致(确定性)。"""
    _redirect_file(monkeypatch, tmp_path)
    deals = er.list_mock_deals(4)
    assert len(deals) == 4
    refs = er.refine_deals_to_experiences(deals)
    assert len(refs) == 4
    for exp in refs:
        assert exp.sales_id and exp.content and exp.industry and exp.outcome
    refs2 = er.refine_deals_to_experiences(deals)
    assert [e.model_dump() for e in refs] == [e.model_dump() for e in refs2]


def test_persist_dedup_and_reload(monkeypatch, tmp_path, isolated_env):
    """入库 → 重跑 0 新增(去重) → 加载读回一致。"""
    target = _redirect_file(monkeypatch, tmp_path)
    refs = er.refine_deals_to_experiences(er.list_mock_deals(4))
    added1 = er.persist_refined_experiences(refs)
    assert added1 == 4, f"首次应新增 4 条, 实得 {added1}"
    assert target.exists()
    # 重跑同样的经验 → 0 新增(按 (sales_id, industry, content前20字) 去重)
    added2 = er.persist_refined_experiences(refs)
    assert added2 == 0
    # 重复一次 pipeline(同样的演示商机)→ 0 新增
    added3 = er.persist_refined_experiences(er.refine_deals_to_experiences(er.list_mock_deals(4)))
    assert added3 == 0
    # 加载读回: 4 条且与入库内容一致
    loaded = er.load_refined_experiences()
    assert len(loaded) == 4
    assert {e.content for e in loaded} == {e.content for e in refs}


def test_partial_dedup(monkeypatch, tmp_path, isolated_env):
    """部分去重: 先入 2 条, 再入 4 条 → 只新增 2 条, 文件共 4 条。"""
    target = _redirect_file(monkeypatch, tmp_path)
    deals4 = er.list_mock_deals(4)
    assert er.persist_refined_experiences(er.refine_deals_to_experiences(deals4[:2])) == 2
    assert er.persist_refined_experiences(er.refine_deals_to_experiences(deals4)) == 2
    assert len(er.load_refined_experiences()) == 4


def test_run_refinery_pipeline(monkeypatch, tmp_path, isolated_env):
    """端到端管道: 首次 4 条入库, 重跑 0 新增。"""
    _redirect_file(monkeypatch, tmp_path)
    first = er.run_refinery_pipeline()
    assert len(first) == 4
    second = er.run_refinery_pipeline()
    assert second == []


def test_new_corpus_improves_retrieval(monkeypatch, tmp_path, isolated_env):
    """新语料提升检索命中: 入库后, 检索结果溯源片段出现新经验(检索消费闭环)。"""
    _redirect_file(monkeypatch, tmp_path)
    sales = load_sales()
    base_exps = load_sales_experiences()
    # 画像与"成都华锐数控"演示商机高度重合(数控机床联网率低/人工抄录/180-220万)
    query = (
        "成都某数控装备公司, 约260人, 预算180-220万, "
        "痛点数控机床联网率低、生产数据靠人工抄录"
    )
    before = retrieve_top_sales(query, sales, base_exps, top_k=5)
    assert before, "基线检索应非空"
    # 入库一条新经验(演示商机第 1 条 = S003 成都华锐数控)
    refs = er.refine_deals_to_experiences(er.list_mock_deals(1))
    assert len(refs) == 1 and "华锐数控" in refs[0].content
    assert er.persist_refined_experiences(refs) == 1
    enriched = base_exps + er.load_refined_experiences()
    after = retrieve_top_sales(query, sales, enriched, top_k=5)
    assert after, "扩充语料后检索应非空"
    # 新经验出现在 Top 结果的溯源片段中(供生成可追溯的匹配理由)
    surfaced = [snip for m in after for snip in (m.matched_experiences or [])]
    assert any("华锐数控" in snip for snip in surfaced), "新入库经验应提升并出现在溯源中"
    # 至少不差于基线(S003 聚合文本变长后分数不降)
    assert after[0].score >= before[0].score - 1e-9