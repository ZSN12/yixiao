# -*- coding: utf-8 -*-
"""FastAPI 服务层(api.py): 把"每日运行结果 → 可查询 → 可人工复核"串成对外 Web 服务。

端♂一览(全部返回 JSON):
- GET  /health          -> {"status": "ok"}: 健康检查。
- POST /pipeline/run    -> 手动触发一次完整流水线(mock 模式全规则引擎, 免费):
                           加载数据 → run_pipeline_graph → 对每个客户把分析结果
                           model_dump 成 dict 写入 analysis_history(SQLite 落库)
                           → 返回 summary(客户数 / 意向分层 / 分配条数 / 推送状态)。
- GET  /history/{customer_id} -> 某客户画像历史快照列表(按时间倒序)的字典:
                           每条含 customer_id / customer_name / result / created_at;
                           无记录返回 404 + detail。
- POST /feedback        -> body {customer_id, correct_sales_id, note?}:
                           调 lead_assigner.submit_feedback 升级/新建强记忆,
                           返回升级后的 MemoryEntry(dict);
                           agent_memory 不可用时返回 400 + detail。
- GET  /memories        -> agent_memory.list_memories(limit=50) 的 dict 列表
                           (无记忆返回空数组), 供运营看板"学习效果回放"。
- GET  /                -> 返回 static/index.html(运营看板首页)。
- 静态资源              -> /static/* 挂载 static/ 目录(HTML/CSS/JS)。

健壮性:
- 全局异常处理器统一把异常转成 {"detail": ...} JSON, 流水线异常捕获为 500 而非裸抛;
- CORS 允许本地前端调试(allow_origins=["*"]);
- 使用 FastAPI TestClient 可直接 import 本模块自检, 无需起服务。

启动: python api.py  -> uvicorn 127.0.0.1:8000
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# 项目根目录(本文件位于 <项目根>/api.py)加入 sys.path, 保证任何 cwd 下可运行
PROJECT_ROOT: Path = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import agent_memory, data_loader  # noqa: E402
from modules import bitable_sync  # noqa: E402
from modules import data_source_registry  # noqa: E402
from modules import follow_up_notes  # noqa: E402
from modules import lead_assigner  # noqa: E402
from modules import sales_profile_engine  # noqa: E402
from modules import sla_monitor  # noqa: E402
from modules import talk_track  # noqa: E402
from modules import user_auth  # noqa: E402
from orchestrator import language_graph_flow as lgf  # noqa: E402

logger = logging.getLogger(__name__)

app = FastAPI(title="销售线索智能分析与分发助手 API", version="1.0.0")

# CORS: 允许本地前端调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup_seed_admin() -> None:
    """应用启动时确保超级管理员种子账号存在(admin/123456)。

    注: 用 on_event 而非 lifespan, 兼容当前 FastAPI 0.115 及旧版(避免
    lifespan 上下文在 TestClient 下额外接线)。"""
    try:
        user_auth.ensure_seed_admin()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动时创建种子管理员失败: %s", exc)
    try:
        data_source_registry.ensure_seed_sources()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动时写入预置数据源失败: %s", exc)


# ============================================================
# 请求/响应模型
# ============================================================


class FeedbackRequest(BaseModel):
    """人工复核反馈请求体。

    Attributes:
        customer_id: 客户 ID。
        correct_sales_id: 人工确认/修正后的正确销售 ID。
        note: 人工备注(可选)。
    """

    customer_id: str
    correct_sales_id: str
    note: str = ""


class SalesCreateRequest(BaseModel):
    """新增销售人员请求体。"""

    sales_id: str
    name: str
    good_at_industries: List[str] = []
    responsible_cities: List[str] = []
    current_load: int = 0
    mobile: str = ""
    open_id: str = ""


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str
    password: str


class DataSourceCreateRequest(BaseModel):
    """新增数据源请求体。"""

    name: str
    type: str
    config: Dict[str, Any] = {}
    enabled: bool = True


class DataSourceUpdateRequest(BaseModel):
    """编辑数据源请求体(所有字段可选)。"""

    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    status: Optional[str] = None


# ============================================================
# 数据源中心 CRUD API
# ============================================================


@app.get("/api/data-sources/types")
def data_source_types() -> Dict[str, Any]:
    """返回预置数据源类型定义(前端「添加数据源」弹窗的下拉/表单依据)。"""
    return {"types": data_source_registry.SOURCE_TYPES}


@app.get("/api/data-sources")
def list_data_sources() -> Dict[str, Any]:
    """列出全部数据源(含停用)。"""
    try:
        return {"sources": data_source_registry.list_sources()}
    except Exception as exc:  # noqa: BLE001
        logger.error("列出数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"列出数据源失败: {exc}")


@app.post("/api/data-sources", status_code=201)
def create_data_source(body: DataSourceCreateRequest) -> Dict[str, Any]:
    """新增一个数据源。"""
    try:
        row = data_source_registry.add_source(body.name, body.type, body.config, body.enabled)
        return {"source": row}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("新增数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"新增数据源失败: {exc}")


@app.patch("/api/data-sources/{source_id}")
def update_data_source(source_id: int, body: DataSourceUpdateRequest) -> Dict[str, Any]:
    """编辑/启停一个数据源。"""
    try:
        row = data_source_registry.update_source(
            source_id,
            name=body.name,
            config=body.config,
            enabled=body.enabled,
            status=body.status,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="数据源不存在")
        return {"source": row}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("更新数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"更新数据源失败: {exc}")


@app.delete("/api/data-sources/{source_id}")
def delete_data_source(source_id: int) -> Dict[str, Any]:
    """删除一个数据源。"""
    try:
        ok = data_source_registry.delete_source(source_id)
        if not ok:
            raise HTTPException(status_code=404, detail="数据源不存在")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("删除数据源失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"删除数据源失败: {exc}")


# ============================================================
# 全局异常处理 —— 统一返回 {"detail": ...}
# ============================================================


@app.exception_handler(Exception)
async def _global_exception_handler(request, exc: Exception):
    """全局兜底: 任何未捕获异常都转成 {"detail": ...} JSON, 不裸抛堆栈。"""
    logger.error("未捕获异常(%s): %s", type(exc).__name__, exc)
    return _json_response(500, "服务内部错误: %s" % exc)



def _json_response(status_code: int, detail: Any) -> JSONResponse:
    """构造统一格式的 JSON 响应(成功/失败都走这里)。"""
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _feishu_card_response(card: Optional[Dict[str, Any]] = None, toast: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """构造飞书卡片回调的标准响应(用于卡片按钮点击后原地更新卡片)。

    飞书卡片回调(交互回调, card.action.trigger)更新卡片: 响应体直接返回 JSON,
    含 card(替换当前卡片) 与/或 toast(弹提示)。

    关键: 使用原始 JSON 卡片(raw)时, card 字段必须包装成
    {"type": "raw", "data": {卡片本体}}, 缺少 type/data 包装会触发飞书错误码 200672。
    """
    payload: Dict[str, Any] = {}
    if card:
        payload["card"] = {"type": "raw", "data": card}
    if toast:
        payload["toast"] = toast
    return payload


# ============================================================
# 端点: 用户认证(登录 / 登出 / 当前用户)
# ============================================================


@app.post("/api/login")
def api_login(req: LoginRequest) -> Dict[str, Any]:
    """用户登录: 校验账号密码, 成功返回 token + 用户信息。

    Args:
        req: {username, password}。

    Returns:
        dict: {token, username, role, display_name}。

    Raises:
        HTTPException(401): 账号或密码错误。
    """
    result = user_auth.login(req.username, req.password)
    if result is None:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return result


@app.post("/api/logout")
def api_logout(request: Request) -> Dict[str, Any]:
    """用户登出: 清除会话 token。"""
    token = (request.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
    user_auth.logout(token)
    return {"status": "ok"}


@app.get("/api/me")
def api_me(request: Request) -> Dict[str, Any]:
    """获取当前登录用户信息(校验 token)。

    Returns:
        dict: {username, role, display_name} 或 {authenticated: False}。
    """
    token = (request.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
    sess = user_auth.get_session(token)
    if sess is None:
        return {"authenticated": False}
    return {"authenticated": True, **sess}


# ============================================================
# 端点: 健康检查
# ============================================================


@app.get("/health")
def health() -> Dict[str, str]:
    """健康检查。

    Returns:
        dict: {"status": "ok"}。
    """
    return {"status": "ok"}


# ============================================================
# 端点: 手动触发完整流水线
# ============================================================


@app.post("/pipeline/run")
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
        customers, records, sales = data_loader.load_all()
        chat_map = data_loader.build_chat_map(records)
        experiences = data_loader.load_sales_experiences()
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
        # 附加分配明细(供运营看板「分配清单」渲染; 仅新增键, 不改变既有字段)
        summary["assignments"] = _serialize_assignments(state, analysis_results)
        return summary
    except Exception as exc:  # noqa: BLE001 —— 统一 500 detail, 不裸抛
        logger.error("流水线执行异常: %s", exc)
        raise HTTPException(status_code=500, detail="流水线执行异常: %s" % exc)


# ============================================================
# 端点: 查询客户画像历史
# ============================================================


def _serialize_assignments(state: Dict[str, Any], analysis_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把流水线分配结果序列化成看板可渲染的简表(含客户意向等级)。

    Args:
        state: 流水线最终状态(含 assignments 列表)。
        analysis_results: {customer_id: AnalysisResult}, 用于补意向等级。

    Returns:
        list[dict]: 每条含 customer_id / customer_name / intention_level /
            sales_id / sales_name / match_reason / needs_human。
    """
    briefs: List[Dict[str, Any]] = []
    for a in state.get("assignments") or []:
        ar = analysis_results.get(getattr(a, "customer_id", ""))
        intention = getattr(ar, "intention_level", None) if ar is not None else None
        briefs.append({
            "customer_id": getattr(a, "customer_id", ""),
            "customer_name": getattr(a, "customer_name", ""),
            "intention_level": intention,
            "sales_id": getattr(a, "sales_id", ""),
            "sales_name": getattr(a, "sales_name", ""),
            "match_reason": getattr(a, "match_reason", ""),
            "needs_human": bool(getattr(a, "needs_human", False)),
        })
    return briefs


@app.get("/history/{customer_id}")
def get_history(customer_id: str) -> Dict[str, Any]:
    """查询某客户的画像分析历史(按 created_at 倒序)。

    Args:
        customer_id: 客户 ID(路径参数)。

    Returns:
        dict: {"customer_id": ..., "records": [...]}。
            records 内每条含 customer_id / customer_name / result(画像快照 dict) /
            created_at。

    Raises:
        HTTPException(404): 该客户无任何历史记录(带 detail 说明)。
    """
    data_loader.init_db()
    history: List[dict] = data_loader.get_analysis_history(customer_id)
    if not history:
        raise HTTPException(
            status_code=404,
            detail="客户 %s 暂无画像分析历史记录 —— 请先调用 POST /pipeline/run 生成。" % customer_id,
        )
    return {"customer_id": customer_id, "records": history}


# ============================================================
# 端点: 跟进小记(查询 + AI 动态再分析)
# ============================================================


@app.get("/follow-up-notes")
def list_follow_up_notes(customer_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """查询跟进小记列表(可选按客户过滤, 按时间倒序)。

    Args:
        customer_id: 可选, 按客户 ID 过滤。

    Returns:
        list[dict]: 每条含 customer_id / customer_name / sales_id / note_text /
            intention_before / intention_after / churn_before / churn_after /
            created_at。
    """
    return follow_up_notes.list_notes(customer_id=customer_id)


class NoteReanalyzeRequest(BaseModel):
    """跟进小记再分析请求体。"""

    customer_id: str
    note_text: str
    sales_id: str = ""


@app.post("/follow-up-notes/reanalyze")
def reanalyze_note(req: NoteReanalyzeRequest) -> Dict[str, Any]:
    """直接提交一条跟进小记, 触发 AI 动态重估意向(HTTP 入口)。

    供手机端/电脑端工作台录入小记使用, 与飞书卡片 submit_note 共用同一引擎。

    Args:
        req: {customer_id, note_text, sales_id?}。

    Returns:
        dict: 再分析结果(意向/流失等级变化 + 新跟进策略)。
    """
    customer = data_loader.load_customers()
    matched = next((c for c in customer if c.customer_id == req.customer_id), None)
    customer_name = matched.customer_name if matched else req.customer_id
    return follow_up_notes.reanalyze_with_note(
        customer_id=req.customer_id,
        customer_name=customer_name,
        note_text=req.note_text,
        sales_id=req.sales_id,
        persist=True,
    )


# ============================================================
# 端点: AI 话术生成(微信破冰 / 电话话术)
# ============================================================


@app.get("/talk-track/{customer_id}")
def get_talk_track(customer_id: str, track_type: str = "wechat", sales_id: str = "") -> Dict[str, Any]:
    """AI 一键生成跟进话术(微信破冰 / 电话话术)。

    供手机端/电脑端工作台调用, 与飞书卡片 gen_talk_track 共用同一引擎。

    Args:
        customer_id: 客户 ID(路径参数)。
        track_type: "wechat"(微信破冰) | "phone"(电话话术), 默认 wechat。
        sales_id: 销售 ID(用于话术署名), 可选。

    Returns:
        dict: {customer_id, customer_name, track_type, content, engine}。
            engine 为 "llm"(Kimi K2.7 Code) | "rules"(规则模板)。
    """
    # 反查销售姓名(用于话术署名)
    sales_name = ""
    if sales_id:
        try:
            sales = data_loader.load_sales()
            matched = next((s for s in sales if s.sales_id == sales_id), None)
            if matched:
                sales_name = matched.name
        except Exception:  # noqa: BLE001
            sales_name = sales_id

    return talk_track.generate_talk_track(customer_id, track_type, sales_name)


# ============================================================
# 端点: SLA 超时预警 + 自动流转公海
# ============================================================


@app.get("/sla/status")
def sla_status() -> Dict[str, Any]:
    """查询当前所有已归属客户的 SLA 状态(只读, 不触发流转)。"""
    result = sla_monitor.check_sla(apply_changes=False)
    return {
        "warning": result["warning"],
        "overdue": result["overdue"],
        "ok": result["ok"],
        "summary": {
            "warning_count": len(result["warning"]),
            "overdue_count": len(result["overdue"]),
            "ok_count": len(result["ok"]),
        },
    }


@app.post("/sla/check")
def sla_check() -> Dict[str, Any]:
    """触发一次 SLA 检测: 超时客户自动流转公海(释放归属), 返回预警/超时名单。

    检测到预警/超时后, 会向相关销售发送飞书卡片提醒(需配置飞书应用凭证)。
    """
    result = sla_monitor.check_sla(apply_changes=True)

    # 发送飞书预警通知(尽力而为, 失败不影响检测结果)
    notifier = None
    try:
        from modules import feishu_app_notifier as notifier_mod
        notifier = notifier_mod
    except Exception:  # noqa: BLE001
        pass

    notified: List[str] = []
    if notifier is not None:
        try:
            sales = data_loader.load_sales()
            sales_by_id = {s.sales_id: s for s in sales}
        except Exception:  # noqa: BLE001
            sales_by_id = {}

        # 预警提醒 + 超时通知
        for item in result["warning"] + result["overdue"]:
            sid = item.get("owner_sales_id", "")
            sales = sales_by_id.get(sid)
            open_id = getattr(sales, "open_id", "") if sales else ""
            if not open_id:
                continue
            status = item.get("sla_status", "warning")
            ok_sent = notifier.send_sla_alert_card(
                receive_id=open_id,
                customer_name=item.get("customer_name", ""),
                sla_status=status,
                elapsed_hours=item.get("elapsed_hours", 0),
                overdue_hours=sla_monitor.DEFAULT_OVERDUE_HOURS,
            )
            if ok_sent:
                notified.append(item.get("customer_name", ""))

    return {
        "warning": result["warning"],
        "overdue": result["overdue"],
        "ok": result["ok"],
        "summary": {
            "warning_count": len(result["warning"]),
            "overdue_count": len(result["overdue"]),
            "ok_count": len(result["ok"]),
            "released_to_pool": [o["customer_name"] for o in result["overdue"]],
            "notified_sales": notified,
        },
    }


# ============================================================
# 端点: 人工复核反馈(记忆升级)
# ============================================================


@app.post("/feedback")
def submit_feedback(req: FeedbackRequest) -> Dict[str, Any]:
    """人工复核反馈: 把该客户的记忆升级/新建为强记忆(影响后续分配)。

    Args:
        req: FeedbackRequest {customer_id, correct_sales_id, note?}。

    Returns:
        dict: 升级后的强记忆条目(MemoryEntry 的 dict 表示), 含 memory_id /
            customer_id / query_text / sales_id / decision / correct_sales_id /
            confidence / source="strong" / feedback_note / created_at。

    Raises:
        HTTPException(400): agent_memory 不可用(记忆功能未启用)时返回 400。
    """
    entry = lead_assigner.submit_feedback(
        customer_id=req.customer_id,
        correct_sales_id=req.correct_sales_id,
        note=req.note,
        memory=agent_memory,
    )
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail="agent_memory 记忆模块不可用 —— 人工复核反馈无法记录。",
        )
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    return dict(entry)


# ============================================================
# 静态资源托管(运营看板前端) + 根路由
# ============================================================

# 静态目录: <项目根>/static(与 api.py 同级)
STATIC_DIR: Path = PROJECT_ROOT / "static"


def _mount_static() -> None:
    """挂载 /static 静态资源目录(FastAPI StaticFiles 自带依赖)。

    StaticFiles 不可用(异常导入)时跳过挂载, 不阻塞应用启动 ——
    根路由 / 仍有 FileResponse 读文件兜底, 保证看板页面可访问。
    """
    try:
        from fastapi.staticfiles import StaticFiles

        if STATIC_DIR.is_dir():
            app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
            logger.info("已挂载静态目录: %s -> /static", STATIC_DIR)
    except Exception as exc:  # noqa: BLE001 —— 静态挂载失败不阻塞服务
        logger.warning("StaticFiles 挂载失败(%s), 使用 FileResponse 兜底", exc)


_mount_static()


@app.get("/")
def index(request: Request) -> FileResponse:
    """系统首页: 智能自适应双端。

    - 手机端访问(Mobile/iPhone/Android/飞书移动App) -> 自动返回手机专属客户画像页 static/m.html;
    - 电脑端访问 -> 返回桌面全功能运营看板 static/index.html。
    """
    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(kw in ua for kw in ["mobile", "android", "iphone", "ipad", "ipod", "micromessenger"])
    target_file = (STATIC_DIR / "m.html") if is_mobile else (STATIC_DIR / "index.html")
    if not target_file.exists():
        target_file = STATIC_DIR / "index.html"
    if not target_file.exists():
        raise HTTPException(status_code=404, detail="前端页面文件不存在, 请检查 static 目录。")
    return FileResponse(str(target_file), media_type="text/html; charset=utf-8")


# ============================================================
# 端点: 飞书移动端入口(销售在飞书里看自己的客户)
# ============================================================


@app.get("/m/")
def mobile_index() -> FileResponse:
    """销售移动端入口页: 返回 static/m.html(手机友好 H5)。

    销售从飞书网页应用/工作台进入该地址(可携带 ?open_id=xxx), 页面会
    自动用 URL 里的 open_id 调 /api/my/customers 拉取自己名下的客户。
    """
    m_file = STATIC_DIR / "m.html"
    if not m_file.exists():
        raise HTTPException(status_code=404, detail="static/m.html 不存在, 请检查前端文件。")
    return FileResponse(str(m_file), media_type="text/html; charset=utf-8")


def _find_sales_by_open_id(open_id: str) -> Optional[Dict[str, Any]]:
    """根据飞书 open_id 定位当前销售(反查销售列表的 open_id 字段)。"""
    if not open_id:
        return None
    try:
        sales = data_loader.load_sales()
    except Exception:  # noqa: BLE001
        return None
    for s in sales:
        d = s.model_dump() if hasattr(s, "model_dump") else dict(s)
        if d.get("open_id") == open_id:
            return d
    return None


def _customers_with_levels() -> List[Dict[str, Any]]:
    """返回全量客户列表, 并从 analysis_history 最新批次 join 意向/流失等级。

    抽取自 /customers 的公共逻辑, 供移动端"我的客户"复用。
    注意: 客户列表已合并飞书多维表格同步状态(跟进状态/归属销售) + SLA 超时流转
    (超时释放回公海的客户 owner_sales_id 覆盖为 None)。
    """
    customers = data_loader.apply_bitable_sync_state(data_loader.load_customers())
    # 合并 SLA 超时流转(释放归属回公海)
    customers = sla_monitor.apply_sla_overlay(customers)
    result: List[Dict[str, Any]] = [
        c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in customers
    ]
    level_map: Dict[str, Dict[str, str]] = {}
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is not None:
            try:
                from modules.data_loader import AnalysisHistory
                rows = (
                    session.query(AnalysisHistory)
                    .order_by(AnalysisHistory.created_at.desc())
                    .all()
                )
                seen: set = set()
                for row in rows:
                    if row.customer_id in seen:
                        continue
                    seen.add(row.customer_id)
                    try:
                        r = json.loads(row.result_json)
                    except (json.JSONDecodeError, TypeError):
                        r = {}
                    level_map[row.customer_id] = {
                        "intention_level": r.get("intention_level"),
                        "churn_risk": r.get("churn_risk"),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("join 意向/流失等级失败, 仅返回基础资料: %s", exc)
            finally:
                session.close()
    except Exception:  # noqa: BLE001
        pass
    for item in result:
        meta = level_map.get(item["customer_id"], {})
        item["intention_level"] = meta.get("intention_level")
        item["churn_risk"] = meta.get("churn_risk")
    return result


@app.get("/api/my/customers")
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


# ============================================================
# 端点: 记忆列表(学习效果回放)
# ============================================================


@app.get("/memories")
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
# 端点: 客户列表 / 销售列表 / 流水线最近运行摘要(「易销」平台只读数据)
# ============================================================


@app.get("/customers")
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
    customers = data_loader.apply_bitable_sync_state(data_loader.load_customers())
    # 合并 SLA 超时流转(释放归属回公海)
    customers = sla_monitor.apply_sla_overlay(customers)
    result: List[Dict[str, Any]] = [
        c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in customers
    ]

    # 从 analysis_history 最新批次读取意向/流失等级
    level_map: Dict[str, Dict[str, str]] = {}   # customer_id -> {intention, churn}
    try:
        data_loader.init_db()
        session = data_loader._get_session()
        if session is not None:
            try:
                from modules.data_loader import AnalysisHistory
                rows = (
                    session.query(AnalysisHistory)
                    .order_by(AnalysisHistory.created_at.desc())
                    .all()
                )
                # 取每个客户最新一条(rows 已按时间倒序)
                seen: set = set()
                for row in rows:
                    if row.customer_id in seen:
                        continue
                    seen.add(row.customer_id)
                    try:
                        r = json.loads(row.result_json)
                    except (json.JSONDecodeError, TypeError):
                        r = {}
                    level_map[row.customer_id] = {
                        "intention_level": r.get("intention_level"),
                        "churn_risk": r.get("churn_risk"),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("join 意向/流失等级失败, 仅返回基础资料: %s", exc)
            finally:
                session.close()
    except Exception:  # noqa: BLE001 —— join 失败不阻断列表
        pass

    for item in result:
        meta = level_map.get(item["customer_id"], {})
        item["intention_level"] = meta.get("intention_level")
        item["churn_risk"] = meta.get("churn_risk")

    # 筛选
    if intention:
        result = [x for x in result if x.get("intention_level") == intention]
    if churn:
        result = [x for x in result if x.get("churn_risk") == churn]

    return result


@app.get("/sales")
def list_sales() -> List[Dict[str, Any]]:
    """「易销」平台: 返回销售人员列表。

    Returns:
        list[dict]: 每条含 sales_id / name / good_at_industries /
            responsible_cities / current_load / mobile。
    """
    sales = data_loader.load_sales()
    return [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in sales]


@app.post("/sales")
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


@app.patch("/sales/{sales_id}")
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


@app.delete("/sales/{sales_id}")
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


@app.get("/sales/{sales_id}/profile")
def get_sales_profile(sales_id: str) -> Dict[str, Any]:
    """「易销」平台: 基于 CRM 历史成交数据，通过大模型生成销售能力画像。"""
    try:
        profile = sales_profile_engine.analyze_sales_profile(sales_id.strip(), auto_sync_to_model=False)
        deals = data_loader.load_crm_deals(sales_id=sales_id.strip())
        profile["deals"] = deals
        return profile
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成销售画像失败: {exc}")


@app.post("/sales/{sales_id}/sync-profile")
def sync_sales_profile(sales_id: str) -> Dict[str, Any]:
    """「易销」平台: AI 分析销售成单历史并自动反哺更新其擅长行业与能力标签。"""
    try:
        profile = sales_profile_engine.analyze_sales_profile(sales_id.strip(), auto_sync_to_model=True)
        return {"status": "ok", "profile": profile}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步销售画像失败: {exc}")


@app.post("/sales/sync-all-profiles")
def sync_all_sales_profiles() -> Dict[str, Any]:
    """「易销」平台: 全员一键 AI 扫描 CRM 成交记录并同步画像图谱。"""
    try:
        results = sales_profile_engine.analyze_all_sales()
        return {"status": "ok", "synced_count": len(results), "profiles": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"批量同步销售画像失败: {exc}")


@app.post("/feishu/card-action")
async def feishu_card_action(request: Request) -> Dict[str, Any]:
    """接收并处理飞书卡片按钮点击回调事件 (Card Action Handler)。

    支持动作:
    1. accept_lead: 销售点击「✅ 立即接单跟进」 -> 更新客户归属, 实时更新卡片提示已接单;
    2. request_reassign: 销售点击「🔄 申请改派/转交」 -> 标记待人工处理/改派;
    3. log_note: 销售点击「📝 快速录入小记」 -> 记录跟进状态。
    """
    try:
        body = await request.json()
        logger.info("收到飞书卡片交互回调: %s", json.dumps(body, ensure_ascii=False))

        # 飞书 URL 校验 Challenge (配置 Webhook 请求网址时使用)
        if "challenge" in body:
            return {"challenge": body["challenge"]}

        # 飞书卡片回调 schema 2.0: 按钮点击信息在 event.action.value 下;
        # 兼容旧版回调(顶层 action)。两种都取, 以实际存在者为准。
        event_obj = body.get("event", {}) or {}
        action_data = body.get("action", {}) or event_obj.get("action", {}) or {}
        value = action_data.get("value", {}) or {}
        # 输入框(input)的值也会通过 value[输入框name] 一并带过来
        action_type = value.get("action", "")
        customer_id = value.get("customer_id", "")
        customer_name = value.get("customer_name", customer_id)
        operator_open_id = event_obj.get("operator", {}).get("open_id", "") or body.get("open_id", "")

        # 1. 接单动作
        if action_type == "accept_lead":
            # 用回调的 operator_open_id 反查当前销售 ID, 写为客户的真正归属
            operator_sales = _find_sales_by_open_id(operator_open_id) or {}
            owner_sid = operator_sales.get("sales_id") or ""
            # 将客户归属写入/更新
            try:
                customers = data_loader.load_customers()
                matched_cust = next((c for c in customers if c.customer_id == customer_id), None)
                if matched_cust:
                    # 归属写入实际销售 ID(优先按 open_id 反查; 查不到则保留原归属,
                    # 但不再写入状态文本, 避免污染 owner_sales_id)
                    if owner_sid:
                        matched_cust.owner_sales_id = owner_sid
                    elif matched_cust.owner_sales_id in ("已接单", "待改派"):
                        matched_cust.owner_sales_id = None
                    data_loader.save_customers(customers)
            except Exception as e:
                logger.warning("更新客户接单状态异常: %s", e)

            from modules import feishu_app_notifier
            accepted_card = feishu_app_notifier.build_accepted_card(customer_id, customer_name)

            # 卡片回调更新: 通过响应返回 card 让飞书原地替换当前卡片
            # (注: PUT /im/v1/messages 更新接口对卡片消息不支持, 已弃用)
            return _feishu_card_response(
                card=accepted_card,
                toast={"type": "success", "content": f"🎉 您已成功接单「{customer_name}」，请尽快跟进！"},
            )

        # 2. 申请改派 —— 第一步：把原卡片原地更新为「填写原因」表单卡片
        elif action_type == "request_reassign":
            from modules import feishu_app_notifier
            form_card = feishu_app_notifier.build_reassign_form_card(customer_id, customer_name)
            return _feishu_card_response(
                card=form_card,
                toast={"type": "info", "content": "请在上方输入框填写改派原因，然后点击「提交改派申请」"},
            )

        # 2b. 提交改派申请 —— 第二步：读取输入框原因，记录改派并原地更新卡片
        elif action_type == "submit_reassign":
            reason = value.get("reassign_reason", "") or ""
            logger.info("收到改派申请: customer=%s reason=%s operator=%s", customer_id, reason, operator_open_id)
            # 标记客户为待改派: 清除归属(待主管复核后重新分配), 不写入状态文本
            try:
                customers = data_loader.load_customers()
                matched_cust = next((c for c in customers if c.customer_id == customer_id), None)
                if matched_cust:
                    matched_cust.owner_sales_id = None
                    data_loader.save_customers(customers)
            except Exception as e:
                logger.warning("更新客户改派状态异常: %s", e)

            from modules import feishu_app_notifier
            submitted_card = feishu_app_notifier.build_reassign_submitted_card(
                customer_id, customer_name, reason=reason,
            )
            return _feishu_card_response(
                card=submitted_card,
                toast={"type": "success", "content": f"已提交「{customer_name}」的改派申请，主管将收到复核提醒"},
            )

        # 2c. 返回原卡片(取消填写)
        elif action_type == "cancel_reassign":
            from modules import feishu_app_notifier
            back_card = feishu_app_notifier.build_assignment_card(
                customer_id=customer_id,
                customer_name=customer_name,
                intention_level=value.get("intention_level", "中"),
                churn_risk=value.get("churn_risk", "中"),
                match_reason=value.get("match_reason", ""),
            )
            return _feishu_card_response(card=back_card, toast={"type": "info", "content": "已取消改派"})

        # 3. 录入小记 —— 第一步: 把原卡片原地更新为「录入小记」表单卡片
        elif action_type == "log_note":
            from modules import feishu_app_notifier
            note_form_card = feishu_app_notifier.build_note_form_card(customer_id, customer_name)
            return _feishu_card_response(
                card=note_form_card,
                toast={"type": "info", "content": "请在上方输入框填写本次跟进小记，提交后 AI 将重估意向"},
            )

        # 3b. 提交跟进小记 —— 第二步: 读取小记文本, AI 重估意向, 刷新卡片
        elif action_type == "submit_note":
            note_text = value.get("note_text", "") or ""
            logger.info("收到跟进小记: customer=%s note=%s operator=%s", customer_id, note_text[:50], operator_open_id)

            # 反查销售 ID(用于小记归属)
            operator_sales = _find_sales_by_open_id(operator_open_id) or {}
            sales_id = operator_sales.get("sales_id") or ""

            if not note_text.strip():
                return _feishu_card_response(
                    toast={"type": "error", "content": "跟进小记内容为空，请先填写再提交"},
                )

            # AI 动态重估意向等级 + 流失风险 + 跟进策略
            reanalysis = follow_up_notes.reanalyze_with_note(
                customer_id=customer_id,
                customer_name=customer_name,
                note_text=note_text,
                sales_id=sales_id,
                persist=True,
            )

            from modules import feishu_app_notifier
            analyzed_card = feishu_app_notifier.build_note_analyzed_card(
                customer_id, customer_name, note_text, reanalysis,
            )

            # 变化提示文案
            change = reanalysis.get("intention_change", "unchanged")
            if change == "upgrade":
                toast_msg = f"🎉 意向升级: {reanalysis['intention_before']} → {reanalysis['intention_after']}，请优先跟进！"
            elif change == "downgrade":
                toast_msg = f"📉 意向下调: {reanalysis['intention_before']} → {reanalysis['intention_after']}"
            else:
                toast_msg = f"✅ 小记已记录，意向保持 {reanalysis['intention_after']}"

            return _feishu_card_response(card=analyzed_card, toast={"type": "success", "content": toast_msg})

        # 4. 生成破冰话术 —— 调 Kimi K2.7 Code 生成微信/电话话术
        elif action_type == "gen_talk_track":
            track_type = value.get("track_type", "wechat") or "wechat"
            if track_type not in ("wechat", "phone"):
                track_type = "wechat"

            # 反查销售姓名(用于话术署名)
            operator_sales = _find_sales_by_open_id(operator_open_id) or {}
            sales_name = operator_sales.get("name") or operator_sales.get("sales_id") or ""

            from modules import feishu_app_notifier

            result = talk_track.generate_talk_track(customer_id, track_type, sales_name)
            talk_card = feishu_app_notifier.build_talk_track_card(
                customer_id, customer_name, track_type, result["content"], result["engine"],
            )

            if result["engine"] == "llm":
                toast_msg = f"🤖 已用 Kimi K2.7 Code 生成{'微信破冰' if track_type == 'wechat' else '电话话术'}，长按复制即可发送"
            else:
                toast_msg = f"已生成{'微信破冰' if track_type == 'wechat' else '电话话术'}（规则模板）"

            return _feishu_card_response(card=talk_card, toast={"type": "success", "content": toast_msg})

        return _feishu_card_response(toast={"type": "info", "content": "操作已记录"})
    except Exception as exc:
        logger.error("处理飞书卡片交互异常: %s", exc)
        return _feishu_card_response(toast={"type": "error", "content": f"处理失败: {exc}"})


@app.get("/pipeline/summary")
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
    import json as _json
    records: List[dict] = []
    for row in all_rows:
        try:
            result = _json.loads(row.result_json)
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


# ============================================================
# 端点: 飞书多维表格(Bitable) <-> 易销 双向同步
# ============================================================


@app.post("/api/sync/bitable/pull")
def sync_bitable_pull(dry_run: bool = False) -> Dict[str, Any]:
    """反向同步: 飞书 Bitable -> 易销(拉取表格变更回写本地)。

    说明: 飞书 Bitable 无官方变更 Webhook, 采用拉取式全量比对:
    把飞书表格里「跟进状态 / 归属销售 / 新增线索」的差异回写到本地
    mock_customers.json, 实现双向同步的"飞书改 -> 易销感知"。

    Args:
        dry_run: 为 true 时仅预览差异, 不落库。

    Returns:
        dict: {direction, pulled, status_updated, owner_updated, new_customers, changes}。
    """
    try:
        return bitable_sync.pull_from_bitable(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bitable 反向同步失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bitable 反向同步失败: {exc}")


@app.post("/api/sync/bitable/push")
def sync_bitable_push(dry_run: bool = False) -> Dict[str, Any]:
    """正向同步: 易销 -> 飞书 Bitable(把本地客户 + AI 画像推送到表格)。

    Args:
        dry_run: 为 true 时仅预览差异, 不写表格。

    Returns:
        dict: {direction, created, updated, changes}。
    """
    try:
        return bitable_sync.push_to_bitable(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bitable 正向同步失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bitable 正向同步失败: {exc}")


@app.post("/api/sync/bitable")
def sync_bitable_both(dry_run: bool = False) -> Dict[str, Any]:
    """双向同步: 先 pull(飞书->本地) 再 push(本地->飞书), 实现闭环。

    Returns:
        dict: {pull: {...}, push: {...}}。
    """
    try:
        return bitable_sync.sync_both(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("Bitable 双向同步失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"Bitable 双向同步失败: {exc}")


# ============================================================
# 入口: 直接 python api.py 启动
# ============================================================


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=8000)