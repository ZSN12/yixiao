# -*- coding: utf-8 -*-
"""客户 / 销售团队 / 销售画像 / 移动端我的客户 / 记忆列表 路由。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from modules import agent_memory, data_loader, sales_profile_engine

from .common import _customers_with_levels, _find_sales_by_open_id, require_admin, require_auth

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


@router.get("/memories", dependencies=[Depends(require_admin)])
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
    _session: Dict[str, Any] = Depends(require_auth),
) -> List[Dict[str, Any]]:
    """「易销」平台: 返回客户列表(含意向/流失等级)。

    权限隔离: 超级管理员返回全量; 销售角色仅返回其名下(owner_sales_id=当前销售工号)客户。

    Args:
        intention: 可选, 按意向等级过滤("高"/"中"/"低")。
        churn: 可选, 按流失风险过滤("高"/"中"/"低")。
        _session: 登录会话(含 role / username)。

    Returns:
        list[dict]: 客户列表(销售角色仅含自己名下)。
    """
    result = _customers_with_levels()

    # 权限隔离: 销售角色只可见自己名下客户
    if _session.get("role") != "super_admin":
        sid = _session.get("username") or ""
        result = [x for x in result if x.get("owner_sales_id") == sid]

    # 筛选
    if intention:
        result = [x for x in result if x.get("intention_level") == intention]
    if churn:
        result = [x for x in result if x.get("churn_risk") == churn]

    return result


# ============================================================
# 销售团队
# ============================================================


@router.get("/sales", dependencies=[Depends(require_admin)])
def list_sales() -> List[Dict[str, Any]]:
    """「易销」平台: 返回销售人员列表。

    Returns:
        list[dict]: 每条含 sales_id / name / good_at_industries /
            responsible_cities / current_load / mobile。
    """
    sales = data_loader.load_sales()
    return [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in sales]


@router.post("/sales", dependencies=[Depends(require_admin)])
def create_sales(req: SalesCreateRequest) -> Dict[str, Any]:
    """「易销」平台: 新增一名销售团队成员。"""
    try:
        from modules.data_loader import Sales
        mobile = req.mobile.strip()
        open_id = req.open_id.strip()
        # 若未填 open_id 但填了手机号，尝试从飞书 API 自动反查
        if not open_id and mobile:
            try:
                from modules import feishu_app_notifier
                fetched = feishu_app_notifier.get_open_id_by_mobile(mobile)
                if fetched:
                    open_id = fetched
            except Exception as exc:  # noqa: BLE001
                logger.warning("通过手机号反查飞书 open_id 失败: %s", exc)

        sales_item = Sales(
            sales_id=req.sales_id.strip(),
            name=req.name.strip(),
            good_at_industries=req.good_at_industries,
            responsible_cities=req.responsible_cities,
            current_load=req.current_load,
            mobile=mobile,
            open_id=open_id,
        )
        saved = data_loader.add_sales_member(sales_item)
        return saved.model_dump() if hasattr(saved, "model_dump") else dict(saved)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"新增员工失败: {exc}")


@router.patch("/sales/{sales_id}", dependencies=[Depends(require_admin)])
def update_sales(sales_id: str, req: SalesCreateRequest) -> Dict[str, Any]:
    """「易销」平台: 更新一名销售成员的字段(按 sales_id 定位, 常用于绑定飞书 open_id)。"""
    try:
        from modules.data_loader import Sales
        mobile = req.mobile.strip()
        open_id = req.open_id.strip()
        # 若未填 open_id 但填了手机号，尝试从飞书 API 自动反查
        if not open_id and mobile:
            try:
                from modules import feishu_app_notifier
                fetched = feishu_app_notifier.get_open_id_by_mobile(mobile)
                if fetched:
                    open_id = fetched
            except Exception as exc:  # noqa: BLE001
                logger.warning("通过手机号反查飞书 open_id 失败: %s", exc)

        sales_item = Sales(
            sales_id=sales_id.strip(),
            name=req.name.strip(),
            good_at_industries=req.good_at_industries,
            responsible_cities=req.responsible_cities,
            current_load=req.current_load,
            mobile=mobile,
            open_id=open_id,
        )
        saved = data_loader.update_sales_member(sales_item)
        return saved.model_dump() if hasattr(saved, "model_dump") else dict(saved)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"更新员工失败: {exc}")


@router.delete("/sales/{sales_id}", dependencies=[Depends(require_admin)])
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


@router.get("/sales/{sales_id}/profile", dependencies=[Depends(require_admin)])
def get_sales_profile(sales_id: str) -> Dict[str, Any]:
    """「易销」平台: 基于 CRM 历史成交数据，通过大模型生成销售能力画像。"""
    try:
        profile = sales_profile_engine.analyze_sales_profile(sales_id.strip(), auto_sync_to_model=False)
        deals = data_loader.load_crm_deals(sales_id=sales_id.strip())
        profile["deals"] = deals
        return profile
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成销售画像失败: {exc}")


@router.post("/sales/{sales_id}/sync-profile", dependencies=[Depends(require_admin)])
def sync_sales_profile(sales_id: str) -> Dict[str, Any]:
    """「易销」平台: AI 分析销售成单历史并自动反哺更新其擅长行业与能力标签。"""
    try:
        profile = sales_profile_engine.analyze_sales_profile(sales_id.strip(), auto_sync_to_model=True)
        return {"status": "ok", "profile": profile}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步销售画像失败: {exc}")


@router.post("/sales/sync-all-profiles", dependencies=[Depends(require_admin)])
def sync_all_sales_profiles() -> Dict[str, Any]:
    """「易销」平台: 全员一键 AI 扫描 CRM 成交记录并同步画像图谱。"""
    try:
        results = sales_profile_engine.analyze_all_sales()
        return {"status": "ok", "synced_count": len(results), "profiles": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"批量同步销售画像失败: {exc}")


@router.post("/sales/sync-open-ids", dependencies=[Depends(require_admin)])
def sync_sales_open_ids() -> Dict[str, Any]:
    """「易销」平台: 批量根据销售成员手机号通过飞书开放平台自动反查并绑定 open_id。"""
    try:
        from modules import feishu_app_notifier
        sales_list = data_loader.load_sales()
        synced_count = 0
        details = []
        updated = False

        for s in sales_list:
            mobile = (getattr(s, "mobile", "") or "").strip()
            old_oid = (getattr(s, "open_id", "") or "").strip()
            if not mobile:
                details.append({"sales_id": s.sales_id, "name": s.name, "status": "no_mobile", "open_id": old_oid})
                continue
            
            # 若已有有效 open_id 且无需强制刷，也可尝试核对或补全
            try:
                fetched_oid = feishu_app_notifier.get_open_id_by_mobile(mobile)
                if fetched_oid:
                    if fetched_oid != old_oid:
                        s.open_id = fetched_oid
                        synced_count += 1
                        updated = True
                    details.append({"sales_id": s.sales_id, "name": s.name, "status": "synced", "open_id": fetched_oid})
                else:
                    details.append({"sales_id": s.sales_id, "name": s.name, "status": "not_found", "open_id": old_oid})
            except Exception as e:
                details.append({"sales_id": s.sales_id, "name": s.name, "status": "error", "error": str(e), "open_id": old_oid})

        if updated:
            data_loader.save_sales(sales_list)

        return {
            "status": "ok",
            "synced_count": synced_count,
            "total_sales": len(sales_list),
            "details": details,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步飞书 open_id 失败: {exc}")


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
