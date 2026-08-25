# -*- coding: utf-8 -*-
"""客户 / 销售团队 / 销售画像 / 移动端我的客户 / 记忆列表 路由。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from modules import agent_memory, data_loader, sales_profile_engine

from .common import _customers_with_levels, _find_sales_by_open_id

router = APIRouter(tags=["customers-sales"])


class SalesCreateRequest(BaseModel):
    """新增销售人员请求体。"""

    sales_id: str
    name: str
    good_at_industries: List[str] = Field(default_factory=list)
    responsible_cities: List[str] = Field(default_factory=list)
    current_load: int = 0
    mobile: str = ""
    open_id: str = ""


# ============================================================
# 记忆列表(学习效果回放)
# ============================================================


@router.get("/memories")
def list_memories() -> List[Dict[str, Any]]:
    """列出系统最近学到的记忆(人工复核反馈), 供运营看板"学习效果回放"。

    Returns:
        list[dict]: agent_memory.list_memories(limit=50) 的 dict 列表;
            无任何记忆时返回空数组 []。
    """
    agent_memory.init_memory_db()
    entries = agent_memory.list_memories(limit=50)
    return [
        entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
        for entry in entries
    ]


# ============================================================
# 客户列表
# ============================================================


@router.get("/customers")
def list_customers(
    intention: Optional[str] = None,   # 按意向等级筛选: 高/中/低
    churn: Optional[str] = None,       # 按流失风险筛选: 高/中/低
) -> List[Dict[str, Any]]:
    """「易销」平台: 返回全量客户列表(含意向/流失等级)。

    从 analysis_history 最新批次读取每个客户的意向/流失等级, join 到客户
    基础资料上; 支持按 intention / churn 筛选(取值 高/中/低)。

    Args:
        intention: 可选, 按意向等级过滤("高"/"中"/"低")。
        churn: 可选, 按流失风险过滤("高"/"中"/"低")。

    Returns:
        list[dict]: 每条含 customer_id / customer_name / industry / city /
            scale / owner_sales_id / create_time / intention_level / churn_risk
            (最新批次有分析结果时才有后两个字段, 否则为 None)。
    """
    result = _customers_with_levels()

    # 筛选
    if intention:
        result = [x for x in result if x.get("intention_level") == intention]
    if churn:
        result = [x for x in result if x.get("churn_risk") == churn]

    return result


# ============================================================
# 销售团队
# ============================================================


@router.get("/sales")
def list_sales() -> List[Dict[str, Any]]:
    """「易销」平台: 返回销售人员列表。

    Returns:
        list[dict]: 每条含 sales_id / name / good_at_industries /
            responsible_cities / current_load / mobile。
    """
    sales = data_loader.load_sales()
    return [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in sales]


@router.post("/sales")
def create_sales(req: SalesCreateRequest) -> Dict[str, Any]:
    """「易销」平台: 新增一名销售团队成员。"""
    try:
        from modules.data_loader import Sales
        sales_item = Sales(
            sales_id=req.sales_id.strip(),
            name=req.name.strip(),
            good_at_industries=req.good_at_industries,
            responsible_cities=req.responsible_cities,
            current_load=req.current_load,
            mobile=req.mobile.strip(),
            open_id=req.open_id.strip(),
        )
        saved = data_loader.add_sales_member(sales_item)
        return saved.model_dump() if hasattr(saved, "model_dump") else dict(saved)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"新增员工失败: {exc}")


@router.patch("/sales/{sales_id}")
def update_sales(sales_id: str, req: SalesCreateRequest) -> Dict[str, Any]:
    """「易销」平台: 更新一名销售成员的字段(按 sales_id 定位, 常用于绑定飞书 open_id)。"""
    try:
        from modules.data_loader import Sales
        sales_item = Sales(
            sales_id=sales_id.strip(),
            name=req.name.strip(),
            good_at_industries=req.good_at_industries,
            responsible_cities=req.responsible_cities,
            current_load=req.current_load,
            mobile=req.mobile.strip(),
            open_id=req.open_id.strip(),
        )
        saved = data_loader.update_sales_member(sales_item)
        return saved.model_dump() if hasattr(saved, "model_dump") else dict(saved)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"更新员工失败: {exc}")


@router.delete("/sales/{sales_id}")
def delete_sales(sales_id: str) -> Dict[str, Any]:
    """「易销」平台: 删除一名销售团队成员。"""
    try:
        ok = data_loader.delete_sales_member(sales_id.strip())
        if not ok:
            raise HTTPException(status_code=404, detail=f"未找到工号为 {sales_id} 的员工")
        return {"status": "ok", "deleted_sales_id": sales_id}
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除员工失败: {exc}")


@router.get("/sales/{sales_id}/profile")
def get_sales_profile(sales_id: str) -> Dict[str, Any]:
    """「易销」平台: 基于 CRM 历史成交数据，通过大模型生成销售能力画像。"""
    try:
        profile = sales_profile_engine.analyze_sales_profile(sales_id.strip(), auto_sync_to_model=False)
        deals = data_loader.load_crm_deals(sales_id=sales_id.strip())
        profile["deals"] = deals
        return profile
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成销售画像失败: {exc}")


@router.post("/sales/{sales_id}/sync-profile")
def sync_sales_profile(sales_id: str) -> Dict[str, Any]:
    """「易销」平台: AI 分析销售成单历史并自动反哺更新其擅长行业与能力标签。"""
    try:
        profile = sales_profile_engine.analyze_sales_profile(sales_id.strip(), auto_sync_to_model=True)
        return {"status": "ok", "profile": profile}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步销售画像失败: {exc}")


@router.post("/sales/sync-all-profiles")
def sync_all_sales_profiles() -> Dict[str, Any]:
    """「易销」平台: 全员一键 AI 扫描 CRM 成交记录并同步画像图谱。"""
    try:
        results = sales_profile_engine.analyze_all_sales()
        return {"status": "ok", "synced_count": len(results), "profiles": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"批量同步销售画像失败: {exc}")


# ============================================================
# 移动端「我的客户」
# ============================================================


@router.get("/api/my/customers")
def my_customers(open_id: Optional[str] = None) -> Dict[str, Any]:
    """销售移动端「我的客户」: 根据 open_id 返回当前销售名下的客户列表。

    Args:
        open_id: 飞书用户 open_id(网页应用自动带上)。

    Returns:
        dict: {sales: {sales_id,name,...}, customers: [ {...}, ... ]}。
            若 open_id 缺失或匹配不到销售, 返回 {sales: None, customers: []},
            并带 message 提示(前端据此显示引导)。
    """
    sales = _find_sales_by_open_id(open_id or "")
    if sales is None:
        return {"sales": None, "customers": [], "message": "未识别到销售身份，请确认已在「易销」配置该飞书账号的 open_id。"}

    customers = _customers_with_levels()
    mine = [c for c in customers if c.get("owner_sales_id") == sales.get("sales_id")]
    return {"sales": sales, "customers": mine, "message": None}
