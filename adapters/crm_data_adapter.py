# -*- coding: utf-8 -*-
"""真实数据适配层(adapters): 把真实系统导出(CRM CSV / 企微会话存档 JSON)
映射为与 mock 数据严格同构的模型, 做到"接真实系统调用方零改动"。

背景: data_loader 预留的 fetch_crm_customers / fetch_wework_chat / fetch_crm_deals
目前是空桩(mock 阶段返回 [])。本模块实现第一版可运行的"真实数据适配实证":
- 用 CSV 模拟真实 CRM 的客户导出(带中文表头 + 故意脏数据);
- 用 JSON 模拟企业微信"会话内容存档"的导出;
- 适配层输出与 data/ 下 mock 数据严格同构: 客户 → data_loader.Customer,
  会话 → data_loader.ChatRecord, 从而 analyze_customers_batch / assign_leads /
  main 等既有流水线一行不改即可消化真实来源数据。

工程边界(合理取舍):
- 销售主数据(sales)与经验语料(experiences)继续用 mock —— 销售主数据通常
  来自 HR/组织系统而非 CRM 客户导出, 不随客户 CSV 走(真实边界)。
- 字段映射与 data_loader.fetch_crm_customers docstring 逐字对齐:
  customer_id <- 客户ID; customer_name <- 客户名称; industry <- 行业;
  city <- 所在城市; scale <- 规模; owner_sales_id <- 归属销售ID;
  create_time <- 创建时间。
- 零第三方依赖: 仅用标准库 csv / json / logging / datetime。

用法:
    python adapters/crm_data_adapter.py            # 重新生成演示数据 + 跑通端到端实证
    from adapters.crm_data_adapter import run_real_data_demo
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import List

# 项目根: 本文件位于 <项目根>/adapters/crm_data_adapter.py
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
# 直接运行本文件(python adapters/crm_data_adapter.py)时, 把项目根加入 sys.path
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.data_loader import ChatMessage, ChatRecord, Customer

logger = logging.getLogger(__name__)

# 演示数据文件(作为演示资产保留在仓库, 体积小)
DEFAULT_CSV_PATH: Path = PROJECT_ROOT / "data" / "real" / "crm_customers.csv"
DEFAULT_CHAT_JSON_PATH: Path = PROJECT_ROOT / "data" / "real" / "wework_chat_export.json"


# ============================================================
# 1. 生成真实感 CRM 导出 CSV(含故意脏数据)
# ============================================================

def generate_sample_crm_csv(csv_path: str) -> None:
    """生成 4-5 家客户的真实感 CRM 导出 CSV(中文表头, 含故意脏数据)。

    脏数据设计(用于验证兜底逻辑):
    - 1 家缺 city(空值)        → 适配时兜底为 "未知";
    - 1 家 industry 为空/未知   → 适配时兜底为 "其他";
    - 2 家无归属(owner_sales_id 空) → 便于走线索分配;
    - 行业覆盖 2-3 个与 mock 客户一致的行业(智能制造/医疗器械/软件服务等)。

    Args:
        csv_path: 输出 CSV 文件路径(相对路径以项目根解析)。

    Returns:
        None

    Notes:
        生成的 CSV 作为演示资产保留在 data/real/, 供 load_customers_from_csv
        直接读取; 真实 CRM 导出的列名/编码与本文件对齐即可零改动接入。
    """
    out = Path(csv_path)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    header = ["客户ID", "客户名称", "行业", "城市", "规模", "归属销售ID", "创建时间"]
    rows = [
        # 正常完整行(归属)
        ["R001", "宁波中科精密制造有限公司", "智能制造", "宁波", "中大型", "S001", "2024-06-15"],
        # 缺 city(空值) —— 测试兜底 → "未知"
        ["R002", "无锡恒信自动化设备有限公司", "智能制造", "", "中型", "S003", "2024-07-03"],
        # industry 为 "未知" —— 测试兜底 → "其他"
        ["R003", "南通瑞达工业器材有限公司", "未知", "南通", "小型", "", "2024-08-20"],
        # 行业与 mock 一致 + 无归属(便于分配)
        ["R004", "青岛明远软件服务有限公司", "软件服务", "青岛", "中型", "", "2024-09-12"],
        # 完整行 + 无归属(便于分配)
        ["R005", "嘉兴华康医疗器械有限公司", "医疗器械", "嘉兴", "中大型", "", "2024-10-08"],
    ]
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    logger.info("已生成示例 CRM 导出 CSV: %s(共 %d 行数据, 含脏数据行)", out, len(rows))


# ============================================================
# 2. CSV → List[Customer](字段映射 + 脏数据兜底)
# ============================================================

def _normalize_industry(raw: str) -> str:
    """行业兜底: 空值/"未知" → "其他"。"""
    value = (raw or "").strip()
    if not value or value in ("未知", "不适用", "-"):
        return "其他"
    return value


def _normalize_city(raw: str) -> str:
    """城市兜底: 空值 → "未知"。"""
    value = (raw or "").strip()
    return value if value else "未知"


def _normalize_date(raw: str) -> str:
    """创建时间兜底: 缺失/非法 → 当前日期(YYYY-MM-DD)。"""
    value = (raw or "").strip()
    if value:
        return value
    return date.today().isoformat()


def load_customers_from_csv(csv_path: str) -> List[Customer]:
    """从 CRM 导出 CSV 加载客户列表(字段映射 + 脏数据兜底)。

    字段映射(与 data_loader.fetch_crm_customers docstring 逐字对齐):
        customer_id   <- 客户ID
        customer_name <- 客户名称
        industry      <- 行业(空/未知 → "其他")
        city          <- 城市(空 → "未知")
        scale         <- 规模
        owner_sales_id<- 归属销售ID(空 → None, 即无归属走分配)
        create_time   <- 创建时间(缺失 → 当前日期)

    Args:
        csv_path: CSV 文件路径(相对路径以项目根解析)。

    Returns:
        list[Customer]: 客户模型列表; 单行解析失败跳过并记日志, 不中断整体;
                保证返回的每条 Customer 都能被流水线消费。

    Raises:
        FileNotFoundError: CSV 文件不存在时抛出(中文报错)。
    """
    csv_file = Path(csv_path)
    if not csv_file.is_absolute():
        csv_file = PROJECT_ROOT / csv_file
    if not csv_file.exists():
        raise FileNotFoundError(
            f"CRM 导出 CSV 不存在: {csv_file} —— 先调用 generate_sample_crm_csv 生成, "
            f"或传入真实导出的 CSV 路径。"
        )

    customers: List[Customer] = []
    with open(csv_file, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for line_no, row in enumerate(reader, start=2):   # 2 = 表头下一行
            try:
                customers.append(Customer(
                    customer_id=str(row.get("客户ID") or "").strip(),
                    customer_name=str(row.get("客户名称") or "").strip(),
                    industry=_normalize_industry(row.get("行业") or ""),
                    city=_normalize_city(row.get("城市") or ""),
                    scale=str(row.get("规模") or "").strip() or "未知",
                    owner_sales_id=(row.get("归属销售ID") or "").strip() or None,
                    create_time=_normalize_date(row.get("创建时间") or ""),
                ))
            except Exception as exc:  # noqa: BLE001 —— 单行失败跳过, 不中断整体
                logger.warning("CSV 第 %d 行解析失败, 跳过该行: %s", line_no, exc)
    logger.info("CSV 客户加载完成: %s, 共 %d 家(含脏数据兜底)", csv_file, len(customers))
    return customers


# ============================================================
# 3. 企微会话存档 JSON → List[ChatRecord]
# ============================================================

def load_chat_records_from_export(json_path: str) -> List[ChatRecord]:
    """从企微会话存档导出 JSON 加载会话记录(角色映射为 销售|客户)。

    JSON 结构示例(模拟"会话内容存档"导出, 顶层数组):
        [
          {
            "record_id": "W001",
            "customer_id": "R001",
            "sales_id": "S001",
            "chat_time": "2024-10-12",
            "messages": [
              {"role": "员工", "name": "张伟", "content": "王总您好..."},
              {"role": "外部联系人", "name": "王总", "content": "预算方面..."}
            ]
          }
        ]
    角色映射: "员工" → 销售; "外部联系人" → 客户; 其他角色名保持原样。

    Args:
        json_path: 会话存档 JSON 文件路径(相对路径以项目根解析)。

    Returns:
        list[ChatRecord]: 会话记录列表; 单条解析失败跳过并记日志, 不中断整体。

    Raises:
        FileNotFoundError: JSON 文件不存在时抛出(中文报错)。
    """
    chat_file = Path(json_path)
    if not chat_file.is_absolute():
        chat_file = PROJECT_ROOT / chat_file
    if not chat_file.exists():
        raise FileNotFoundError(
            f"企微会话存档 JSON 不存在: {chat_file} —— 先调用生成函数落盘, "
            f"或传入真实导出的 JSON 路径。"
        )

    with open(chat_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"会话存档 JSON 应为数组: {chat_file}")

    records: List[ChatRecord] = []
    for idx, item in enumerate(data):
        try:
            raw_messages = item.get("messages") or []
            messages = []
            for msg in raw_messages:
                role = str(msg.get("role") or "").strip()
                # 角色映射: 员工 → 销售; 外部联系人 → 客户
                if role == "员工":
                    role = "销售"
                elif role == "外部联系人":
                    role = "客户"
                messages.append(ChatMessage(role=role, content=str(msg.get("content") or "")))
            records.append(ChatRecord(
                record_id=str(item.get("record_id") or f"W{idx + 1:03d}"),
                customer_id=str(item.get("customer_id") or ""),
                sales_id=(str(item.get("sales_id") or "").strip()) or None,
                chat_time=str(item.get("chat_time") or ""),
                messages=messages,
            ))
        except Exception as exc:  # noqa: BLE001 —— 单条失败跳过, 不中断整体
            logger.warning("会话存档第 %d 条解析失败, 跳过: %s", idx + 1, exc)
    logger.info("企微会话加载完成: %s, 共 %d 条会话", chat_file, len(records))
    return records


# ============================================================
# 4. 演示数据生成(落盘 data/real/ 作为资产) + 端到端实证
# ============================================================

def generate_sample_chat_export(json_path: str) -> None:
    """生成模拟企微会话存档导出 JSON(与 mock 聊天记录同风格关键词)。

    Args:
        json_path: 输出 JSON 文件路径(相对路径以项目根解析)。

    Returns:
        None
    """
    out = Path(json_path)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    data = [
        {
            "record_id": "W001",
            "customer_id": "R001",
            "sales_id": "S001",
            "chat_time": "2024-10-12",
            "messages": [
                {"role": "员工", "name": "张伟", "content": "王总您好，贵司产线改造的智能制造方案初稿已发您邮箱。"},
                {"role": "外部联系人", "name": "王总", "content": "方案收到了，整体符合预期。我们今年设备采购预算大概400万，希望尽快立项推进。"},
            ],
        },
        {
            "record_id": "W002",
            "customer_id": "R002",
            "sales_id": "S003",
            "chat_time": "2024-10-15",
            "messages": [
                {"role": "员工", "name": "王强", "content": "陈总，自动化产线的报价单和方案对比表发您了，请您过目。"},
                {"role": "外部联系人", "name": "陈总", "content": "看了下报价，感觉偏贵了，我们还在跟另外一家竞品对比，暂时缓一缓。"},
            ],
        },
        {
            "record_id": "W003",
            "customer_id": "R004",
            "sales_id": None,
            "chat_time": "2024-10-18",
            "messages": [
                {"role": "外部联系人", "name": "刘总", "content": "我们CRM系统采购需求已经立项，希望尽快收到你们的方案和报价。"},
                {"role": "员工", "name": "李娜", "content": "好的刘总，需求文档我们已收到，三天内出完整方案，合同条款也可以提前对齐。"},
            ],
        },
        {
            "record_id": "W004",
            "customer_id": "R005",
            "sales_id": None,
            "chat_time": "2024-10-20",
            "messages": [
                {"role": "员工", "name": "张伟", "content": "吴主任您好，医疗影像设备方案和预算清单已发您，预算大概200万区间。"},
                {"role": "外部联系人", "name": "吴主任", "content": "方案可以，不过要等设备科审批，时间上可能要到下个月。"},
            ],
        },
    ]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    logger.info("已生成示例企微会话存档 JSON: %s(共 %d 条会话)", out, len(data))


def run_real_data_demo(csv_path: str, chat_json_path: str) -> dict:
    """端到端实证: 真实来源数据 → 与 mock 完全相同的流水线路径消化。

    流程:
    1. CSV → List[Customer](含脏数据兜底);
    2. 会话 JSON → List[ChatRecord](角色映射);
    3. 与 mock 流水线同一调用路径消化:
       - build_chat_map(同 data_loader 的分组函数) → analyze_customers_batch(规则引擎);
       - 无归属客户 → assign_leads(同 mock 的分配函数, 销售/经验语料沿用 mock
         主数据 —— 销售主数据不常从 CRM 客户导出, 这是合理的真实边界);
    4. 返回实证摘要 {customer_count, analyzed, assignments, note}。

    Args:
        csv_path: CRM 导出 CSV 路径。
        chat_json_path: 企微会话存档 JSON 路径。

    Returns:
        dict: {"customer_count": int, "analyzed": int, "assignments": int,
               "note": "调用方零改动"}; 任一环节内部已有容错, 不抛异常。
    """
    from modules import data_loader
    from modules.lead_assigner import assign_leads
    from modules.profile_analyzer import analyze_customers_batch

    # 1) 真实来源数据适配
    customers = load_customers_from_csv(csv_path)
    chat_records = load_chat_records_from_export(chat_json_path)
    # 2) 与 mock 完全相同的调用路径
    chat_map = data_loader.build_chat_map(chat_records)
    analysis = analyze_customers_batch(customers, chat_map)
    # 3) 分配: 无归属客户走分配(销售/经验沿用 mock 主数据)
    unassigned = [c for c in customers if not c.owner_sales_id]
    sales = data_loader.load_sales()
    experiences = data_loader.load_sales_experiences()
    assignments = assign_leads(unassigned, sales, experiences, analysis_results=analysis)

    logger.info("真实数据实证完成: 客户 %d 家, 分析 %d 家, 分配 %d 条",
                len(customers), len(analysis), len(assignments))
    return {
        "customer_count": len(customers),
        "analyzed": len(analysis),
        "assignments": len(assignments),
        "note": "调用方零改动",
    }


# ============================================================
# 命令行入口(演示): 生成资产 + 跑通实证
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    generate_sample_crm_csv(str(DEFAULT_CSV_PATH))
    generate_sample_chat_export(str(DEFAULT_CHAT_JSON_PATH))
    summary = run_real_data_demo(str(DEFAULT_CSV_PATH), str(DEFAULT_CHAT_JSON_PATH))
    print("\n=== 真实数据适配实证 ===")
    print(f"客户数: {summary['customer_count']} | 分析数: {summary['analyzed']} | "
          f"分配数: {summary['assignments']} | 说明: {summary['note']}")