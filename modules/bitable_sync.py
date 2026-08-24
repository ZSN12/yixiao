# -*- coding: utf-8 -*-
"""飞书多维表格(Bitable) <-> 易销系统 双向同步模块。

架构说明(诚实约束):
    飞书开放平台没有为 Bitable 记录变更提供官方 Webhook/事件推送(经
    `lark-cli event list` 核实: 事件清单中不存在 base/bitable/record 事件)。
    同时后端自建应用 cli_aa02dfc240f8dd01 未开通 bitable 相关 scope,
    因此本模块通过「子进程调用 lark-cli(已授权的 cli_aa01b496c0b81bd5,
    用户态 user)」来读写多维表格 —— 这是当前唯一稳定可行的通道。

    同步策略(拉取式全量比对, 非秒级事件):
    - 反向同步(飞书 -> 易销): lark-cli base +record-list 拉取全量记录,
      与本地客户数据逐条比对, 把飞书表格里的「跟进状态 / 归属销售 / 新增线索」
      差异回写到本地 mock_customers.json。
    - 正向同步(易销 -> 飞书): 把本地最新客户资料 + AI 画像 + 跟进状态,
      通过 lark-cli base +record-upsert 批量推回 Bitable。

    同步入口:
    - API:  POST /api/sync/bitable/pull | /push | (both)
    - CLI:  python -m modules.bitable_sync --pull/--push/--both [--dry-run]

    依赖: 系统 PATH 中存在 `lark-cli` 命令, 且已登录(cli_aa01b496c0b81bd5)。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

LARK_CLI = "lark-cli"

# 跟进状态归一化映射(飞书表格里可能带近义词)
_STATUS_ALIASES = {
    "待跟进": "待跟进",
    "已接单跟进": "已接单跟进",
    "已接单": "已接单跟进",
    "已电话沟通": "已电话沟通",
    "已成单": "已成单",
    "已转交/改派": "已转交/改派",
    "已转交改派": "已转交/改派",
    "已转交": "已转交/改派",
}


def _run_lark_cli(args: List[str]) -> Optional[dict]:
    """调用 lark-cli 子进程并解析 JSON 输出。

    Args:
        args: lark-cli 的参数列表(不含 "lark-cli" 本身)。

    Returns:
        dict: 解析后的 JSON; 失败返回 None。
    """
    cmd = [LARK_CLI] + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        logger.error("未找到 lark-cli 命令, 请确认已安装并加入 PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.error("lark-cli 调用超时: %s", " ".join(cmd))
        return None

    if proc.returncode != 0:
        logger.error("lark-cli 调用失败(%s): %s", proc.returncode, proc.stderr.strip()[:300])
        # 尝试从 stdout 解析(部分命令错误也输出 JSON 到 stdout)
    try:
        data = json.loads(proc.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        logger.error("lark-cli 输出非 JSON: %s", proc.stdout.strip()[:200])
        return None

    if data.get("ok") is False:
        err = data.get("error") or {}
        logger.error("lark-cli 业务失败: code=%s msg=%s", err.get("code"), err.get("message"))
        return None
    return data


def _record_list_raw() -> Optional[dict]:
    """拉取「客户线索池」全量记录(通过 lark-cli base +record-list --as user)。"""
    base_token = settings.feishu_base_token
    table_id = settings.feishu_base_leads_table
    if not (base_token and table_id):
        logger.warning("未配置 feishu_base_token / feishu_base_leads_table")
        return None
    return _run_lark_cli([
        "base", "+record-list",
        "--base-token", base_token,
        "--table-id", table_id,
        "--as", "user",
        "--format", "json",
    ])


def fetch_bitable_leads() -> List[Dict[str, Any]]:
    """拉取 Bitable 客户线索池记录, 解析为字段字典列表。

    Returns:
        list[dict]: 每条含 record_id + 归一化字段 {customer_name, industry,
            intention_level, churn_risk, city, scale, owner_sales_text,
            core_demands, suggestion, follow_up_status}。
    """
    data = _record_list_raw()
    if not data:
        return []

    inner = data.get("data") or {}
    rows = inner.get("data") if isinstance(inner.get("data"), list) else []
    record_ids = inner.get("record_id_list") or []
    field_ids = inner.get("field_id_list") or []
    # fields 是与 field_id_list 一一对应的「字段名」列表(纯字符串数组)
    field_names = inner.get("fields") or []

    # field_id -> field_name 映射
    id2name: Dict[str, str] = {
        field_ids[i]: field_names[i]
        for i in range(min(len(field_ids), len(field_names)))
    }

    results: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        record_id = record_ids[idx] if idx < len(record_ids) else None
        fields: Dict[str, Any] = {}
        for j, val in enumerate(row):
            fid = field_ids[j] if j < len(field_ids) else None
            name = id2name.get(fid, fid) if fid else None
            if name:
                fields[name] = val

        results.append({
            "record_id": record_id,
            "customer_name": _extract_text(fields.get("客户名称")),
            "industry": _normalize_select(fields.get("行业")),
            "intention_level": _normalize_select(fields.get("意向等级")),
            "churn_risk": _normalize_select(fields.get("流失风险")),
            "city": _extract_text(fields.get("城市")),
            "scale": _normalize_select(fields.get("企业规模")),
            "owner_sales_text": _extract_text(fields.get("归属销售")),
            "core_demands": _extract_text(fields.get("AI核心诉求")),
            "suggestion": _extract_text(fields.get("AI跟进策略建议")),
            "follow_up_status": _normalize_status(fields.get("跟进状态")),
        })
    logger.info("已从 Bitable 拉取客户记录 %d 条", len(results))
    return results


def _extract_text(value: Any) -> str:
    """提取飞书文本类字段为纯字符串。"""
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        if isinstance(value[0], dict):
            return "".join(str(v.get("text", "")) for v in value)
        return "".join(str(v) for v in value)
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value)


def _normalize_select(value: Any) -> Any:
    """飞书 select 字段返回数组(如 ["高"]), 归一化为纯文本。"""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _normalize_status(raw: Any) -> str:
    """把飞书表格里的跟进状态归一化为本地枚举。"""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    raw = str(raw or "").strip()
    return _STATUS_ALIASES.get(raw, raw or "待跟进")


def parse_sales_from_text(text: str) -> Optional[str]:
    """从「归属销售」文本(如 "张伟 (S001)")解析 sales_id。"""
    if not text:
        return None
    m = re.search(r"\(?\s*(S\d{3})\s*\)?", str(text))
    if m:
        return m.group(1)
    m = re.search(r"\b(S\d{3})\b", str(text))
    return m.group(1) if m else None


# 同步状态持久化文件(独立于 mock_customers.json, 避免污染测试基线)
SYNC_STATE_FILE = "data/bitable_sync_state.json"


def _load_sync_state() -> Dict[str, Any]:
    """加载同步状态文件, 结构: {customer_name: {follow_up_status, owner_sales_id}}。"""
    from pathlib import Path
    p = Path(SYNC_STATE_FILE)
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sync_state(state: Dict[str, Any]) -> None:
    """持久化同步状态文件。"""
    from pathlib import Path
    p = Path(SYNC_STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 反向同步: 飞书 -> 本地(同步状态层)
# ============================================================

def pull_from_bitable(dry_run: bool = False) -> Dict[str, Any]:
    """反向同步: 拉取飞书表格, 比对本地, 把差异写入同步状态层。

    设计: 不修改 data/mock_customers.json(它是测试基线), 而是把飞书表格里的
    「跟进状态 / 归属销售 / 新增线索」同步到独立的 data/bitable_sync_state.json,
    业务查询时由上层合并。这样双向同步既能真实落地, 又不破坏测试基线。
    """
    remote = fetch_bitable_leads()
    if not remote:
        return {"direction": "pull", "pulled": 0, "changes": [], "error": "拉取失败或为空"}

    from modules import data_loader
    local = data_loader.load_customers()
    local_by_name: Dict[str, Any] = {c.customer_name: c for c in local}
    local_by_id: Dict[str, Any] = {c.customer_id: c for c in local}

    # 现有同步状态
    sync_state = _load_sync_state()

    status_updated = 0
    owner_updated = 0
    new_customers = 0
    changes: List[Dict[str, Any]] = []

    for r in remote:
        cname = (r.get("customer_name") or "").strip()
        if not cname:
            continue
        local_c = local_by_name.get(cname)
        new_status = r.get("follow_up_status") or "待跟进"
        new_owner = parse_sales_from_text(r.get("owner_sales_text") or "")

        if local_c is None:
            # 飞书新增的线索(本地 mock 无此客户) -> 记录到同步状态
            new_customers += 1
            changes.append({
                "type": "new_customer",
                "customer_name": cname,
                "detail": f"新增线索: {cname} (行业: {r.get('industry') or '未知'})",
            })
            if not dry_run:
                sync_state.setdefault(cname, {})
                sync_state[cname]["_remote_only"] = True
                sync_state[cname]["industry"] = r.get("industry") or ""
                sync_state[cname]["city"] = r.get("city") or ""
                sync_state[cname]["follow_up_status"] = new_status
                sync_state[cname]["owner_sales_id"] = new_owner
            continue

        # 本地客户当前状态(优先取同步状态, 其次 mock 基线)
        prev = sync_state.get(cname, {})
        prev_status = prev.get("follow_up_status", getattr(local_c, "follow_up_status", "待跟进"))
        prev_owner = prev.get("owner_sales_id", local_c.owner_sales_id)

        patch = {}
        if new_status != prev_status:
            patch["follow_up_status"] = new_status
            status_updated += 1
            changes.append({
                "type": "status",
                "customer_name": cname,
                "customer_id": local_c.customer_id,
                "from": prev_status,
                "to": new_status,
                "detail": f"跟进状态: {prev_status} -> {new_status}",
            })
        # 归属销售: 飞书表格「未分配(公海)」视为 None
        if new_owner != prev_owner:
            patch["owner_sales_id"] = new_owner
            owner_updated += 1
            changes.append({
                "type": "owner",
                "customer_name": cname,
                "customer_id": local_c.customer_id,
                "from": prev_owner,
                "to": new_owner,
                "detail": f"归属销售: {prev_owner or '公海'} -> {new_owner or '公海'}",
            })

        if patch and not dry_run:
            sync_state.setdefault(cname, {})
            sync_state[cname]["follow_up_status"] = patch.get("follow_up_status", prev_status)
            sync_state[cname]["owner_sales_id"] = patch.get("owner_sales_id", prev_owner)

    if not dry_run and (status_updated or owner_updated or new_customers):
        _save_sync_state(sync_state)

    return {
        "direction": "pull",
        "pulled": len(remote),
        "status_updated": status_updated,
        "owner_updated": owner_updated,
        "new_customers": new_customers,
        "changes": changes,
        "dry_run": dry_run,
    }


# ============================================================
# 正向同步: 本地 -> 飞书
# ============================================================

def _load_local_analysis_map() -> Dict[str, Dict[str, Any]]:
    """从 analysis_history 读取每个客户最新画像结果。"""
    from modules import data_loader
    analysis_map: Dict[str, Dict[str, Any]] = {}
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is not None:
            from modules.data_loader import AnalysisHistory
            rows = session.query(AnalysisHistory).order_by(AnalysisHistory.created_at.desc()).all()
            seen = set()
            for row in rows:
                if row.customer_id in seen:
                    continue
                seen.add(row.customer_id)
                try:
                    analysis_map[row.customer_id] = json.loads(row.result_json)
                except (json.JSONDecodeError, TypeError):
                    analysis_map[row.customer_id] = {}
            session.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 analysis_history 失败: %s", exc)
    return analysis_map


def _fetch_record_ids_by_name() -> Dict[str, str]:
    """拉取现有记录的 record_id, 键为客户名称。"""
    remote = fetch_bitable_leads()
    return {
        r.get("customer_name", "").strip(): r.get("record_id")
        for r in remote if r.get("record_id")
    }


def push_to_bitable(dry_run: bool = False) -> Dict[str, Any]:
    """正向同步: 把本地客户 + 画像推送到 Bitable(按客户名称 upsert)。

    注意: 使用 apply_bitable_sync_state 合并同步状态层, 确保本地已同步的
    「跟进状态 / 归属销售」能正确推回飞书(而非用 mock 基线覆盖)。
    """
    from modules import data_loader
    customers = data_loader.apply_bitable_sync_state(data_loader.load_customers())
    sales = data_loader.load_sales()
    sales_map = {s.sales_id: s.name for s in sales}
    analysis_map = _load_local_analysis_map()

    existing_ids = _fetch_record_ids_by_name()
    base_token = settings.feishu_base_token
    table_id = settings.feishu_base_leads_table

    created = 0
    updated = 0
    changes: List[Dict[str, Any]] = []

    for c in customers:
        prof = analysis_map.get(c.customer_id, {})
        sid = c.owner_sales_id or ""
        sname = sales_map.get(sid, sid)
        owner_text = f"{sname} ({sid})" if sid else "未分配（公海）"
        demands = "；".join(prof.get("core_demands", [])) if prof.get("core_demands") else ""
        suggestion = prof.get("follow_up_suggestion", "")

        field_map = {
            "客户名称": c.customer_name,
            "行业": [c.industry] if c.industry else ["智能制造"],
            "意向等级": [prof.get("intention_level", "中")],
            "流失风险": [prof.get("churn_risk", "低")],
            "城市": c.city or "全国",
            "企业规模": [c.scale] if c.scale else ["中型"],
            "归属销售": owner_text,
            "AI核心诉求": demands,
            "AI跟进策略建议": suggestion,
            "跟进状态": [getattr(c, "follow_up_status", "待跟进") or "待跟进"],
        }

        rid = existing_ids.get(c.customer_name)
        args = [
            "base", "+record-upsert",
            "--base-token", base_token,
            "--table-id", table_id,
            "--as", "user",
            "--format", "json",
            "--json", json.dumps(field_map, ensure_ascii=False),
        ]
        if rid:
            args += ["--record-id", rid]

        if dry_run:
            if rid:
                updated += 1
                changes.append({"type": "update", "customer_name": c.customer_name, "record_id": rid})
            else:
                created += 1
                changes.append({"type": "create", "customer_name": c.customer_name})
            continue

        resp = _run_lark_cli(args)
        if resp is not None:
            if rid:
                updated += 1
                changes.append({"type": "update", "customer_name": c.customer_name, "record_id": rid})
            else:
                created += 1
                changes.append({"type": "create", "customer_name": c.customer_name})
        else:
            changes.append({"type": "error", "customer_name": c.customer_name, "detail": "upsert 失败"})

    return {
        "direction": "push",
        "created": created,
        "updated": updated,
        "changes": changes,
        "dry_run": dry_run,
    }


def sync_both(dry_run: bool = False) -> Dict[str, Any]:
    """双向同步: 先 pull(飞书->本地) 再 push(本地->飞书)。"""
    pull_result = pull_from_bitable(dry_run=dry_run)
    push_result = push_to_bitable(dry_run=dry_run)
    return {"pull": pull_result, "push": push_result, "dry_run": dry_run}


# ============================================================
# CLI 入口
# ============================================================
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="飞书 Bitable <-> 易销 双向同步(经 lark-cli)")
    parser.add_argument("--pull", action="store_true", help="飞书 -> 易销 反向同步")
    parser.add_argument("--push", action="store_true", help="易销 -> 飞书 正向同步")
    parser.add_argument("--both", action="store_true", help="双向同步")
    parser.add_argument("--dry-run", action="store_true", help="只预览差异, 不落库/不写表格")
    args = parser.parse_args()

    if args.both or (not args.pull and not args.push):
        result = sync_both(dry_run=args.dry_run)
    elif args.pull:
        result = pull_from_bitable(dry_run=args.dry_run)
    else:
        result = push_to_bitable(dry_run=args.dry_run)

    print(json.dumps(result, ensure_ascii=False, indent=2))
