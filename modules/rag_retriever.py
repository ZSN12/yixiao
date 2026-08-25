# -*- coding: utf-8 -*-
"""RAG 检索器: 检索与"新客户画像"最匹配的销售经验片段。

职责(检索增强生成链路中的 "R" 环节):
1. 把新客户画像文本作为 query, 对该客户的候选销售做"向量/相似度粗排 Top-K + 规则加权精排",
   输出带分数与命中依据(溯源)的匹配结果, 供上层 LLM 生成可追溯的推荐理由/分配决策,
   防画像幻觉(避免让 LLM 凭空编造"为什么分配给他")。

Embedding 可插拔设计:
- 配置了 embedding_api_base / embedding_api_key / embedding_model(见 config.settings)时,
  走 OpenAI 兼容 /embeddings 接口做向量检索(余弦相似度);
- 未配置(默认)时, 零依赖本地兜底: 基于中文字符 n-gram 的朴素相似度(Jaccard / 余弦),
  保证"没有 API Key 也能跑通整条 RAG 链路"。
- 两种检索方式由内部工厂/选择逻辑(`_embed_with_method`)统一封装, 对外接口保持不变。

工程取舍(为什么用"规则加权"代替 reranker, 调研 FastGPT 等项目结论):
小语料(数百~数千条经验片段)场景下, 重排序模型(reranker)的排序质量提升有限,
却要额外引入一个大模型部署与推理成本; FastGPT 等开源项目在语料量小时同样采用
"向量粗排 + 规则精排"的轻量方案。因此本模块用二级检索
(第一级语义/相似度粗排取 Top-K, 第二级行业/关键词规则加权精排)代替 reranker。
"""

from typing import Dict, List, Optional, Tuple

import logging

from pydantic import BaseModel

from config.settings import settings
from modules.data_loader import Sales, SalesExperience, build_experience_map

logger = logging.getLogger(__name__)

# ============================================================
# 检索结果模型(带溯源)
# ============================================================


class SalesMatch(BaseModel):
    """一条"客户画像 -> 销售"的匹配结果(带分数与溯源依据)。"""

    sales_id: str
    sales_name: str                    # 从销售列表查得
    score: float                       # 综合分(0~1): similarity * (1 + rule_bonus) 封顶
    similarity: float                  # 语义/相似度分(embedding 余弦 或 本地 n-gram 相似度)
    rule_bonus: float                  # 规则加权分(行业命中 + 关键词命中)
    matched_experiences: List[str]     # 命中的经验片段内容(供生成理由, 溯源用)
    match_method: str                  # "embedding" | "local_similarity"


# ============================================================
# 规则加权参数(行业命中 / 关键词命中)
# ============================================================

# 一级粗排后进入规则精排的候选数(语料小时 Top-K 即全部候选)
DEFAULT_TOP_K: int = 5

# 规则加权: 行业命中(客户画像中提到的行业)
_QUERY_GOOD_AT_BONUS: float = 0.15     # 行业命中销售"擅长行业"(good_at_industries)
_QUERY_EXP_INDUSTRY_BONUS: float = 0.10  # 行业命中该销售某一经验片段的 industry
_RULE_BONUS_MAX: float = 0.30          # 规则加权总分上限(避免规则喧宾夺主)

# 规则加权: 关键词命中(query 与经验内容同时出现 → 加小分)
# 每个关键词权重低(0.02), 命中多个关键词封顶 0.10。
_DOMAIN_KEYWORDS: List[Tuple[str, float]] = [
    ("MES", 0.02),
    ("APS", 0.02),
    ("ERP", 0.02),
    ("排产", 0.02),
    ("数据采集", 0.02),
    ("设备联网", 0.02),
    ("合规", 0.02),
    ("POC", 0.02),
    ("续约", 0.02),
    ("数字化", 0.02),
]
_KEYWORD_BONUS_MAX: float = 0.10


# ============================================================
# 内嵌文本工具(字符 n-gram)
# ============================================================


def _char_ngrams(text: str, n: int = 2) -> List[str]:
    """把文本切成字符 n-gram 列表(默认 2-gram, 适合中文)。

    Args:
        text: 原始文本。
        n: n-gram 长度, 默认 2(bigram)。

    Returns:
        list[str]: n-gram 列表; 文本去除空白后长度不足 n 时, 返回原文本整体
                   (保证任何文本至少有一个特征)。
    """
    chars = [c for c in text if not c.isspace()]   # 去除空白, 避免特征破碎
    if len(chars) < n:
        return ["".join(chars)]
    return ["".join(chars[i:i + n]) for i in range(len(chars) - n + 1)]


# ============================================================
# 本地向量化(零依赖兜底)
# ============================================================


def _local_embed_texts(texts: List[str]) -> List[List[float]]:
    """零依赖本地文本向量化: 字符 2-gram 布尔特征向量(TF 简化版)。

    Args:
        texts: 一批文本。词表取这批文本的 2-gram 并集, 每个文本映射为
               0/1 特征向量(出现该 n-gram 记 1)。

    Returns:
        list[list[float]]: 与 texts 等长的特征向量列表; 空文本映射为全 0 向量。
    """
    gram_sets: List[set] = [set(_char_ngrams(t)) for t in texts]
    vocab: List[str] = sorted(set().union(*gram_sets)) if gram_sets else []
    vectors: List[List[float]] = []
    for gs in gram_sets:
        vectors.append([1.0 if g in gs else 0.0 for g in vocab])
    logger.debug("本地向量化完成: %d 个文本, 词表 %d 个 2-gram", len(texts), len(vocab))
    return vectors


# ============================================================
# Embedding API(OpenAI 兼容, 可插拔)
# ============================================================


def _embedding_configured() -> bool:
    """判断是否配置了可用的 embedding API(三者齐备才算)。"""
    return bool(
        settings.embedding_api_base
        and settings.embedding_api_key
        and settings.embedding_model
    )


def _api_embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """通过 OpenAI 兼容 /embeddings 接口计算文本向量。

    Args:
        texts: 一批文本。

    Returns:
        list[list[float]]: 向量列表; 调用失败(网络/鉴权/格式异常)时返回 None,
                           由上层降级到本地兜底。
    """
    if not _embedding_configured():
        return None
    try:
        # 延迟导入: openai 是可选依赖, 未安装时走本地兜底
        import httpx
        from openai import OpenAI
        http_client = httpx.Client(timeout=settings.llm_timeout)
        client = OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_api_base,
            http_client=http_client,
        )
        resp = client.embeddings.create(
            model=settings.embedding_model,
            input=texts,
        )
        # resp.data 顺序与 input 一致(OpenAI 兼容接口约定)
        vectors = [item.embedding for item in resp.data]
        if len(vectors) != len(texts):
            logger.error("embedding 接口返回数量不符(期望 %d, 实得 %d)", len(texts), len(vectors))
            return None
        logger.info("embedding API 调用成功: %d 个文本, model=%s", len(texts), settings.embedding_model)
        return vectors
    except Exception as exc:  # noqa: BLE001 —— 任何失败都降级, 绝不中断链路
        logger.error("embedding API 调用失败(%s), 降级到本地兜底", exc)
        return None


# ============================================================
# 对外向量化入口(可插拔工厂)
# ============================================================


def _embed_with_method(texts: List[str]) -> Tuple[List[List[float]], str]:
    """向量化工厂: 优先 API, 失败/未配置则本地兜底, 并报告实际使用的方法。

    Args:
        texts: 一批文本。

    Returns:
        tuple[list[list[float]], str]: (向量列表, 检索方法)
            "embedding" —— 向量来自 embedding API;
            "local_similarity" —— 向量来自本地 n-gram 兜底。
    """
    if _embedding_configured():
        api_vectors = _api_embed_texts(texts)
        if api_vectors is not None:
            return api_vectors, "embedding"
        logger.warning("embedding API 不可用, 自动降级为本地 n-gram 向量(方法: local_similarity)")
    return _local_embed_texts(texts), "local_similarity"


def embed_texts(texts: List[str]) -> List[List[float]]:
    """计算一批文本的向量(可插拔: 配置了 embedding API 走接口, 否则本地兜底)。

    Args:
        texts: 一批文本。

    Returns:
        list[list[float]]: 与 texts 等长的向量列表(API 向量 或 本地 2-gram 布尔向量)。
    """
    if not texts:
        return []
    vectors, _method = _embed_with_method(texts)
    return vectors


# ============================================================
# 相似度计算
# ============================================================


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算两个向量的余弦相似度。

    Args:
        a: 向量 a。
        b: 向量 b。

    Returns:
        float: 余弦相似度(0~1); 任一向量为零向量或维度不一致时返回 0.0。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def local_similarity(text_a: str, text_b: str) -> float:
    """零依赖本地文本相似度: 字符 2-gram 集合的 Jaccard 相似度。

    Args:
        text_a: 文本 a。
        text_b: 文本 b。

    Returns:
        float: Jaccard 相似度(0~1); 双方 2-gram 并集为空时返回 0.0。
    """
    grams_a = set(_char_ngrams(text_a))
    grams_b = set(_char_ngrams(text_b))
    if not grams_a or not grams_b:
        return 0.0
    union = grams_a | grams_b
    if not union:
        return 0.0
    return len(grams_a & grams_b) / len(union)


# ============================================================
# 规则加权精排
# ============================================================


def _industry_vocabulary(sales_list: List[Sales], experiences: List[SalesExperience]) -> set:
    """收集行业词表: 销售的擅长行业 + 经验片段的 industry(供从画像文本中抽行业)。"""
    vocab: set = set()
    for sales in sales_list:
        vocab.update(sales.good_at_industries)
    for exp in experiences:
        if exp.industry:
            vocab.add(exp.industry)
    return vocab


def _extract_industries(query_text: str, vocab: set) -> set:
    """从客户画像文本中抽取命中的行业(子串匹配)。"""
    if not vocab or not query_text:
        return set()
    return {ind for ind in vocab if ind and ind in query_text}


def _rule_bonus(
    query_text: str,
    query_industries: set,
    sales: Sales,
    experiences: List[SalesExperience],
) -> float:
    """计算规则加权分: 行业命中(good_at_industries / 经验 industry) + 关键词命中。

    Args:
        query_text: 客户画像文本。
        query_industries: 从画像中抽出的行业集合。
        sales: 候选销售。
        experiences: 该销售的经验片段列表。

    Returns:
        float: 规则加权分, 封顶 _RULE_BONUS_MAX(0.30)。
    """
    bonus: float = 0.0
    # 1) 行业命中销售"擅长行业"
    if query_industries & set(sales.good_at_industries):
        bonus += _QUERY_GOOD_AT_BONUS
    # 2) 行业命中某条经验片段的 industry
    exp_industries = {e.industry for e in experiences if e.industry}
    if query_industries & exp_industries:
        bonus += _QUERY_EXP_INDUSTRY_BONUS
    # 3) 关键词命中: 画像与经验内容同时出现的关键词
    agg_text = "\n".join(e.content for e in experiences)
    keyword_bonus: float = 0.0
    for kw, weight in _DOMAIN_KEYWORDS:
        if kw in query_text and kw in agg_text:
            keyword_bonus += weight
            if keyword_bonus >= _KEYWORD_BONUS_MAX:
                keyword_bonus = _KEYWORD_BONUS_MAX
                break
    bonus += keyword_bonus
    return min(bonus, _RULE_BONUS_MAX)


def _rank_experiences(
    query_text: str,
    experiences: List[SalesExperience],
    query_industries: set,
    method: str,
) -> List[str]:
    """按与画像的相关度给经验片段排序(溯源素材选择), 返回片段内容列表。

    Args:
        query_text: 客户画像文本。
        experiences: 该销售的经验片段列表。
        query_industries: 画像中抽出的行业集合。
        method: 检索方法("embedding" | "local_similarity"), 决定片段相似度的计算方式。

    Returns:
        list[str]: 按相关度降序的经验片段内容(行业命中优先, 再按文本相似度)。
    """
    if not experiences:
        return []
    if method == "embedding":
        texts = [query_text] + [e.content for e in experiences]
        vecs, _ = _embed_with_method(texts)
        q_vec = vecs[0]
        base_scores = [cosine_similarity(q_vec, v) for v in vecs[1:]]
    else:
        base_scores = [local_similarity(query_text, e.content) for e in experiences]

    scored: List[Tuple[float, str]] = []
    for i, exp in enumerate(experiences):
        industry_bonus = _QUERY_EXP_INDUSTRY_BONUS if exp.industry in query_industries else 0.0
        scored.append((base_scores[i] + industry_bonus, exp.content))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [content for _, content in scored]


# ============================================================
# 二级检索主流程
# ============================================================


def retrieve_top_sales(
    query_text: str,
    sales_list: List[Sales],
    experiences: List[SalesExperience],
    top_k: int = DEFAULT_TOP_K,
) -> List[SalesMatch]:
    """检索与客户画像最匹配的 Top-K 销售(两级检索: 粗排 + 规则精排)。

    流程:
    1. 对每名销售聚合其经验片段文本;
    2. 第一级粗排: 计算 query 与各销售经验集的相似度(embedding 余弦 或 本地 n-gram),
       取 Top-K(语料小时 Top-K 即全部候选);
    3. 第二级精排: 规则加权(画像行业 vs 销售擅长行业 / 经验 industry 命中 +0.1~0.2,
       画像与经验关键词命中加分)以"乘法放大"方式作用于相似度
       (综合分 = similarity × (1 + rule_bonus)), 与相似度合成最终排序;
    4. 每个匹配项携带命中的经验片段内容(matched_experiences)作为溯源依据,
       供上层 LLM 生成可追溯的推荐理由, 防画像幻觉。

    Args:
        query_text: 客户画像文本(由 profile_analyzer 或基础信息拼接, 本模块不负责生成)。
        sales_list: 销售人员列表(load_sales 的结果)。
        experiences: 销售经验片段列表(load_sales_experiences 的结果)。
        top_k: 返回前多少个候选, 默认 5; 非法(<=0)时回落默认值。

    Returns:
        list[SalesMatch]: 按综合分 score 降序的匹配结果;
                查无销售 / 查无经验 / 画像文本为空时返回空列表。
    """
    if not query_text or not query_text.strip():
        logger.warning("retrieve_top_sales 收到空画像文本, 返回空列表")
        return []
    if not sales_list or not experiences:
        logger.warning("retrieve_top_sales 无销售或无经验语料(sales=%d, experiences=%d), 返回空列表",
                       len(sales_list or []), len(experiences or []))
        return []

    if top_k is None or top_k <= 0:
        top_k = DEFAULT_TOP_K

    sales_by_id: Dict[str, Sales] = {s.sales_id: s for s in sales_list}
    exp_map: Dict[str, List[SalesExperience]] = build_experience_map(experiences)

    # 只考虑"有经验片段 且 在销售列表中"的候选, 保证能查到姓名
    candidates: List[Tuple[str, List[SalesExperience]]] = [
        (sid, exps) for sid, exps in exp_map.items() if sid in sales_by_id
    ]
    if not candidates:
        logger.warning("retrieve_top_sales 没有任何销售拥有经验片段, 返回空列表")
        return []

    # ---- 第一级: 粗排(向量/相似度) ----
    agg_texts: List[str] = ["\n".join(e.content for e in exps) for _, exps in candidates]
    texts_to_embed: List[str] = [query_text] + agg_texts
    vectors, method = _embed_with_method(texts_to_embed)
    q_vec = vectors[0]

    sims: List[float] = []
    for i, (_, exps) in enumerate(candidates):
        if method == "embedding":
            sims.append(cosine_similarity(q_vec, vectors[i + 1]))
        else:
            sims.append(local_similarity(query_text, agg_texts[i]))
    logger.info("粗排完成: %d 名候选销售, 方法=%s", len(candidates), method)

    # Top-K 粗排结果(sims 降序)
    ranked_idx = sorted(range(len(candidates)), key=lambda i: sims[i], reverse=True)[:top_k]

    # ---- 第二级: 规则加权精排 ----
    vocab = _industry_vocabulary(sales_list, experiences)
    query_industries = _extract_industries(query_text, vocab)

    results: List[SalesMatch] = []
    for idx in ranked_idx:
        sid, exps = candidates[idx]
        sales = sales_by_id[sid]
        similarity = sims[idx]
        bonus = _rule_bonus(query_text, query_industries, sales, exps)
        # 综合分 = similarity * (1 + rule_bonus), 封顶 1.0:
        # 规则加权采用"乘法放大"而非直接相加 —— 规则只对已有语义相关性的候选放大,
        # 避免规则分喧宾夺主、把低相关候选顶上来(工程取舍: 语料小时规则当 reranker 用, 见模块 docstring)。
        score = min(1.0, similarity * (1.0 + bonus))
        matched = _rank_experiences(query_text, exps, query_industries, method)[:2]
        results.append(SalesMatch(
            sales_id=sid,
            sales_name=sales.name,
            score=round(score, 4),
            similarity=round(similarity, 4),
            rule_bonus=round(bonus, 4),
            matched_experiences=matched,
            match_method=method,
        ))

    results.sort(key=lambda m: m.score, reverse=True)
    logger.info("最终匹配 %d 名销售: Top1=%s(%.4f), 方法=%s",
                len(results), results[0].sales_id, results[0].score, method)
    return results


def match_customer_to_sales(
    customer_profile: str,
    sales_list: List[Sales],
    experiences: List[SalesExperience],
    top_k: int = DEFAULT_TOP_K,
) -> List[SalesMatch]:
    """便捷封装: 直接吃客户画像文本, 内部调 retrieve_top_sales。

    Args:
        customer_profile: 客户画像文本(如 "某新能源电池厂, 300人, 预算150-200万, ...")。
        sales_list: 销售人员列表。
        experiences: 销售经验片段列表。
        top_k: 返回前多少个候选, 默认 5。

    Returns:
        list[SalesMatch]: 按综合分降序的匹配结果(同 retrieve_top_sales)。
    """
    logger.info("match_customer_to_sales: 画像长度=%d 字符, top_k=%d",
                len(customer_profile or ""), top_k)
    return retrieve_top_sales(customer_profile, sales_list, experiences, top_k=top_k)