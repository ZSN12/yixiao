# -*- coding: utf-8 -*-
"""FastAPI 服务装配层(api.py): 创建 app、注册路由、统一异常处理、启动服务。

业务路由已按域拆分到 api_routes/ 子包:
- auth.py         : 用户认证(登录 / 登出 / 当前用户)。
- data_sources.py : 数据源接入中心 CRUD。
- pipeline.py     : 流水线运行 / 画像历史 / 最近运行摘要。
- customers.py    : 客户列表 / 销售团队 / 销售画像 / 移动端我的客户 / 记忆列表。
- notes.py        : 跟进小记 / AI 话术 / 人工复核反馈。
- sla.py          : SLA 超时预警 + 自动流转公海。
- feishu.py       : 飞书卡片按钮交互回调。
- bitable.py      : 飞书多维表格双向同步。
- static.py       : 静态资源托管 + 根路由 + 健康检查。

本文件只负责: 生命周期(种子数据) → 创建 app → CORS → 注册各 router →
全局异常处理 → 挂载静态资源 → uvicorn 入口。

启动: python api.py  -> uvicorn 127.0.0.1:8000
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 项目根目录(本文件位于 <项目根>/api.py)加入 sys.path, 保证任何 cwd 下可运行
PROJECT_ROOT: Path = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import data_source_registry  # noqa: E402
from modules import user_auth  # noqa: E402

from api_routes import auth, bitable, customers, data_sources, feishu, notes, pipeline, sla, static  # noqa: E402
from api_routes.common import _global_exception_handler  # noqa: E402

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用启动时确保种子数据就绪(超级管理员 + 预置数据源)。

    使用 FastAPI lifespan 事件(替代已废弃的 on_event)。
    """
    try:
        user_auth.ensure_seed_admin()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动时创建种子管理员失败: %s", exc)
    try:
        data_source_registry.ensure_seed_sources()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动时写入预置数据源失败: %s", exc)
    yield


app = FastAPI(title="销售线索智能分析与分发助手 API", version="1.0.0", lifespan=_lifespan)

# CORS: 允许本地前端调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 注册各业务域路由 ----
app.include_router(auth.router)
app.include_router(data_sources.router)
app.include_router(pipeline.router)
app.include_router(customers.router)
app.include_router(notes.router)
app.include_router(sla.router)
app.include_router(feishu.router)
app.include_router(bitable.router)
app.include_router(static.router)

# ---- 全局异常处理(统一返回 {"detail": ...}) ----
app.add_exception_handler(Exception, _global_exception_handler)

# ---- 挂载静态资源目录 ----
static.mount_static(app)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=8000)
