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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
            connect_args={"check_same_thread": False},   # 允许跨线程使用(FastAPI/调度场景)
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


def save_customers(customers: List[Customer]) -> None:
    """持久化保存客户基础资料到 data/mock_customers.json。"""
    CUSTOMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw_data = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in customers]
    with open(CUSTOMERS_FILE, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2)
    logger.info("客户列表已持久化保存: %d 家", len(customers))


def apply_bitable_sync_state(customers: List["Customer"]) -> List["Customer"]:
    """把飞书多维表格同步状态层合并到客户列表(用于业务查询感知飞书变更)。

    同步状态层(modules/bitable_sync 维护的 data/bitable_sync_state.json)记录
    飞书表格里「跟进状态 / 归属销售」相对本地 mock 基线的变更; 本函数把这些
    变更合并到传入的客户对象上, 返回新列表(不修改原对象, 也不改 mock 基线文件)。

    Args:
        customers: 客户模型列表。

    Returns:
        list[Customer]: 合并同步状态后的新列表。
    """
    state_file = PROJECT_ROOT / "data" / "bitable_sync_state.json"
    if not state_file.exists():
        return customers
    try:
        with open(state_file, encoding="utf-8") as fh:
            sync_state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return customers
    if not isinstance(sync_state, dict):
        return customers

    merged: List[Customer] = []
    for c in customers:
        st = sync_state.get(c.customer_name)
        if not isinstance(st, dict) or st.get("_remote_only"):
            merged.append(c)
            continue
        # 构造合并后的字段(不改原对象)
        new_data = c.model_dump() if hasattr(c, "model_dump") else dict(c)
        if st.get("follow_up_status") is not None:
            new_data["follow_up_status"] = st["follow_up_status"]
        if "owner_sales_id" in st:
            new_data["owner_sales_id"] = st.get("owner_sales_id")
        merged.append(Customer(**new_data))
    return merged


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
    """持久化保存销售人员列表到 data/mock_sales.json。"""
    SALES_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw_data = [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in sales_list]
    with open(SALES_FILE, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2)
    logger.info("销售人员列表已持久化保存: %d 名", len(sales_list))


def add_sales_member(sales_item: Sales) -> Sales:
    """新增一名销售人员。"""
    sales_list = load_sales()
    if any(s.sales_id == sales_item.sales_id for s in sales_list):
        raise ValueError(f"销售人员工号 {sales_item.sales_id} 已存在")
    sales_list.append(sales_item)
    save_sales(sales_list)
    return sales_item


def update_sales_member(sales_item: Sales) -> Sales:
    """更新一名销售人员的字段(按 sales_id 定位, 覆盖整条记录)。"""
    sales_list = load_sales()
    target = next((s for s in sales_list if s.sales_id == sales_item.sales_id), None)
    if target is None:
        raise ValueError(f"未找到工号为 {sales_item.sales_id} 的销售人员")
    sales_list = [sales_item if s.sales_id == sales_item.sales_id else s for s in sales_list]
    save_sales(sales_list)
    return sales_item


def delete_sales_member(sales_id: str) -> bool:
    """删除一名销售人员(不允许删除默认管理员 admin)。"""
    if sales_id == "admin":
        raise ValueError("系统默认管理员 admin 不允许删除")
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
