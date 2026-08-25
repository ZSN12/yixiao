# -*- coding: utf-8 -*-
"""本模块是全项目配置中心:所有业务模块(数据层/画像分析/线索分配/钉钉推送/飞书推送/调度编排)统一从这里读取配置。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="config/.env", env_file_encoding="utf-8", extra="ignore")
    # 统一 LLM 网关(llm_client)的 provider 选择: "kimi" | "openai"
    # - kimi:   Anthropic Messages 接口(api.kimi.com/coding 等), 当前默认;
    # - openai: OpenAI 兼容 Chat Completions(OpenAI/DeepSeek/通义/vLLM 等), 预留。
    llm_provider: str = "kimi"
    # 大模型(OpenAI 兼容接口, 支持 DeepSeek/通义等)—— llm_provider="openai" 时生效
    llm_api_base: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: float = 60.0
    # Kimi K2.7 Code(Anthropic Messages 接口, 用于话术生成等语义任务);
    # base_url 形如 https://api.kimi.com/coding, 实际请求追加 /v1/messages。
    kimi_api_base: str = "https://api.kimi.com/coding"
    kimi_api_key: str = ""          # 对应环境变量 KIMI_API_KEY
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
    # 飞书多维表格(Bitable)同步配置: base_token 属访问凭证, 请在 config/.env 配置,
    # 勿写死在源码中; table_id 非敏感, 可给默认值。
    feishu_base_token: str = ""
    feishu_base_leads_table: str = "tbluJCdsnsMaWYYD"      # 客户线索池
    feishu_base_sales_table: str = "tbl9XxtsQVMt30Gr"      # 销售团队画像
    feishu_base_memory_table: str = "tblJeTx2w24LvQdc"     # 分配记录与记忆中心
    # 通知通道统一入口选择的默认通道: "feishu" | "dingtalk"(谁配了 webhook 用谁, 都没配打印提示)
    notifier_channel: str = "feishu"
    # 销售人员 ID -> 飞书绑定手机号映射 (用于一对一定向推送工作通知);
    # 含个人手机号, 属敏感信息, 请在 config/.env 配置。
    sales_mobile_map: str = ""
    # HTTPS 证书校验: False 时兼容内网代理/自签名证书环境(默认), 生产环境建议设 True
    verify_ssl: bool = False
    # 数据库
    db_path: str = "data/sales_agent.db"
    # 调度
    daily_run_time: str = "08:30"
    # Mock 模式: True 时「飞书个人通知」等主动推送静默跳过, 避免测试/演示产生真实外呼;
    # 画像分析/销售画像/话术等语义任务是否走 Kimi 取决于 kimi_api_key 是否配置
    # (未配置 key 时自动降级规则引擎)。
    mock_mode: bool = True
    # RAG 检索 - embedding(可选): 配置了就走 API 向量检索, 未配置则用零依赖本地文本相似度兜底
    embedding_api_base: str = ""    # 如 "https://api.openai.com/v1" 或通义等兼容端点
    embedding_api_key: str = ""
    embedding_model: str = ""       # 如 "text-embedding-3-small"
    # 电话录音 ASR(adapters/phone_call_adapter): 语音转写 + 说话人角色判定
    asr_provider: str = "mock"           # mock | aliyun | tencent | xfyun
    asr_api_key: str = ""
    asr_api_secret: str = ""
    asr_app_id: str = ""                 # 讯飞等需要
    asr_timeout: float = 120.0
    phone_role_llm_fallback: bool = True  # Tier-3 是否启用 LLM 角色兜底
    phone_role_min_confidence: float = 0.7  # 低于此值打 review 标记


settings = Settings()