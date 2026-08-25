# -*- coding: utf-8 -*-
"""数据层(data_loader): 全项目的共享数据契约与数据访问入口。

职责:
1. 定义业务模型 Customer / ChatMessage / ChatRecord / Sales(Pydantic 模型)。
2. 从 data/ 目录加载三份 mock 数据(客户 / 聊天记录 / 销售人员)。
3. 提供 SQLite 存储层(SQLAlchemy 2.0), 保存画像分析历史, 供 main.py GET /history 查询。
4. 预留对接真实 CRM / 企业微信会话存档的接口(当前返回空列表)。

所有文件路径一律基于 Path(__file__).resolve().parent.parent 定位项目根,
不依赖当前工作目录(cwd)。所有函数均有完整类型注解, 并通过 logging 记录日志。
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from pydantic import BaseModel
from sqlalchemy import create_engine, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# 项目根目录: 本文件位于 <项目根>/modules/data_loader.py
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
# 数据文件目录
DATA_DIR: Path = PROJECT_ROOT / "data"

# 各数据文件相对项目根的路径
CUSTOMERS_FILE: Path = DATA_DIR / "mock_customers.json"
CHAT_RECORDS_FILE: Path = DATA_DIR / "mock_chat_records.json"
SALES_FILE: Path = DATA_DIR / "mock_sales.json"
EXPERIENCE_FILE: Path = DATA_DIR / "mock_sales_experience.json"

logger = logging.getLogger(__name__)

# ---- 进程内文件写锁: 防止 FastAPI/调度器多线程并发读-改-写互相覆盖 ----
# 使用可重入锁 RLock, 同一线程在写锁内再次调用 save_* 不会自锁(死锁)。
_customers_lock = threading.RLock()
_sales_lock = threading.RLock()


@contextmanager
def customers_write_lock() -> Iterator[None]:
    """客户 JSON 的读-改-写临界区锁(供 feishu 回调等外部调用方使用)。"""
    with _customers_lock:
        yield


@contextmanager
def sales_write_lock() -> Iterator[None]:
    """销售 JSON 的读-改-写临界区锁(供销售画像反哺等外部调用方使用)。"""
    with _sales_lock:
        yield


# ============================================================
# 业务模型(共享数据契约 —— 字段名与类型必须与全团队一致)
# ============================================================

class Customer(BaseModel):
    """客户模型。"""

    customer_id: str
    customer_name: str
    industry: str
    city: str
    scale: str
    owner_sales_id: Optional[str] = None   # 归属销售, 无归属为 None
    follow_up_status: str = "待跟进"       # 跟进状态: 待跟进/已接单跟进/已电话沟通/已成单/已转交改派
    create_time: str
    social_security_count: Optional[str] = None   # 社保人数(字符串, 可存 "120" 或 "120-300" 区间)


class ChatMessage(BaseModel):
    """单条聊天消息。"""

    role: str          # "销售" | "客户"
    content: str


class ChatRecord(BaseModel):
    """一次会话记录(企业微信/IM 会话存档的抽象)。"""

    record_id: str
    customer_id: str
    sales_id: Optional[str] = None   # 会话关联的销售, 可能为空
    chat_time: str
    messages: List[ChatMessage]


class Sales(BaseModel):
    """销售人员模型。"""

    sales_id: str
    name: str
    good_at_industries: List[str]     # 擅长行业
    responsible_cities: List[str]     # 负责城市
    current_load: int = 0             # 当前线索负载(用于负载均衡)
    mobile: str = ""                  # 飞书绑定手机号(用于工作通知推送)
    open_id: str = ""                 # 飞书用户 open_id(用于飞书网页应用身份识别)


class SalesExperience(BaseModel):
    """销售经验片段(供 RAG 语义检索匹配新客户)。"""

    sales_id: str
    content: str          # 经验片段完整描述(客户场景/预算/角色/痛点/结果)
    industry: str = ""    # 关联行业(检索粗筛用), 可空
    outcome: str = ""     # 结果: 成单 / 跟进中 / 流失, 可空


# ============================================================
# SQLite 存储层(SQLAlchemy 2.0 声明式)
# ============================================================

class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类。"""


class AnalysisHistory(Base):
    """画像分析历史记录表: 每条记录保存一次客户画像分析的结果快照。"""

    __tablename__ = "analysis_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_name: Mapped[str] = mapped_column(String(128))
    result_json: Mapped[str] = mapped_column(Text)        # AnalysisResult 序列化后的 JSON 文本
    created_at: Mapped[str] = mapped_column(String(32))   # ISO 时间字符串


class RoleReview(Base):
    """电话录音角色判定复核表: 低置信度的说话人角色判定, 待人工确认。

    当 speaker_role_resolver 判定 confidence < phone_role_min_confidence 时,
    该通话的角色映射落库待复核; 人工确认后 status 置为 resolved。
    """

    __tablename__ = "role_review"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), default="")
    speaker_roles: Mapped[str] = mapped_column(Text)       # 当前判定 {"Speaker_0": "销售", ...}
    method: Mapped[str] = mapped_column(String(16), default="")   # metadata/heuristic/llm/manual
    confidence: Mapped[str] = mapped_column(String(16), default="")  # 置信度(字符串, 保留小数)
    notes: Mapped[str] = mapped_column(Text, default="")    # 判定依据
    transcript: Mapped[str] = mapped_column(Text, default="")  # 分段转写快照(供人工判断)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/resolved
    resolved_roles: Mapped[str] = mapped_column(Text, default="")  # 人工确认后的最终角色
    created_at: Mapped[str] = mapped_column(String(32))    # ISO 时间字符串


# 全局数据库引擎与会话工厂(init_db 时初始化)
_engine = None
_SessionLocal = None
# 记录当前已初始化引擎对应的数据库文件路径, 同路径重复调用时复用引擎,
# 避免每次 init_db 都 create_engine 造成旧连接/旧引擎泄漏。
_engine_db_file: Optional[str] = None


def init_db(db_path: Optional[str] = None) -> None:
    """初始化 SQLite 数据库并建表。

    Args:
        db_path: 数据库文件路径。None 时取 config.settings 中的 db_path
                 (默认 "data/sales_agent.db", 相对项目根解析);
                 传入相对路径同样以项目根为基准。绝对路径直接使用。

    Returns:
        None

    Notes:
        自动创建 data/ 父目录; 数据库异常只记日志, 不向上抛, 保证上层不崩溃。
    """
    global _engine, _SessionLocal, _engine_db_file

    if db_path is None:
        # 延迟导入 settings, 避免循环依赖(settings 不依赖本模块)
        from config.settings import settings
        raw_path = settings.db_path
    else:
        raw_path = db_path

    db_file = Path(raw_path)
    if not db_file.is_absolute():
        db_file = PROJECT_ROOT / db_file

    try:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        # 同一路径重复调用时复用已建引擎(会话工厂同样复用), 防止连接泄漏
        if _engine is not None and _engine_db_file == str(db_file):
            logger.debug("SQLite 引擎已存在, 复用: %s", db_file)
            return
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:  # noqa: BLE001
                pass
        _engine = create_engine(
            f"sqlite:///{db_file}",
            echo=False,
            connect_args={
                "check_same_thread": False,   # 允许跨线程使用(FastAPI/调度场景)
                # 多线程并发写同一 SQLite 文件时, 等待最多 30 秒拿写锁,
                # 避免并发写入立即抛 "database is locked" 或形成等待死锁。
                "timeout": 30,
            },
            pool_pre_ping=True,               # 复用连接前先探活, 避免拿到失效连接
        )
        _engine_db_file = str(db_file)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        # 建表(幂等)
        Base.metadata.create_all(_engine)
        logger.info("SQLite 数据库初始化完成: %s", db_file)
    except Exception as exc:  # noqa: BLE001 —— 数据库异常不允许让上层崩溃
        logger.error("初始化 SQLite 数据库失败(%s): %s", db_file, exc)


def _get_session():
    """获取数据库会话; 数据库未初始化或异常时返回 None。"""
    try:
        if _SessionLocal is None:
            init_db()
        if _SessionLocal is not None:
            return _SessionLocal()
    except Exception as exc:  # noqa: BLE001
        logger.error("获取数据库会话失败: %s", exc)
    return None


def save_analysis_record(
    customer_id: str,
    customer_name: str,
    result: dict,
    created_at: Optional[str] = None,
) -> None:
    """保存一条画像分析历史记录。

    Args:
        customer_id: 客户 ID。
        customer_name: 客户名称。
        result: 画像分析结果字典(AnalysisResult 序列化后的 dict)。
        created_at: ISO 时间字符串; None 时取当前时间。一次流水线批量落库时
                    建议由调用方统一传入同一时间戳, 便于"最新批次"按时间精确分组。

    Returns:
        None

    Notes:
        数据库异常只记日志, 不向上抛, 保证上层业务不崩溃。
    """
    session = _get_session()
    if session is None:
        logger.warning("跳过保存分析记录: 数据库不可用 (customer_id=%s)", customer_id)
        return

    from datetime import datetime
    ts = (created_at or "").strip() or datetime.now().isoformat(timespec="seconds")
    try:
        record = AnalysisHistory(
            customer_id=customer_id,
            customer_name=customer_name,
            result_json=json.dumps(result, ensure_ascii=False),
            created_at=ts,
        )
        session.add(record)
        session.commit()
        logger.info("已保存分析记录: customer_id=%s", customer_id)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.error("保存分析记录失败 (customer_id=%s): %s", customer_id, exc)
    finally:
        session.close()


def get_analysis_history(customer_id: str) -> List[dict]:
    """按客户查询画像分析历史(按 created_at 倒序)。

    Args:
        customer_id: 客户 ID。

    Returns:
        list[dict]: 每条含 customer_id / customer_name / result(解析后的 dict) /
                    created_at; 查无或数据库异常时返回 []。
    """
    session = _get_session()
    if session is None:
        logger.warning("跳过查询分析历史: 数据库不可用 (customer_id=%s)", customer_id)
        return []

    try:
        rows = (
            session.query(AnalysisHistory)
            .filter(AnalysisHistory.customer_id == customer_id)
            .order_by(AnalysisHistory.created_at.desc())
            .all()
        )
        history = []
        for row in rows:
            try:
                result = json.loads(row.result_json)
            except (json.JSONDecodeError, TypeError):
                result = {}
            history.append({
                "customer_id": row.customer_id,
                "customer_name": row.customer_name,
                "result": result,
                "created_at": row.created_at,
            })
        logger.info("查询分析历史: customer_id=%s, 共 %d 条", customer_id, len(history))
        return history
    except Exception as exc:  # noqa: BLE001
        logger.error("查询分析历史失败 (customer_id=%s): %s", customer_id, exc)
        return []
    finally:
        session.close()


# ============================================================
# 数据加载(JSON -> Pydantic 模型)
# ============================================================

def _read_json_file(file_path: Path) -> List[dict]:
    """读取 JSON 文件并返回解析后的列表。

    Args:
        file_path: JSON 文件绝对路径。

    Returns:
        list[dict]: 解析后的数据列表。

    Raises:
        FileNotFoundError: 文件不存在时抛出, 报错信息含清晰的中文说明。
    """
    if not file_path.exists():
        raise FileNotFoundError(
            f"数据文件不存在: {file_path} —— 请确认项目 data/ 目录下已放置该 mock 数据文件。"
        )
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError(f"数据文件格式错误(应为 JSON 数组): {file_path}")
        return data
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"数据文件 JSON 解析失败: {file_path} —— {exc}"
        ) from exc


def _load_models(file_path: Path, model_cls, label: str):
    """通用的模型列表加载器。

    Args:
        file_path: JSON 文件路径。
        model_cls: Pydantic 模型类(Customer / ChatRecord / Sales)。
        label: 日志中使用的数据名称。

    Returns:
        list[model_cls]: 校验通过后的模型实例列表。
    """
    raw_list = _read_json_file(file_path)
    models = [model_cls(**item) for item in raw_list]
    logger.info("已加载 %s %d 条: %s", label, len(models), file_path.name)
    return models


def load_customers() -> List[Customer]:
    """加载全部客户。

    Returns:
        list[Customer]: 客户模型列表。

    Raises:
        FileNotFoundError: data/mock_customers.json 缺失时抛出。
    """
    return _load_models(CUSTOMERS_FILE, Customer, "客户")


def _atomic_write_json(file_path: Path, data: List[dict]) -> None:
    """原子写入 JSON: 先写同目录临时文件, 再 os.replace 原子替换。

    避免写一半进程崩溃/断电导致 JSON 文件损坏(数据基线文件被写坏后
    整个流水线无法启动)。
    """
    import os
    import tempfile

    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=file_path.name + ".",
        suffix=".tmp",
        dir=str(file_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, file_path)
    except Exception:
        # 失败时清理临时文件, 不留下垃圾
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_customers(customers: List[Customer]) -> None:
    """持久化保存客户基础资料到 data/mock_customers.json(加锁 + 原子写)。"""
    raw_data = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in customers]
    with _customers_lock:
        _atomic_write_json(CUSTOMERS_FILE, raw_data)
    logger.info("客户列表已持久化保存: %d 家", len(customers))


def load_chat_records() -> List[ChatRecord]:
    """加载全部会话记录。

    Returns:
        list[ChatRecord]: 会话记录模型列表。

    Raises:
        FileNotFoundError: data/mock_chat_records.json 缺失时抛出。
    """
    return _load_models(CHAT_RECORDS_FILE, ChatRecord, "会话记录")


def load_sales() -> List[Sales]:
    """加载全部销售人员。

    Returns:
        list[Sales]: 销售人员模型列表。

    Raises:
        FileNotFoundError: data/mock_sales.json 缺失时抛出。
    """
    return _load_models(SALES_FILE, Sales, "销售人员")


def save_sales(sales_list: List[Sales]) -> None:
    """持久化保存销售人员列表到 data/mock_sales.json(加锁 + 原子写)。"""
    raw_data = [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in sales_list]
    with _sales_lock:
        _atomic_write_json(SALES_FILE, raw_data)
    logger.info("销售人员列表已持久化保存: %d 名", len(sales_list))


def add_sales_member(sales_item: Sales) -> Sales:
    """新增一名销售人员(读-改-写整体加锁, 防并发覆盖)。"""
    with _sales_lock:
        sales_list = load_sales()
        if any(s.sales_id == sales_item.sales_id for s in sales_list):
            raise ValueError(f"销售人员工号 {sales_item.sales_id} 已存在")
        sales_list.append(sales_item)
        save_sales(sales_list)
    return sales_item


def update_sales_member(sales_item: Sales) -> Sales:
    """更新一名销售人员的字段(按 sales_id 定位, 覆盖整条记录; 读-改-写加锁)。"""
    with _sales_lock:
        sales_list = load_sales()
        target = next((s for s in sales_list if s.sales_id == sales_item.sales_id), None)
        if target is None:
            raise ValueError(f"未找到工号为 {sales_item.sales_id} 的销售人员")
        sales_list = [sales_item if s.sales_id == sales_item.sales_id else s for s in sales_list]
        save_sales(sales_list)
    return sales_item


def delete_sales_member(sales_id: str) -> bool:
    """删除一名销售人员(不允许删除默认管理员 admin; 读-改-写加锁)。"""
    if sales_id == "admin":
        raise ValueError("系统默认管理员 admin 不允许删除")
    with _sales_lock:
        sales_list = load_sales()
        new_list = [s for s in sales_list if s.sales_id != sales_id]
        if len(new_list) == len(sales_list):
            return False
        save_sales(new_list)
    return True


def load_all() -> Tuple[List[Customer], List[ChatRecord], List[Sales]]:
    """一次加载三份 mock 数据。

    Returns:
        tuple[list[Customer], list[ChatRecord], list[Sales]]:
            (客户列表, 会话记录列表, 销售人员列表)。
    """
    customers = load_customers()
    chat_records = load_chat_records()
    sales = load_sales()
    logger.info("全量数据加载完成: 客户 %d 家, 会话 %d 条, 销售 %d 名",
                len(customers), len(chat_records), len(sales))
    return customers, chat_records, sales


def build_chat_map(records: List[ChatRecord]) -> Dict[str, List[ChatRecord]]:
    """将会话记录按 customer_id 分组, 供画像分析模块使用。

    Args:
        records: 会话记录列表。

    Returns:
        dict[str, list[ChatRecord]]: 以 customer_id 为键、该客户的会话记录列表为值;
                无任何会话的客户不会出现在结果中。
    """
    chat_map: Dict[str, List[ChatRecord]] = {}
    for record in records:
        chat_map.setdefault(record.customer_id, []).append(record)
    logger.info("会话分组完成: %d 个客户有会话记录", len(chat_map))
    return chat_map


def load_sales_experiences() -> List[SalesExperience]:
    """加载全部销售经验语料(供 RAG-lite 线索匹配检索)。

    Returns:
        list[SalesExperience]: 经验片段列表。

    Raises:
        FileNotFoundError: data/mock_sales_experience.json 缺失时抛出(中文报错)。
    """
    return _load_models(EXPERIENCE_FILE, SalesExperience, "销售经验片段")


def build_experience_map(experiences: List[SalesExperience]) -> Dict[str, List[SalesExperience]]:
    """将销售经验语料按 sales_id 分组, 便于检索器按销售聚合检索。

    Args:
        experiences: 销售经验片段列表。

    Returns:
        dict[str, list[SalesExperience]]: 以 sales_id 为键、该销售的经验片段列表为值;
                无经验的销售不会出现在结果中。
    """
    exp_map: Dict[str, List[SalesExperience]] = {}
    for exp in experiences:
        exp_map.setdefault(exp.sales_id, []).append(exp)
    logger.info("经验语料分组完成: %d 名销售有经验片段", len(exp_map))
    return exp_map


# ============================================================
# 预留接口(对接真实系统 —— 当前返回空列表)
# ============================================================

def fetch_crm_customers() -> List[Customer]:
    """从真实 CRM 系统拉取客户列表(预留接口)。

    Args:
        无。

    Returns:
        list[Customer]: CRM 客户列表; 当前为 mock 阶段, 返回空列表。

    Notes:
        对接真实系统时: 在此处调用 CRM 开放接口(如销售易/纷享销客/自建 CRM API),
        将返回的字段映射为 Customer 模型后返回。映射字段:
        customer_id <- CRM 客户 ID; customer_name <- 客户名称; industry <- 行业;
        city <- 所在城市; scale <- 规模; owner_sales_id <- 归属销售 ID;
        create_time <- 创建时间。
    """
    logger.info("fetch_crm_customers 被调用 —— 真实 CRM 对接尚未启用, 返回空列表")
    return []


def fetch_wework_chat(customer_id: str, days: int = 30) -> List[ChatRecord]:
    """拉取企业微信会话存档中某客户最近 N 天的聊天记录(预留接口)。

    Args:
        customer_id: 客户 ID。
        days: 拉取最近多少天的会话, 默认 30 天。

    Returns:
        list[ChatRecord]: 该客户的会话记录列表; 当前为 mock 阶段, 返回空列表。

    Notes:
        对接真实系统时: 在此处调用企业微信"会话内容存档"接口
        (需配置可信 IP、私钥解密等), 将拉取的聊天消息组装为 ChatRecord 后返回;
        角色映射: 销售会话成员 -> "销售", 外部联系人 -> "客户"。
    """
    logger.info("fetch_wework_chat(%s, days=%d) 被调用 —— 企微会话存档对接尚未启用, 返回空列表",
                customer_id, days)
    return []


def fetch_qikebao_customers() -> List[Customer]:
    """从企客宝 OpenAPI 拉取客户列表(主数据源, 可选)。

    - settings.qikebao_sync_enabled=False → 返回 [] (未启用);
    - qikebao_mock_mode=True → 读 data/real/qikebao_customers_sample.json;
    - 否则 → 调 adapters.qikebao_adapter.load_customers_from_qikebao();
    - 任何异常记日志并返回 [], 不中断调用方。

    Returns:
        list[Customer]: 企客宝客户列表(已映射, customer_id 带 QKB- 前缀)。
    """
    try:
        from config.settings import settings
        if not settings.qikebao_sync_enabled:
            return []
        from adapters.qikebao_adapter import load_customers_from_qikebao
        customers = load_customers_from_qikebao()
        logger.info("fetch_qikebao_customers: 共 %d 家", len(customers))
        return customers
    except Exception as exc:  # noqa: BLE001 —— 企客宝不可用返回空, 降级 mock
        logger.error("fetch_qikebao_customers 失败(%s), 返回空列表", exc)
        return []


def fetch_qikebao_chat(customer_ids: Optional[List[str]] = None) -> List[ChatRecord]:
    """从企客宝拉取聊天记录(P1; 未开通会话存档返回空)。

    Args:
        customer_ids: 客户 ID 列表(可空, 空则按全量客户拉取)。

    Returns:
        list[ChatRecord]: 会话记录列表。
    """
    try:
        from config.settings import settings
        if not settings.qikebao_sync_enabled or not settings.qikebao_sync_chat:
            return []
        customers = fetch_qikebao_customers()
        if customer_ids:
            wanted = set(customer_ids)
            customers = [c for c in customers if c.customer_id in wanted]
        from adapters.qikebao_adapter import load_chat_map_from_qikebao
        chat_map = load_chat_map_from_qikebao(customers)
        records: List[ChatRecord] = []
        for recs in chat_map.values():
            records.extend(recs)
        return records
    except Exception as exc:  # noqa: BLE001
        logger.error("fetch_qikebao_chat 失败(%s), 返回空列表", exc)
        return []


def load_pipeline_data() -> Tuple[List[Customer], List[ChatRecord], List[Sales], List[SalesExperience]]:
    """按优先级加载流水线数据(供 main.py 与 pipeline.py 共用)。

    优先级:
        1. 企客宝已启用且返回了客户 → fetch_qikebao_customers + fetch_qikebao_chat;
        2. 否则 → 现有 load_all() mock JSON。

    sales / experiences 始终走 mock(销售主数据来自 HR, 不随 CRM 客户走)。
    企客宝未启用或拉取失败时, 行为与现在完全一致(mock 跑通)。

    Returns:
        tuple[客户, 会话, 销售, 经验]: 四元组(与 load_all 相比多出经验)。
    """
    qikebao_customers = fetch_qikebao_customers()
    if qikebao_customers:
        # 企客宝优先: 客户来自企客宝, 会话来自企客宝(P1)或空
        chat_records = fetch_qikebao_chat([c.customer_id for c in qikebao_customers])
        sales = load_sales()
        experiences = load_sales_experiences()
        logger.info("流水线数据来源: qikebao(客户 %d 家, 会话 %d 条)",
                    len(qikebao_customers), len(chat_records))
        return qikebao_customers, chat_records, sales, experiences

    customers, chat_records, sales = load_all()
    experiences = load_sales_experiences()
    logger.info("流水线数据来源: mock(客户 %d 家, 会话 %d 条)", len(customers), len(chat_records))
    return customers, chat_records, sales, experiences


DEALS_FILE: Path = DATA_DIR / "mock_crm_deals.json"


def fetch_crm_deals(sales_id: Optional[str] = None, days: int = 90) -> List[dict]:
    """从 CRM 自动拉取成交商机记录。

    对接真实系统时调用 CRM 开放接口 (销售易/纷享销客/自建 CRM)。
    默认返回空列表 (由上层或专用模块提供商机数据)。

    Args:
        sales_id: 销售 ID, 可空 —— 空表示拉取全部销售的商机记录。
        days: 拉取最近 N 天的商机记录, 默认 90 天。

    Returns:
        list[dict]: 商机记录列表。
    """
    logger.info("fetch_crm_deals(sales_id=%s, days=%d) 真实接口尚未启用", sales_id, days)
    return []


def load_crm_deals(sales_id: Optional[str] = None) -> List[dict]:
    """从 data/mock_crm_deals.json 加载 CRM 历史商机数据。"""
    deals: List[dict] = []
    if DEALS_FILE.exists():
        try:
            with open(DEALS_FILE, "r", encoding="utf-8") as f:
                deals = json.load(f)
        except Exception as exc:
            logger.warning("读取 mock_crm_deals.json 失败: %s", exc)
            deals = []
    if sales_id:
        deals = [d for d in deals if d.get("sales_id") == sales_id]
    return deals


def generate_sales_experiences(deal_records: List[dict]) -> List[SalesExperience]:
    """把抓取到的成交商机记录提炼为标准经验片段(经验沉淀管道的核心提炼环节, 预留接口)。

    Args:
        deal_records: 商机记录列表, 格式与 fetch_crm_deals 的出参一致
                     (含 customer_name/industry/scale/budget/decision_maker/
                     pain_points/result 等字段)。

    Returns:
        list[SalesExperience]: 提炼出的标准经验片段列表;
                当前为 mock 阶段, 返回 []。

    Notes:
        生产环境实现: 将 deal_records 打包进 Prompt, 调用大模型(见 config.settings 的
        llm_api_base/llm_api_key/llm_model)摘要生成结构化的经验片段
        (痛点 / 打法 / 结果 三要素), 输出映射为 SalesExperience(sales_id/content/
        industry/outcome); 任一记录提炼失败(网络/超时/格式异常)时降级跳过该条,
        整体失败返回空列表, 绝不向上抛异常。
        与 load_sales_experiences() / build_experience_map() 一起构成
        "自动获取 → LLM 提炼 → 入库 → RAG 检索"的完整经验沉淀链路。
    """
    logger.info("generate_sales_experiences(%d 条商机) 被调用 —— LLM 提炼尚未启用, 返回空列表",
                len(deal_records))
    try:
        # mock 阶段: 暂不提炼, 直接返回空列表; 预留异常兜底保证不抛。
        return []
    except Exception as exc:  # noqa: BLE001 —— 提炼失败不允许让上层崩溃
        logger.error("生成销售经验片段失败: %s", exc)
        return []


# ============================================================
# 电话录音角色判定复核(RoleReview)持久化
# ============================================================


def save_role_review(
    call_id: str,
    customer_id: str,
    speaker_roles: Dict[str, str],
    method: str,
    confidence: float,
    notes: str = "",
    transcript: str = "",
) -> Optional[Dict]:
    """落库一条待人工复核的角色判定记录。

    Args:
        call_id: 通话 ID。
        customer_id: 客户 ID。
        speaker_roles: 当前判定 {"Speaker_0": "销售", ...}。
        method: 判定方法 metadata/heuristic/llm/manual。
        confidence: 判定置信度 0~1。
        notes: 判定依据。
        transcript: 分段转写快照(供人工判断)。

    Returns:
        dict | None: 保存成功返回行字典, 失败返回 None。
    """
    try:
        init_db()
        session = _get_session()
        if session is None:
            logger.warning("数据库不可用, 跳过保存角色复核记录")
            return None
        from datetime import datetime
        review = RoleReview(
            call_id=call_id,
            customer_id=customer_id,
            speaker_roles=json.dumps(speaker_roles, ensure_ascii=False),
            method=method,
            confidence=f"{confidence:.2f}",
            notes=notes,
            transcript=transcript,
            status="pending",
            resolved_roles="",
            created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        result = {
            "id": review.id, "call_id": review.call_id, "customer_id": review.customer_id,
            "speaker_roles": speaker_roles, "method": review.method,
            "confidence": confidence, "notes": review.notes,
            "transcript": review.transcript, "status": review.status,
            "created_at": review.created_at,
        }
        session.close()
        logger.info("已保存角色复核记录: call=%s, method=%s, conf=%.2f",
                    call_id, method, confidence)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("保存角色复核记录失败(%s): %s", call_id, exc)
        return None


def list_role_reviews(status: Optional[str] = None, limit: int = 100) -> List[Dict]:
    """查询角色复核记录列表(可按状态过滤, 按时间倒序)。

    Args:
        status: 状态过滤 pending/resolved; None 返回全部。
        limit: 最多返回条数。

    Returns:
        list[dict]: 复核记录列表。
    """
    try:
        init_db()
        session = _get_session()
        if session is None:
            return []
        q = session.query(RoleReview)
        if status:
            q = q.filter(RoleReview.status == status)
        rows = q.order_by(RoleReview.id.desc()).limit(limit).all()
        result = []
        for r in rows:
            try:
                roles = json.loads(r.speaker_roles or "{}")
            except (json.JSONDecodeError, TypeError):
                roles = {}
            result.append({
                "id": r.id, "call_id": r.call_id, "customer_id": r.customer_id,
                "speaker_roles": roles, "method": r.method,
                "confidence": float(r.confidence or 0), "notes": r.notes,
                "transcript": r.transcript, "status": r.status,
                "resolved_roles": (json.loads(r.resolved_roles) if r.resolved_roles else {}),
                "created_at": r.created_at,
            })
        session.close()
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("查询角色复核记录失败: %s", exc)
        return []


def resolve_role_review(review_id: int, resolved_roles: Dict[str, str]) -> bool:
    """人工确认某条复核记录, 写入最终角色并置为 resolved。

    Args:
        review_id: 复核记录主键。
        resolved_roles: 人工确认的最终角色 {"Speaker_0": "销售", ...}。

    Returns:
        bool: 成功 True, 失败 False。
    """
    try:
        init_db()
        session = _get_session()
        if session is None:
            return False
        row = session.query(RoleReview).filter(RoleReview.id == review_id).first()
        if row is None:
            session.close()
            return False
        row.resolved_roles = json.dumps(resolved_roles, ensure_ascii=False)
        row.status = "resolved"
        session.commit()
        session.close()
        logger.info("角色复核已确认: review_id=%d, roles=%s", review_id, resolved_roles)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("确认角色复核失败(%d): %s", review_id, exc)
        return False
