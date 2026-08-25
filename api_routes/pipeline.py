# -*- coding: utf-8 -*-
"""流水线路由: 手动触发 / 画像历史 / 最近运行摘要。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from config.settings import settings
from modules import data_loader
from orchestrator import language_graph_flow as lgf

from .common import _customers_with_levels, _serialize_assignments, require_admin, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pipeline"])


@router.post("/pipeline/run", dependencies=[Depends(require_admin)])
def run_pipeline() -> Dict[str, Any]:
    """手动触发一次完整流水线(加载 → 分析 → 分配 → 推送), 并把每个客户的分析
    结果快照实际写入 SQLite 的 analysis_history 表。

    Returns:
        dict: 流水线 summary, 含:
            customer_count / analyzed_count / intention_stats(高/中/低) /
            churn_stats / assignment_count / needs_human_count / push_ok /
            push_hint / errors。

    Raises:
        HTTPException(500): 流水线整体执行失败(统一 detail, 不裸抛)。
    """
    try:
        # ---- 数据加载 + 数据库初始化 ----
        customers, records, sales, experiences = data_loader.load_pipeline_data()
        chat_map = data_loader.build_chat_map(records)
        data_loader.init_db()

        # ---- 多智能体流水线(内部已降级兜底, 不抛) ----
        state = lgf.run_pipeline_graph(customers, chat_map, sales, experiences)

        # ---- 落库: 每个客户的分析结果写入 analysis_history ----
        # 整批使用同一时间戳, 保证 pipeline_summary 能按 created_at 精确切分"最新批次"
        from datetime import datetime
        batch_ts = datetime.now().isoformat(timespec="seconds")
        saved = 0
        analysis_results: Dict[str, Any] = state.get("analysis_results") or {}
        by_id: Dict[str, str] = {c.customer_id: c.customer_name for c in customers}
        for customer_id, result in analysis_results.items():
            result_dict = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            data_loader.save_analysis_record(
                customer_id=customer_id,
                customer_name=by_id.get(customer_id, customer_id),
                result=result_dict,
                created_at=batch_ts,
            )
            saved += 1
        logger.info("流水线完成: 分析 %d 家, 已落库 %d 条", len(analysis_results), saved)

        summary: Dict[str, Any] = dict(state.get("summary") or {})
        summary["saved_records"] = saved
        summary["meta"] = state.get("meta") or {}
        # 数据源标识: 以实际加载到的客户 ID 前缀为准(企客宝拉取失败时会
        # 自动降级到 mock, 此时不能再标成 qikebao)。
        summary["data_source"] = (
            "qikebao"
            if customers and customers[0].customer_id.startswith(settings.qikebao_customer_id_prefix)
            else "mock"
        )
        # 附加分配明细(供运营看板「分配清单」渲染; 仅新增键, 不改变既有字段)
        summary["assignments"] = _serialize_assignments(state, analysis_results)
        return summary
    except Exception as exc:  # noqa: BLE001 —— 统一 500 detail, 不裸抛
        logger.error("流水线执行异常: %s", exc)
        raise HTTPException(status_code=500, detail="流水线执行异常: %s" % exc)


@router.get("/history/{customer_id}")
def get_history(
    customer_id: str,
    _session: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """查询某客户的画像分析历史(按 created_at 倒序)。

    权限隔离: 销售角色仅可查看自己名下客户的历史; 超级管理员可看任意。

    Args:
        customer_id: 客户 ID(路径参数)。
        _session: 登录会话(含 role / username)。

    Returns:
        dict: {"customer_id": ..., "records": [...]}。
            records 内每条含 customer_id / customer_name / result(画像快照 dict) /
            created_at。

    Raises:
        HTTPException(403): 销售角色访问非本人客户。
        HTTPException(404): 该客户无任何历史记录(带 detail 说明)。
    """
    # 权限隔离: 销售只能看自己名下客户
    if _session.get("role") != "super_admin":
        from modules import data_loader as _dl
        all_customers = _customers_with_levels()
        mine_ids = {c.get("customer_id") for c in all_customers if c.get("owner_sales_id") == _session.get("username")}
        if customer_id not in mine_ids:
            raise HTTPException(status_code=403, detail="无权查看该客户的画像历史")

    data_loader.init_db()
    history: List[dict] = data_loader.get_analysis_history(customer_id)
    if not history:
        raise HTTPException(
            status_code=404,
            detail="客户 %s 暂无画像分析历史记录 —— 请先调用 POST /pipeline/run 生成。" % customer_id,
        )
    return {"customer_id": customer_id, "records": history}


@router.get("/pipeline/summary", dependencies=[Depends(require_admin)])
def pipeline_summary() -> Dict[str, Any]:
    """「易销」平台: 最近一次流水线运行摘要。

    读取 analysis_history 的最新批次(created_at 去重后取最大), 聚合意向/流失分布;
    analysis_history 无数据时返回 {ran: False, hint: "尚未运行流水线"}。

    Returns:
        dict: 运行过时含 ran=True / last_run(ISO 时间) / records(总行数) /
            batch_records(最新批次行数) / intention_stats / churn_stats;
            未运行过时含 ran=False / records=0 / last_run=None / hint。
    """
    data_loader.init_db()
    # 全量历史(按 created_at 倒序), 用于聚合; 表为空时返回 [] 不抛错
    session = data_loader._get_session()
    all_rows: List[Any] = []
    if session is not None:
        try:
            from modules.data_loader import AnalysisHistory
            all_rows = (
                session.query(AnalysisHistory)
                .order_by(AnalysisHistory.created_at.desc())
                .all()
            )
        except Exception as exc:  # noqa: BLE001 —— 汇总查询失败按"未运行"处理
            logger.error("pipeline/summary 查询失败: %s", exc)
            all_rows = []
        finally:
            session.close()

    if not all_rows:
        return {
            "ran": False,
            "records": 0,
            "last_run": None,
            "hint": "尚未运行流水线 —— 请先调用 POST /pipeline/run 生成分析结果。",
        }

    # 解析全部行(复用 get_analysis_history 的解析逻辑, 避免重复代码)
    records: List[dict] = []
    for row in all_rows:
        try:
            result = json.loads(row.result_json)
        except (json.JSONDecodeError, TypeError):
            result = {}
        records.append({
            "customer_id": row.customer_id,
            "customer_name": row.customer_name,
            "result": result,
            "created_at": row.created_at,
        })

    last_run: str = records[0]["created_at"]
    batch_records: int = len([r for r in records if r["created_at"] == last_run])

    # 聚合最新批次的意向/流失分布
    def _dist(level_key: str) -> Dict[str, int]:
        from collections import Counter
        counter = Counter(
            r["result"].get(level_key) for r in records
            if r["created_at"] == last_run and r["result"].get(level_key) in ("高", "中", "低")
        )
        return {"高": counter.get("高", 0), "中": counter.get("中", 0), "低": counter.get("低", 0)}

    return {
        "ran": True,
        "records": len(records),
        "last_run": last_run,
        "batch_records": batch_records,
        "intention_stats": _dist("intention_level"),
        "churn_stats": _dist("churn_risk"),
    }
