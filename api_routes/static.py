# -*- coding: utf-8 -*-
"""静态资源托管 + 根路由 + 健康检查 路由。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

# 静态目录: <项目根>/static
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
STATIC_DIR: Path = PROJECT_ROOT / "static"

router = APIRouter(tags=["static"])


@router.get("/health")
def health() -> Dict[str, str]:
    """健康检查。

    Returns:
        dict: {"status": "ok"}。
    """
    return {"status": "ok"}


@router.get("/")
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


@router.get("/m/")
def mobile_index() -> FileResponse:
    """销售移动端入口页: 返回 static/m.html(手机友好 H5)。

    销售从飞书网页应用/工作台进入该地址(可携带 ?open_id=xxx), 页面会
    自动用 URL 里的 open_id 调 /api/my/customers 拉取自己名下的客户。
    """
    m_file = STATIC_DIR / "m.html"
    if not m_file.exists():
        raise HTTPException(status_code=404, detail="static/m.html 不存在, 请检查前端文件。")
    return FileResponse(str(m_file), media_type="text/html; charset=utf-8")


def mount_static(app: Any) -> None:
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
