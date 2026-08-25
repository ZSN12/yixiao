# -*- coding: utf-8 -*-
"""SLA 超时预警 + 自动流转公海模块。

业务闭环:
    销售接单后若在 SLA 时限内未跟进, 系统:
    1. 预警阶段: 超 warning_hours 未跟进 -> 发送飞书卡片提醒销售尽快跟进;
    2. 超时阶段: 超 overdue_hours 未跟进 -> 自动释放归属(流转回公海),
       并通知原销售 + 主管, 让线索重新进入待分配池。

关键时间锚点:
    - assigned_at: 销售接单(归属分配)时间, 首次检测到 owner_sales_id 时记录;
    - last_follow_up_at: 销售最近一次跟进时间, 从 follow_up_notes 表推断
      (有跟进小记则取最新一条 created_at, 否则视为从未跟进)。

设计约束:
    - 独立状态层 data/sla_state.json, 不污染 mock_customers.json 测试基线
      (与 bitable_sync_state 同款 overlay 思路);
    - 规则确定性、可复现, 不依赖 LLM;
    - 流转公海通过「在 sla_state 里标记 override_owner=None + 通知」实现,
      不直接改 mock_customers.json 的 owner_sales_id 基线。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules import data_loader
from modules import follow_up_notes

logger = logging.getLogger(__name__)

SLA_STATE_FILE: Path = Path(__file__).resolve().parent.parent / "data" / "sla_state.json"

# SLA 阈值(小时): 演示环境用短阈值, 生产可按需调大
DEFAULT_WARNING_HOURS: int = 24   # 接单后 24h 未跟进 -> 预警
DEFAULT_OVERDUE_HOURS: int = 72   # 接单后 72h 未跟进 -> 超时流转公海


# ============================================================
# SLA 状态层读写(独立于 mock 基线)
# ============================================================

def _load_sla_state() -> Dict[str, Any]:
    """加载 SLA 状态层(data/sla_state.json)。"""
    if not SLA_STATE_FILE.exists():
        return {}
    try:
        with open(SLA_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取 SLA 状态层失败: %s", exc)
        return {}


def _save_sla_state(state: Dict[str, Any]) -> None:
    """持久化 SLA 状态层。"""
    SLA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SLA_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def _now() -> datetime:
    return datetime.now()


def _parse_ts(ts: str) -> Optional[datetime]:
    """解析 ISO 时间字符串, 失败返回 None。

    兼容带时区偏移(如 +08:00 / Z)与任意位数微秒的输入; 外部同步/回调写入的
    时间格式不固定, 这里做宽泛解析, 解析不出时由调用方按 0 处理。
    """
    if not ts:
        return None
    text = ts.strip()
    # 统一替换空格分隔与 Z 时区标记
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # 去掉超过 6 位的微秒(时间库只支持 6 位)
    if "." in text:
        base, frac = text.split(".", 1)
        digits = ""
        for ch in frac:
            if ch.isdigit():
                digits += ch
            else:
                break
        tail = frac[len(digits):]
        text = f"{base}.{digits[:6]}{tail}"

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ============================================================
# 关键时间锚点提取
# ============================================================

def _last_follow_up_at(customer_id: str) -> Optional[str]:
    """从 follow_up_notes 推断客户最近跟进时间(ISO 字符串, 无则 None)。"""
    try:
        notes = follow_up_notes.list_notes(customer_id=customer_id, limit=1)
        if notes:
            return notes[0].get("created_at")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取跟进小记失败(%s): %s", customer_id, exc)
    return None


def _effective_owner(customer: Any) -> Optional[str]:
    """取客户「有效归属销售」(合并飞书同步状态后的 owner_sales_id)。

    兼容多种销售 ID 格式(S001 / admin / 企客宝 user_id 等): 只排除历史遗留的
    占位文本状态, 不做 S 前缀等格式猜测, 避免接入新数据源后 SLA 静默失效。
    """
    # customer 可能是 Customer 对象或 dict
    if hasattr(customer, "owner_sales_id"):
        owner = customer.owner_sales_id
    else:
        owner = customer.get("owner_sales_id")
    if not owner:
        return None
    owner = str(owner).strip()
    # 历史代码可能把「跟进状态」误写入 owner_sales_id 字段, 这些不是真实销售 ID。
    status_placeholders = (
        "已接单", "待改派", "待跟进", "已电话沟通", "已成单", "已转交",
    )
    if owner in status_placeholders or owner in ("", "None", "null"):
        return None
    return owner


# ============================================================
# SLA 检测核心
# ============================================================

def check_sla(
    warning_hours: int = DEFAULT_WARNING_HOURS,
    overdue_hours: int = DEFAULT_OVERDUE_HOURS,
    apply_changes: bool = True,
) -> Dict[str, Any]:
    """检测所有已归属客户的 SLA 状态, 返回预警/超时名单。

    检测逻辑:
        对每个有归属销售的客户:
        1. 首次检测到归属 -> 记录 assigned_at 到 sla_state;
        2. 计算 elapsed = now - assigned_at;
        3. elapsed > overdue_hours 且从未跟进 -> overdue(流转公海);
           elapsed > warning_hours 且从未跟进 -> warning(预警);
           否则 ok。

    Args:
        warning_hours: 预警阈值(小时)。
        overdue_hours: 超时阈值(小时)。
        apply_changes: True 时实际写入状态层 + 执行流转(释放归属), False 只读检测。

    Returns:
        dict: {warning: [...], overdue: [...], ok: [...]}, 每项为
            {customer_id, customer_name, owner_sales_id, assigned_at,
             last_follow_up_at, elapsed_hours, sla_status}。
    """
    customers = data_loader.apply_bitable_sync_state(data_loader.load_customers())
    sla_state = _load_sla_state()
    now = _now()

    warning_list: List[Dict[str, Any]] = []
    overdue_list: List[Dict[str, Any]] = []
    ok_list: List[Dict[str, Any]] = []
    changed = False

    for c in customers:
        owner = _effective_owner(c)
        if not owner:
            # 无归属 -> 不参与 SLA(公海线索不占 SLA)
            continue

        cid = c.customer_id if hasattr(c, "customer_id") else c["customer_id"]
        cname = c.customer_name if hasattr(c, "customer_name") else c["customer_name"]

        # 只读检测(apply_changes=False)不应修改状态层:
        # 用 get 而非 setdefault, 后续所有写操作统一用 apply_changes 门控。
        entry = sla_state.get(cid, {})
        # 记录归属时间(首次见到该 owner 时)
        if apply_changes and (entry.get("assigned_at") is None or entry.get("owner") != owner):
            entry = sla_state.setdefault(cid, {})
            entry["assigned_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
            entry["owner"] = owner
            changed = True

        assigned_at = _parse_ts(entry.get("assigned_at", ""))
        last_follow = _last_follow_up_at(cid)

        # 记录最后跟进时间(若有)
        if apply_changes and last_follow and last_follow != entry.get("last_follow_up_at"):
            entry = sla_state.setdefault(cid, {})
            entry["last_follow_up_at"] = last_follow
            changed = True
        last_follow = entry.get("last_follow_up_at") or last_follow

        # 计算超时
        elapsed = now - assigned_at if assigned_at else timedelta(0)
        elapsed_hours = round(elapsed.total_seconds() / 3600, 2)

        has_followed = bool(last_follow)

        if not has_followed and elapsed_hours > overdue_hours:
            sla_status = "overdue"
            if apply_changes:
                entry = sla_state.setdefault(cid, {})
                entry["sla_status"] = "overdue"
                # 超时流转公海: 在 sla_state 里标记 override_owner=None(释放归属)
                entry["override_owner"] = None
                entry["released_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
                entry["release_reason"] = f"SLA 超时: 接单 {overdue_hours}h 内未跟进, 自动流转公海"
                changed = True
            overdue_list.append({
                "customer_id": cid,
                "customer_name": cname,
                "owner_sales_id": owner,
                "assigned_at": entry.get("assigned_at"),
                "last_follow_up_at": last_follow,
                "elapsed_hours": elapsed_hours,
                "sla_status": sla_status,
            })
        elif not has_followed and elapsed_hours > warning_hours:
            sla_status = "warning"
            if apply_changes:
                entry = sla_state.setdefault(cid, {})
                entry["sla_status"] = "warning"
                entry.pop("override_owner", None)  # 预警阶段不释放
                changed = True
            warning_list.append({
                "customer_id": cid,
                "customer_name": cname,
                "owner_sales_id": owner,
                "assigned_at": entry.get("assigned_at"),
                "last_follow_up_at": last_follow,
                "elapsed_hours": elapsed_hours,
                "sla_status": sla_status,
            })
        else:
            sla_status = "ok"
            if apply_changes:
                entry = sla_state.setdefault(cid, {})
                entry["sla_status"] = "ok"
                entry.pop("override_owner", None)
                if entry.get("last_follow_up_at") != last_follow:
                    entry["last_follow_up_at"] = last_follow
                    changed = True
            ok_list.append({
                "customer_id": cid,
                "customer_name": cname,
                "owner_sales_id": owner,
                "assigned_at": entry.get("assigned_at"),
                "last_follow_up_at": last_follow,
                "elapsed_hours": elapsed_hours,
                "sla_status": sla_status,
            })

    if apply_changes and changed:
        _save_sla_state(sla_state)

    logger.info(
        "SLA 检测完成: warning=%d, overdue=%d, ok=%d",
        len(warning_list), len(overdue_list), len(ok_list),
    )
    return {"warning": warning_list, "overdue": overdue_list, "ok": ok_list}


def released_to_pool(customer_id: str) -> bool:
    """判断客户是否已被 SLA 超时流转公海(override_owner=None)。"""
    sla_state = _load_sla_state()
    entry = sla_state.get(customer_id, {})
    return "override_owner" in entry and entry.get("override_owner") is None


def get_sla_overlay(customer_id: str) -> Optional[Dict[str, Any]]:
    """返回某客户的 SLA 覆盖层(供 apply 到客户列表时释放归属)。"""
    sla_state = _load_sla_state()
    entry = sla_state.get(customer_id, {})
    if "override_owner" in entry:
        return {"owner_sales_id": entry.get("override_owner")}
    return None


def apply_sla_overlay(customers: List[Any]) -> List[Any]:
    """把 SLA 超时流转(override_owner)合并到客户列表, 返回新列表。

    不修改原对象, 不改 mock 基线; 只对 sla_state 里 override_owner 的客户
    覆盖其 owner_sales_id 为 None(释放回公海)。
    """
    sla_state = _load_sla_state()
    merged: List[Any] = []
    for c in customers:
        cid = c.customer_id if hasattr(c, "customer_id") else c["customer_id"]
        entry = sla_state.get(cid)
        if isinstance(entry, dict) and "override_owner" in entry:
            new_data = c.model_dump() if hasattr(c, "model_dump") else dict(c)
            new_data["owner_sales_id"] = entry.get("override_owner")
            if hasattr(c, "__class__"):
                merged.append(c.__class__(**new_data))
            else:
                merged.append(new_data)
        else:
            merged.append(c)
    return merged
