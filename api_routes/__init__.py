# -*- coding: utf-8 -*-
"""API 路由子包: 按业务域拆分 FastAPI 路由, 便于维护。

各模块职责:
- common.py       : 公共工具(全局异常处理 / 统一响应 / 飞书卡片响应 / 客户列表 join 等)。
- auth.py         : 用户认证(登录 / 登出 / 当前用户)。
- data_sources.py : 数据源接入中心 CRUD。
- pipeline.py     : 流水线运行 / 画像历史 / 最近运行摘要。
- customers.py    : 客户列表 / 销售团队 CRUD / 销售画像 / 移动端我的客户 / 记忆列表。
- notes.py        : 跟进小记 / AI 话术 / 人工复核反馈。
- sla.py          : SLA 超时预警 + 自动流转公海。
- feishu.py       : 飞书卡片按钮交互回调。
- bitable.py      : 飞书多维表格双向同步。
- phone_webhook.py: 电话录音 webhook 回调 + 低置信度角色复核。
- static.py       : 静态资源托管 + 根路由 + 健康检查。
"""
