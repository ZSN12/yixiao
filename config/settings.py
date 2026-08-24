# -*- coding: utf-8 -*-
"""本模块是全项目配置中心:所有业务模块(数据层/画像分析/线索分配/钉钉推送/飞书推送/调度编排)统一从这里读取配置。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="config/.env", env_file_encoding="utf-8", extra="ignore")
    # 大模型(OpenAI 兼容接口, 支持 DeepSeek/通义等)
    llm_api_base: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: float = 60.0
    # Kimi K2.7 Code(Anthropic Messages 接口, 用于话术生成等语义任务);
    # base_url 形如 https://api.kimi.com/coding, 实际请求追加 /v1/messages。
    kimi_api_base: str = "https://api.kimi.com/coding"
    kimi_api_key: str = ""          # 对应环境变量 KIMI_CODING_API_KEY
    kimi_model: str = "kimi-for-coding"   # Kimi K2.7 Code
    # 钉钉自定义机器人
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""   # 加签密钥, 可选
    # 飞书自定义机器人 (Webhook 群消息)
    feishu_webhook_url: str = ""
    feishu_secret: str = ""     # 加签密钥, 可选
    # 飞书企业自建应用 (发送个人工作通知单聊卡片)。
    # 注意: 凭证属于敏感信息, 请勿写死在源码中, 统一在 config/.env 配置
    # (本仓库 .gitignore 已忽略 .env)。未配置时个人工作通知自动跳过。
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # 易销网页应用公网地址(销售从飞书卡片/网页应用进入易销移动端的入口; 注意 trycloudflare 临时地址重启会变)
    feishu_webapp_url: str = ""
    # 飞书多维表格(Bitable)同步配置: base_token 与各表 table_id, 用于双向实时同步
    feishu_base_token: str = "MMN8bUDu6aLOLEs9UHOcDZaanMf"
    feishu_base_leads_table: str = "tbluJCdsnsMaWYYD"      # 客户线索池
    feishu_base_sales_table: str = "tbl9XxtsQVMt30Gr"      # 销售团队画像
    feishu_base_memory_table: str = "tblJeTx2w24LvQdc"     # 分配记录与记忆中心
    # 通知通道统一入口选择的默认通道: "feishu" | "dingtalk"(谁配了 webhook 用谁, 都没配打印提示)
    notifier_channel: str = "feishu"
    # 销售人员 ID -> 飞书绑定手机号映射 (用于一对一定向推送工作通知);
    # 含个人手机号, 属敏感信息, 请在 config/.env 配置。
    sales_mobile_map: str = ""
    # 数据库
    db_path: str = "data/sales_agent.db"
    # 调度
    daily_run_time: str = "08:30"
    # Mock 模式: True 时画像分析走规则引擎(不调大模型), 开箱即跑; api_key 为空时自动视为 mock
    mock_mode: bool = True
    # RAG 检索 - embedding(可选): 配置了就走 API 向量检索, 未配置则用零依赖本地文本相似度兜底
    embedding_api_base: str = ""    # 如 "https://api.openai.com/v1" 或通义等兼容端点
    embedding_api_key: str = ""
    embedding_model: str = ""       # 如 "text-embedding-3-small"


settings = Settings()