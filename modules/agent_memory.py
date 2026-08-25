# -*- coding: utf-8 -*-
"""Agent Memory 记忆存储层: 弱记忆/强记忆的持久化 SQLite 存储 + 本地相似检索。

设计背景(记忆分层):
- 弱记忆(weak):   规则/RAG 自动产出的高置信结果, 未经人工复核。自动写入, 可能含误判。
- 强记忆(strong): 经人工确认或修正后的结果, 可信度高。由弱记忆升级而来, 检索时加权优先。

用途: 供线索分配器(lead_assigner)集成 —— 老客户再次分配时, 先查记忆:
命中强记忆直接用正确销售, 命中弱记忆作为参考加权, 无记忆走规则/RAG 全量判断。

技术选型: 使用标准库 sqlite3(简单可靠, 零第三方依赖), 表 memory_store
与 data_loader 的 analysis_history 同库不同表(共用 settings.db_path 指向的
data/sales_agent.db)。检索用零依赖的 2-gram Jaccard 相似度(借鉴
rag_retriever 的思路, 此处独立轻量实现)。

文件路径一律基于 Path(__file__).resolve().parent.parent 定位项目根, 不依赖 cwd。
所有对外函数: 全类型注解 + logging 记录 + 中文 docstring; 数据库/检索异常
一律 try/except 记日志后降级, 绝不向上抛, 保证上层不崩溃。
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

from config.settings import settings

logger = logging.getLogger(__name__)

# 项目根目录: 本文件位于 <项目根>/modules/agent_memory.py
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 记忆库表名(与 data_loader 的 analysis_history 同库不同表)
MEMORY_TABLE: str = "memory_store"

# 相似检索排序权重: strong 记忆权重 2, weak 记忆权重 1
STRONG_WEIGHT: float = 2.0
WEAK_WEIGHT: float = 1.0

# 本地相似检索默认 Top-K
DEFAULT_TOP_K: int = 3


# ============================================================
# 记忆模型
# ============================================================

class MemoryEntry(BaseModel):
    """一条记忆: 客户 -> 推荐销售 -> 人工复核结论(弱记忆或强记忆)。

    说明: memory_id / source / created_at 由写入函数自动生成并回填
    (write_weak_memory / upgrade_to_strong), 调用方可留空。
    """

    memory_id: str = ""     # uuid 短码, 自动生成
    customer_id: str        # 来源客户
    query_text: str         # 客户画像/检索 query 文本
    sales_id: str           # 系统推荐销售
    decision: str           # "confirm" | "correct": 人工复核结论(确认 / 修正)
    correct_sales_id: str   # 最终正确销售(decision=correct 时与 sales_id 不同, confirm 时相同)
    confidence: float       # 0~1, 规则与 RAG 一致时建议 0.9, 不一致时 0.6(写入方决定)
    source: str = ""        # "weak" | "strong": 弱记忆 / 强记忆, 写入函数回填
    feedback_note: str = "" # 人工备注(可选)
    created_at: str = ""    # ISO 时间戳, 写入函数回填


# ============================================================
# 数据库路径解析
# ============================================================

def _resolve_db_path() -> Path:
    """解析记忆库文件路径(与 data_loader 对齐: 相对路径以项目根解析)。

    Returns:
        Path: SQLite 数据库文件绝对路径。
    """
    db_file = Path(settings.db_path)
    if not db_file.is_absolute():
        db_file = PROJECT_ROOT / db_file
    return db_file


# ============================================================
# 连接/初始化
# ============================================================

def _connect() -> Optional[sqlite3.Connection]:
    """建立 SQLite 连接(自动创建父目录); 失败返回 None, 不抛。

    Returns:
        sqlite3.Connection | None: 连接对象; 异常时返回 None。
    """
    try:
        db_file = _resolve_db_path()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        # timeout=30: 多线程并发写同一 SQLite 文件时等待写锁, 避免立即
        # 抛 "database is locked" 或形成等待死锁。
        conn = sqlite3.connect(str(db_file), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:  # noqa: BLE001 —— 数据库不可用不允许让上层崩溃
        logger.error("连接记忆库失败: %s", exc)
        return None


def init_memory_db() -> None:
    """初始化记忆库并建表 memory_store(幂等, 已存在则跳过)。

    Returns:
        None
    """
    conn = _connect()
    if conn is None:
        return
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {MEMORY_TABLE} (
                memory_id        TEXT PRIMARY KEY,
                customer_id      TEXT NOT NULL,
                query_text       TEXT NOT NULL,
                sales_id         TEXT NOT NULL,
                decision         TEXT NOT NULL,
                correct_sales_id TEXT NOT NULL,
                confidence       REAL NOT NULL,
                source           TEXT NOT NULL,
                feedback_note    TEXT NOT NULL DEFAULT '',
                created_at       TEXT NOT NULL
            )
        """)
        # 常用过滤/排序字段建索引, 加速查询
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{MEMORY_TABLE}_customer ON {MEMORY_TABLE}(customer_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{MEMORY_TABLE}_source ON {MEMORY_TABLE}(source)")
        conn.commit()
        logger.info("记忆库初始化完成: %s(表 %s)", _resolve_db_path(), MEMORY_TABLE)
    except Exception as exc:  # noqa: BLE001
        logger.error("初始化记忆库失败: %s", exc)
    finally:
        conn.close()


# ============================================================
# 写入
# ============================================================

def _insert(entry: MemoryEntry) -> None:
    """把一条记忆写入库(内部工具, 不处理同客户去重)。

    Args:
        entry: 记忆条目。

    Returns:
        None
    """
    conn = _connect()
    if conn is None:
        return
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {MEMORY_TABLE} "
            "(memory_id, customer_id, query_text, sales_id, decision, "
            " correct_sales_id, confidence, source, feedback_note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.memory_id,
                entry.customer_id,
                entry.query_text,
                entry.sales_id,
                entry.decision,
                entry.correct_sales_id,
                entry.confidence,
                entry.source,
                entry.feedback_note,
                entry.created_at,
            ),
        )
        conn.commit()
        logger.info("记忆已写入: memory_id=%s customer_id=%s source=%s",
                    entry.memory_id, entry.customer_id, entry.source)
    except Exception as exc:  # noqa: BLE001
        logger.error("写入记忆失败(memory_id=%s): %s", entry.memory_id, exc)
    finally:
        conn.close()


def _delete_by_customer(customer_id: str, source: Optional[str] = None) -> None:
    """删除某客户的记忆(可限定来源), 供"同客户只保留最新"防膨胀使用。

    Args:
        customer_id: 客户 ID。
        source: 限定删除的记忆来源("weak"/"strong"), None 表示全部。

    Returns:
        None
    """
    conn = _connect()
    if conn is None:
        return
    try:
        if source is None:
            conn.execute(f"DELETE FROM {MEMORY_TABLE} WHERE customer_id = ?", (customer_id,))
        else:
            conn.execute(
                f"DELETE FROM {MEMORY_TABLE} WHERE customer_id = ? AND source = ?",
                (customer_id, source),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("删除记忆失败(customer_id=%s, source=%s): %s", customer_id, source, exc)
    finally:
        conn.close()


def write_weak_memory(entry: MemoryEntry) -> MemoryEntry:
    """写入一条弱记忆(source=weak)。

    同一 customer_id 重复写入时只保留最新(先删该客户旧 weak 记忆再插入), 防止膨胀。

    Args:
        entry: 记忆条目(应由调用方构造, memory_id 可留空自动生成)。

    Returns:
        MemoryEntry: 落库后的记忆条目(含自动生成的 memory_id 与 created_at)。
    """
    # 规范化: 自动生成 memory_id(uuid 短码)与 created_at(ISO 时间戳)
    if not entry.memory_id:
        entry.memory_id = uuid.uuid4().hex[:8]
    if not entry.created_at:
        entry.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry.source = "weak"
    # 防膨胀: 同一客户只保留最新一条 weak 记忆
    _delete_by_customer(entry.customer_id, source="weak")
    _insert(entry)
    return entry


def upgrade_to_strong(entry: MemoryEntry, feedback_note: str = "") -> MemoryEntry:
    """把一条记忆升级为强记忆(source=strong)。

    调用方应先填好 decision / correct_sales_id(人工复核结论), 本函数负责落库
    并保留人工备注。升级不影响该客户已有的 weak 记忆(两者并存, weak 供参考)。

    Args:
        entry: 记忆条目(decision/correct_sales_id 已由调用方填好)。
        feedback_note: 人工备注, 追加到条目 feedback_note 字段。

    Returns:
        MemoryEntry: 升级后的强记忆条目(含生成的 memory_id / created_at, source=strong)。
    """
    if not entry.memory_id:
        entry.memory_id = uuid.uuid4().hex[:8]
    if not entry.created_at:
        entry.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry.source = "strong"
    if feedback_note:
        entry.feedback_note = (entry.feedback_note + " | " + feedback_note).strip(" |")
    # 一个客户只保留一条强记忆(人工结论唯一), 避免多条强记忆互相矛盾
    _delete_by_customer(entry.customer_id, source="strong")
    _insert(entry)
    return entry


# ============================================================
# 本地相似检索(2-gram Jaccard, 轻量零依赖)
# ============================================================

def _char_bigrams(text: str) -> set:
    """把文本切成字符 2-gram 集合(去空白, 适合中文; 借鉴 rag_retriever 思路)。

    Args:
        text: 原始文本。

    Returns:
        set[str]: 2-gram 集合; 文本过短时返回整体, 保证至少一个特征。
    """
    chars = [c for c in (text or "") if not c.isspace()]
    if len(chars) < 2:
        return {"".join(chars)} if chars else set()
    return {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}


def _jaccard(text_a: str, text_b: str) -> float:
    """两个文本的 2-gram Jaccard 相似度(0~1)。

    Args:
        text_a: 文本 a。
        text_b: 文本 b。

    Returns:
        float: Jaccard 相似度; 任一文本为空或双方并集为空时返回 0.0。
    """
    grams_a = _char_bigrams(text_a)
    grams_b = _char_bigrams(text_b)
    union = grams_a | grams_b
    if not union:
        return 0.0
    return len(grams_a & grams_b) / len(union)


def search_similar_memory(
    query_text: str,
    top_k: int = DEFAULT_TOP_K,
    source_filter: Optional[str] = None,
) -> List[MemoryEntry]:
    """按文本相似度检索记忆(本地 2-gram Jaccard, 强记忆加权)。

    排序分 = source 权重(strong=2, weak=1) × Jaccard 相似度, 降序取 Top-K。

    Args:
        query_text: 检索 query 文本(与写入时的 query_text 同口径, 如 画像文本)。
        top_k: 返回条数, 默认 3; 非法(<=0)时回落默认值。
        source_filter: 来源过滤("weak"/"strong"), None 表示不过滤。

    Returns:
        list[MemoryEntry]: 按加权分降序的记忆列表; 库空/查询失败返回 [] 而不抛错。
    """
    if not query_text or not query_text.strip():
        logger.warning("search_similar_memory 收到空 query, 返回空列表")
        return []
    if top_k is None or top_k <= 0:
        top_k = DEFAULT_TOP_K

    conn = _connect()
    if conn is None:
        return []
    try:
        # 全量加载后内存排序(语料为记忆量级, 数百~数千条, 足够轻量)
        if source_filter is None:
            rows = conn.execute(f"SELECT * FROM {MEMORY_TABLE}").fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {MEMORY_TABLE} WHERE source = ?", (source_filter,)
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.error("读取记忆失败: %s", exc)
        return []
    finally:
        conn.close()

    scored: List[tuple] = []
    for row in rows:
        entry = MemoryEntry(**dict(row))
        sim = _jaccard(query_text, entry.query_text)
        weight = STRONG_WEIGHT if entry.source == "strong" else WEAK_WEIGHT
        scored.append((weight * sim, sim, entry))

    scored.sort(key=lambda t: t[0], reverse=True)
    results = [entry for _, _, entry in scored[:top_k]]
    logger.info("记忆相似检索: query=%d字, 候选 %d 条, 返回 %d 条",
                len(query_text), len(rows), len(results))
    return results


# ============================================================
# 调试/展示
# ============================================================

def list_memories(limit: int = 20) -> List[MemoryEntry]:
    """列出最近写入的记忆(按 created_at 倒序), 供调试/展示。

    Args:
        limit: 返回条数, 默认 20; 非法(<=0)时回落 1 条不返回。

    Returns:
        list[MemoryEntry]: 记忆列表; 库空/失败返回 [] 不抛错。
    """
    if limit <= 0:
        return []
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            f"SELECT * FROM {MEMORY_TABLE} ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        results = [MemoryEntry(**dict(row)) for row in rows]
        logger.info("列出记忆 %d 条(limit=%d)", len(results), limit)
        return results
    except Exception as exc:  # noqa: BLE001
        logger.error("列出记忆失败: %s", exc)
        return []
    finally:
        conn.close()