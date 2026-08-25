# 易销 · 销售线索智能分析与分发助手

> 一句话定位: 一套带 **Web 看板 + 飞书销售端 + 每日自动流水线** 的 B 端销售线索智能分析分发系统 —— 把「企客宝客户 + 电话语音 → 会话画像 → 意向分层 → RAG 线索匹配 → 飞书/钉钉触达 → SLA 超时流转公海」串成一条可闭环、可降级、开箱即跑的流水线。

---

## 这是什么？

「易销」是「销售线索智能分析与分发助手」，核心解决三个问题：

1. **画像**: 从客户对话记录中提炼意向等级 / 流失风险 / 核心需求 / 跟进建议（LLM 与规则双引擎）。
2. **分发**: 用「规则硬约束 + RAG 语义匹配 + 记忆反哺 + 负载均衡」把线索精准分给最合适的销售。
3. **闭环**: 通过飞书卡片让销售接单 / 录入小记 / 生成话术，AI 动态重估意向；SLA 超时自动流转公海。

---

## 系统组成

| 端 | 说明 | 入口 |
| --- | --- | --- |
| **运营看板（Web 桌面端）** | 工作台 / 客户画像 / 智能分配 / 销售团队 / 记忆中心 / 数据接入 | `http://127.0.0.1:8000/` |
| **销售移动端（H5）** | 销售在飞书/手机里看自己名下的客户画像 | `/m/`（按 UA 自动跳转） |
| **飞书卡片** | 销售在飞书里接单 / 改派 / 录小记 / 生成话术 | 卡片按钮回调 `/feishu/card-action` |
| **REST API** | 上述全部能力 + 数据源中心 CRUD | 见下文「API 一览」 |

登录账号（首次启动自动种子）: **`admin` / `123456`**（超级管理员）。

---

## 架构图 (ASCII)

```
                        ┌──────────────────────────────────────────────┐
                        │               main.py (门面)                   │
                        │   --run-once 一次性执行 / APScheduler 每日定时  │
                        └──────────────┬───────────────────────────────┘
                                       │ run_pipeline_graph()
                        ┌──────────────▼───────────────────────────────┐
                        │   orchestrator/language_graph_flow.py        │
                        │   LangGraph StateGraph 多智能体编排            │
                        │   (langgraph 不可用时降级顺序直调)              │
                        └──┬──────────────┬──────────────┬─────────────┘
                           │              │              │
              ┌────────────▼───┐  ┌───────▼─────────┐  ┌▼────────────────┐
              │ 分析师 Agent   │  │ 匹配师 Agent    │  │ 推送员 Agent     │
              │ image_profiling│  │ lead_matching   │  │ report_push     │
              └────────────┬───┘  └───────┬─────────┘  └┬────────────────┘
                           │              │              │
              ┌────────────▼───┐  ┌───────▼─────────┐  ┌▼────────────────┐
              │ profile_analyzer│ │ lead_assigner   │  │ notify / feishu  │
              │ (LLM/规则双引擎) │  │ (规则+RAG+记忆) │  │ (日报+分配+卡片) │
              └────────────┬───┘  └───────┬─────────┘  └┬────────────────┘
                           │              │              │
              ┌────────────▼──────────────▼──────────────▼──────────────┐
              │ 数据层 data_loader: Customer/ChatRecord/Sales/经验 +      │
              │ SQLite(analysis_history / users / data_sources / 记忆)   │
              └─────────────────────────────────────────────────────────┘
```

**Web 服务层**（`api.py` 装配 + `api_routes/` 按域拆分的路由）提供看板与飞书交互的 HTTP 接口，与上面的离线流水线共享同一套业务模块。

---

## 模块清单

| 模块 | 职责 |
| --- | --- |
| `config/settings.py` | 全项目配置中心（pydantic-settings 单例 `settings`，读 `config/.env`） |
| `modules/data_loader.py` | 数据模型 + JSON 加载 + SQLite 持久化（analysis_history 等） |
| `modules/profile_analyzer.py` | 客户画像 / 意向分层 / 流失风险（LLM 优先，失败降级规则引擎） |
| `modules/rag_retriever.py` | 销售经验 RAG 检索（embedding API 或本地 2-gram 兜底） |
| `modules/lead_assigner.py` | 规则硬约束 + RAG 软排序 + 记忆反哺 + 负载均衡分配 |
| `modules/agent_memory.py` | 记忆中心（弱记忆 / 强记忆升级 / 相似检索） |
| `modules/follow_up_notes.py` | 跟进小记落库 + AI 动态重估意向 |
| `modules/talk_track.py` | AI 破冰话术生成（微信 / 电话） |
| `modules/sales_profile_engine.py` | 销售能力画像（基于 CRM 成交历史） |
| `modules/experience_refinery.py` | 成交商机 → 销售经验片段提炼沉淀 |
| `modules/sla_monitor.py` | SLA 超时预警 + 自动流转公海 |
| `modules/data_source_registry.py` | 数据源接入中心（SQLite 持久化 CRUD） |
| `modules/user_auth.py` | 用户认证（账号密码 + 进程内 token 会话） |
| `modules/llm_client.py` | 统一 LLM 网关（Kimi / OpenAI 兼容可插拔） |
| `modules/kimi_client.py` | Kimi（Anthropic Messages 接口）底层封装 |
| `modules/notify.py` | 通知通道统一入口（feishu / dingtalk 自动选择） |
| `modules/dingtalk_notifier.py` | 钉钉群机器人（HMAC-SHA256 加签） |
| `modules/feishu_notifier.py` | 飞书群机器人（加签） |
| `modules/feishu_app_notifier.py` | 飞书企业自建应用（个人卡片 / SLA 预警） |
| `orchestrator/language_graph_flow.py` | LangGraph 多智能体状态图编排 + 顺序降级 |
| `adapters/crm_data_adapter.py` | 真实 CRM CSV / 企微 JSON 适配器（同构接入实证） |
| `api.py` | FastAPI 装配层（注册路由 / 生命周期 / 异常处理） |
| `api_routes/` | 按业务域拆分的路由（auth / customers / pipeline / feishu / …） |
| `main.py` | 离线门面：`--run-once` / APScheduler 每日定时 |

---

## 快速开始

> 运行环境: **Python 3.13**（langgraph 1.2.x 在 3.10/3.11/3.12 上会因 `TypedDict(extra_items=...)` 不兼容而导入失败）。推荐 uv。

```bash
# 1) 创建虚拟环境并安装依赖
uv venv .venv --python 3.13
uv pip install -p .venv/bin/python -r requirements.txt -r requirements-dev.txt
source .venv/bin/activate

# 2) (可选) 复制环境配置 —— 默认即 mock 模式零成本跑通
cp config/.env.example config/.env

# 3) 启动 Web 服务(运营看板 + API)
python api.py
#    → 打开 http://127.0.0.1:8000/  账号 admin / 123456

# 4) 或离线跑一次完整流水线(mock 模式, 无需任何 API Key)
python main.py --run-once
python main.py --run-once --skip-push   # 跳过推送, 数据仍落库
python main.py                          # 每日定时模式(默认 08:30)
```

开箱即跑：未配置任何 LLM Key 时，画像分析 / 话术 / 经验提炼均走规则引擎；配置 `KIMI_API_KEY` 后自动切换 Kimi K2.7 Code；也可设 `LLM_PROVIDER=openai` 接任意 OpenAI 兼容大模型（DeepSeek / 通义等）。

---

## 目录结构

```
sales-agent/
├── main.py                          # 离线门面: --run-once / APScheduler 定时
├── api.py                           # FastAPI 装配层(薄)
├── api_routes/                      # 按业务域拆分的路由
│   ├── common.py                    # 公共工具(异常处理/统一响应/客户列表 join)
│   ├── auth.py                      # 登录 / 登出 / me
│   ├── data_sources.py              # 数据源中心 CRUD
│   ├── pipeline.py                  # 流水线 / 画像历史 / 摘要
│   ├── customers.py                 # 客户 / 销售团队 / 销售画像 / 我的客户
│   ├── notes.py                     # 小记 / 话术 / 反馈
│   ├── sla.py                       # SLA 预警流转
│   ├── feishu.py                    # 飞书卡片回调
│   └── static.py                    # 静态资源 / 根路由 / health
├── config/
│   ├── settings.py                  # pydantic-settings 配置中心
│   └── .env.example                 # 环境变量示例
├── data/                            # mock 数据 + real/ 真实数据示例
├── modules/                         # 业务模块(见「模块清单」)
├── orchestrator/
│   └── language_graph_flow.py       # LangGraph 多智能体状态图
├── adapters/
│   └── crm_data_adapter.py          # 真实 CRM/企微 适配器
├── static/                          # 前端(看板 + 移动端 + 样式 + Vue)
└── tests/                           # pytest 单测
```

---

## 功能亮点

- **LangGraph 多智能体状态图**: 分析师 → 匹配师 → 推送员三个专职节点共享状态对象，节点间只通过 state 流转，可单独 import 测试。
- **可插拔 LLM 网关（`modules/llm_client`）**: 业务模块只依赖 `chat` / `chat_json` / `enabled` 三个接口，底层按 `LLM_PROVIDER` 在 Kimi（Anthropic Messages）与 OpenAI 兼容（Chat Completions）间切换，换大模型零业务改动。
- **三级降级**: langgraph 不可用 → 顺序直调；LLM 失败 → 规则引擎；单环节失败 → 记错误继续，全流程不崩溃。
- **RAG 检索 + 记忆反哺**: 规则硬约束 + 语义相似度粗排 + 规则精排；人工复核反馈升级为强记忆，持续影响后续分配。
- **对话上下文理解**: 画像分析区分说话者，客户主动表达的意向/价格才计为真实信号（销售话术不与客户意图混同）。
- **价格时间衰减**: 客户报价按 7 天新鲜度窗口衰减，超过 7 天视为过期意向价格、降权参与评分并在画像/原因中显式标注。
- **飞书闭环**: 卡片接单 / 改派 / 录小记 AI 重估 / 破冰话术，全部原地更新卡片。
- **SLA 守护**: 超时自动流转公海，预警/超时飞书卡片提醒销售。
- **数据源接入中心**: 6 类数据源（企客宝 CRM / CSV / 企微会话 / CRM 接口 / 自定义 Webhook / 电话录音）可增删改查，SQLite 持久化；企客宝为主数据源，mock/CSV/企微兜底。
- **确定性可复现**: mock 模式全规则引擎，意向分层（高/中/低）与流失分层完全确定，便于验收与回归。

---

## 配置说明

所有配置集中在 `config/settings.py`（pydantic-settings 单例 `settings`），每个字段可被 `config/.env` 或同名环境变量覆盖。

| 环境变量 | settings 字段 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | `llm_provider` | `kimi` | LLM 网关后端: `kimi`(Anthropic Messages) / `openai`(OpenAI 兼容) |
| `LLM_API_BASE` | `llm_api_base` | `https://api.deepseek.com/v1` | OpenAI 兼容 Base URL（`LLM_PROVIDER=openai` 时生效） |
| `LLM_API_KEY` | `llm_api_key` | `""` | OpenAI 兼容 API Key |
| `LLM_MODEL` | `llm_model` | `deepseek-chat` | OpenAI 兼容模型名 |
| `LLM_TIMEOUT` | `llm_timeout` | `60.0` | 大模型调用超时（秒） |
| `KIMI_API_BASE` | `kimi_api_base` | `https://api.kimi.com/coding` | Kimi Anthropic Messages 端点（实际追加 `/v1/messages`） |
| `KIMI_API_KEY` | `kimi_api_key` | `""` | Kimi API Key；配置后画像/销售画像/话术走 Kimi |
| `KIMI_MODEL` | `kimi_model` | `kimi-for-coding` | Kimi 模型名 |
| `FEISHU_WEBHOOK_URL` | `feishu_webhook_url` | `""` | 飞书群机器人 Webhook |
| `FEISHU_SECRET` | `feishu_secret` | `""` | 飞书群机器人加签密钥 |
| `DINGTALK_WEBHOOK_URL` | `dingtalk_webhook_url` | `""` | 钉钉群机器人 Webhook |
| `DINGTALK_SECRET` | `dingtalk_secret` | `""` | 钉钉群机器人加签密钥 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `feishu_app_id` / `feishu_app_secret` | `""` | 飞书企业自建应用凭证（个人卡片 / SLA 预警） |
| `FEISHU_WEBAPP_URL` | `feishu_webapp_url` | `""` | 易销网页应用公网地址 |
| `NOTIFIER_CHANNEL` | `notifier_channel` | `feishu` | 通知通道默认选择: `feishu` / `dingtalk` |
| `SALES_MOBILE_MAP` | `sales_mobile_map` | `""` | 销售 ID→手机号映射（一对一工作通知） |
| `VERIFY_SSL` | `verify_ssl` | `False` | HTTPS 证书校验 |
| `DB_PATH` | `db_path` | `data/sales_agent.db` | SQLite 数据库路径 |
| `DAILY_RUN_TIME` | `daily_run_time` | `08:30` | 每日定时运行时间 |
| `MOCK_MODE` | `mock_mode` | `True` | `True` 时飞书个人通知等主动推送静默跳过 |
| `EMBEDDING_API_BASE` | `embedding_api_base` | `""` | Embedding API 端点（可选，配置后走向量检索） |
| `EMBEDDING_API_KEY` | `embedding_api_key` | `""` | Embedding API Key |
| `EMBEDDING_MODEL` | `embedding_model` | `""` | Embedding 模型名 |
| `QIKEBAO_CLIENT_ID` | `qikebao_client_id` | `""` | 企客宝应用 Client ID（主数据源） |
| `QIKEBAO_CLIENT_SECRET` | `qikebao_client_secret` | `""` | 企客宝应用 Client Secret |
| `QIKEBAO_CORP_ID` | `qikebao_corp_id` | `""` | 企客宝企业 ID（多企业时必填） |
| `QIKEBAO_API_BASE` | `qikebao_api_base` | `""` | 企客宝 API 地址（空则运行时按文档默认） |
| `QIKEBAO_TOKEN_URL` | `qikebao_token_url` | `https://sso.yunshouzhi.net/connect/token` | 企客宝 token 端点 |
| `QIKEBAO_SYNC_ENABLED` | `qikebao_sync_enabled` | `False` | `True` 且凭证齐全时走企客宝 |
| `QIKEBAO_MOCK_MODE` | `qikebao_mock_mode` | `True` | `True` 读 sample JSON，不调外网 |
| `QIKEBAO_SYNC_CHAT` | `qikebao_sync_chat` | `False` | P1: 是否同步企客宝会话存档 |
| `QIKEBAO_CUSTOMER_ID_PREFIX` | `qikebao_customer_id_prefix` | `QKB-` | 客户 ID 前缀，防与 mock 冲突 |

> **企客宝两个开关怎么配合（`QIKEBAO_SYNC_ENABLED` × `QIKEBAO_MOCK_MODE`）**
>
> | `SYNC_ENABLED` | `MOCK_MODE` | 实际行为 |
> |---|---|---|
> | `False`（默认） | `True`（默认） | 读 `sample/` mock JSON，不调外网 —— 开箱即跑 |
> | `False` | `False` | 企客宝完全关闭，走 CSV / 企微 / 其他兜底数据源 |
> | `True` | `True` | 凭证齐全则走企客宝；请求失败/凭证缺失时**降级**读 mock，不中断 |
> | `True` | `False` | 强制走企客宝真实接口（推荐联调上线时用），失败则报错/空列表，不静默回 mock |
>
> 生产建议：联调阶段用 `True / False` 看清楚真实响应，确认字段映射后切回 `True / True`
> 以获得「主数据源失败不崩」的兜底。凭证见 `QIKEBAO_CLIENT_ID / SECRET / CORP_ID`。

### 飞书一键登录（OAuth）配置

登录页「飞书一键登录」让销售子账号免密登录。启用步骤：

1. 在飞书开放平台创建企业自建应用，开通 **网页应用** 能力；
2. 在应用的「安全设置 → 重定向 URL」中注册回调地址：
   **`<FEISHU_WEBAPP_URL>/#/feishu-oauth`**
   （`FEISHU_WEBAPP_URL` 是易销网页应用的公网地址，即前端实际访问的域名，
   注意它必须与浏览器当前访问地址同源，否则飞书授权会失败）；
3. 把 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 填到 `config/.env`；
4. 登录页勾选可用后，点「飞书一键登录」→ 跳飞书授权 → 授权成功后自动回调到
   `#/feishu-oauth`，由前端 `handleFeishuOauthCallback()` 换取会话并登录。

> 未配置 `FEISHU_APP_ID` 或前端不在 `FEISHU_WEBAPP_URL` 下访问时，登录页会隐藏该按钮，
> 不影响超管（admin/123456）与普通账号登录。

### 大模型接入与切换（界面可视化配置）

系统支持直接在 Web 看板 **「数据与模型接入」** 页面中可视化添加、编辑、切换与测试大模型：
- **预置模板**: 预置 **DeepSeek** (`deepseek-chat`)、**Kimi** (`moonshot-v1-32k`)、**小米 MiMo** (`mimo-7b-instruct`) 等常用模型；
- **自定义 OpenAI 兼容端点**: 填入任意提供商的 `API Base URL`、`API Key` 和 `Model 名称`（如通义千问、智谱 GLM、本地 Ollama/vLLM）即可接入；
- **动态生效**: 超级管理员点击「设为启用」即可实时切换主力大模型，无需修改 `.env` 或重启后端；
- **一键测试连通**: 提供「测试连通」功能，直接在界面检验当前填写的 Key 与 Base 是否可连通；
- **未配置自动降级**: 当未填写 Key 或所有模型调用失败时，画像分析自动回退到规则引擎（关键词打分），流水线不中断。

---

## API 一览

> 由 `api.py` 装配，路由按域拆在 `api_routes/` 下。完整端点见各路由文件，核心分组如下：

| 域 | 关键端点 | 说明 |
| --- | --- | --- |
| 健康 | `GET /health` | 健康检查 |
| 认证 | `POST /api/login` / `POST /api/logout` / `GET /api/me` | 登录 / 登出 / 当前用户 |
| 流水线 | `POST /pipeline/run` | 手动触发完整流水线并落库 |
| | `GET /history/{customer_id}` | 某客户画像历史快照 |
| | `GET /pipeline/summary` | 最近一次运行摘要 |
| 客户 | `GET /customers` | 全量客户（含意向/流失等级，支持筛选） |
| | `GET /api/my/customers` | 销售移动端「我的客户」（按 open_id） |
| 销售 | `GET/POST /sales`、`PATCH/DELETE /sales/{id}` | 销售团队 CRUD |
| | `GET /sales/{id}/profile`、`POST /sales/{id}/sync-profile` | 销售画像 |
| | `POST /sales/sync-all-profiles` | 全员画像同步 |
| 记忆 | `GET /memories` | 记忆列表（学习效果回放） |
| | `POST /feedback` | 人工复核反馈（升级强记忆） |
| 小记/话术 | `GET/POST /follow-up-notes*`、`GET /talk-track/{id}` | 跟进小记 + AI 话术 |
| SLA | `GET /sla/status`、`POST /sla/check` | SLA 预警 + 流转公海 |
| 飞书 | `POST /feishu/card-action` | 飞书卡片按钮回调 |
| 数据源 | `GET /api/data-sources` / `GET /api/data-sources/types`、`POST/PATCH/DELETE /api/data-sources[/{id}]` | 数据源接入中心 CRUD |
| 静态 | `GET /`、`GET /m/`、`/static/*` | 看板 / 移动端 / 静态资源 |

---

## 测试

```bash
pip install pytest && pytest -v
```

pytest 单测全量跑（mock 模式、临时 DB 隔离、不依赖真实 LLM/网络/钉钉/飞书）；GitHub Actions 已在 push/PR 时自动执行（Python 3.10 / 3.11 矩阵）。`tests/` 下另有两个历史自检脚本（`test_*_selfcheck.py`），由 `tests/conftest.py` 的 `collect_ignore` 排除出 pytest。

---

## FAQ

**Q1: mock 模式不花钱，是怎么做到的？**

画像分析不调大模型 —— `MOCK_MODE=True`（或未配置 LLM key）时，`profile_analyzer` 走规则引擎，按聊天记录关键词命中计数打分（意向分 = 意向加分 − 意向减分 − 流失加分），阈值映射为 高/中/低，并附带画像文本 / 核心需求 / 跟进建议；RAG 匹配用零依赖字符 2-gram 本地相似度；飞书/钉钉未配置 webhook 时打印提示并跳过推送（数据仍落库）。实测 `python main.py --run-once` 输出：客户 **9** 家，意向 **高6/中1/低2**，流失 **高2/中2/低5**，分配 **5** 条。

**Q2: 匹配度是怎么算的？**

`rag_retriever` 两级检索：① 粗排 —— 配置了 `EMBEDDING_*` 走 API 余弦相似度，否则用本地字符 2-gram Jaccard / 余弦相似度取 Top-K；② 精排 —— 规则加权（行业命中擅长行业 +0.15、命中经验 industry +0.10、领域关键词命中 0.02/个封顶 0.10），综合分 = similarity × (1 + rule_bonus) 封顶 1.0。`lead_assigner` 再叠加硬约束（行业 ∈ 擅长行业 → 候选1，否则城市 ∈ 负责城市 → 候选2）、记忆反哺与 RAG Top1 融合决策，同分取 `current_load` 最小者负载均衡。

**Q3: 钉钉/飞书加签是怎么实现的？**

`dingtalk_notifier` / `feishu_notifier` 的 `_generate_sign` 严格按平台规范：毫秒时间戳 + secret 拼字符串，HMAC-SHA256 摘要 → Base64 → URL 编码，追加到 webhook。全程标准库 `hmac / hashlib / base64 / urllib`，无第三方依赖。

**Q4: langgraph 不可用会怎样？**

`orchestrator` 对 langgraph 宽松导入：未安装 / 导入失败 / 图编译异常时，`run_pipeline_graph` 自动降级 `run_pipeline_sequential` 顺序直调（分析师 → 匹配师 → 推送员 → 汇总），输出状态与图模式完全同构。

**Q5: 怎么接真实 CRM？**

**企客宝（主数据源，推荐）**：`adapters/qikebao_client.py`（`urllib` 零第三方 HTTP 客户端，含 token 缓存 + 401 自动刷新）+ `adapters/qikebao_adapter.py`（原始 dict → `Customer`/`ChatRecord` 字段映射）。在 `config/.env` 配置 `QIKEBAO_CLIENT_ID` / `QIKEBAO_CLIENT_SECRET` / `QIKEBAO_CORP_ID`，并设 `QIKEBAO_SYNC_ENABLED=true`（`QIKEBAO_MOCK_MODE=false`）即走真实企客宝；客户 ID 加 `QKB-` 前缀防与 mock `C001` 冲突。未启用时自动回退 mock/CSV/企微兜底，系统行为与原先一致。字段映射为初版（见 `map_customer` docstring），拿到真实响应后仅需微调 `qikebao_adapter.py`，调用方零改动。演示：`python adapters/qikebao_adapter.py`（mock 样例 `data/real/qikebao_customers_sample.json`）。

`adapters/crm_data_adapter.py` 提供兜底实证：`load_customers_from_csv`（CRM 导出 CSV → `Customer`，脏数据兜底）、`load_chat_records_from_export`（企微会话存档 JSON → `ChatRecord`）。演示数据在 `data/real/`（`crm_customers.csv` + `wework_chat_export.json`）。同时，「数据接入」页提供了可配置的数据源中心（企客宝 CRM / CSV / 企微会话 / CRM 接口 / 飞书多维表 / 自定义 Webhook / 电话录音 七类），可增删改查并持久化。

**Q6: 登录态是怎么持久化的？**

前端 token 存 `localStorage`（关闭浏览器再打开仍保持登录）；后端 token 会话存进程内内存（`user_auth._SESSIONS`），关机/重启后端后需重新登录。默认 token 有效期 24 小时。

---

## 已知待增强（Roadmap）

- [x] **对话上下文语义理解**: 画像分析已区分说话者（客户主动表达的意向/价格才计为真实信号，销售话术不与客户意图混同）。
- [x] **价格/意向时间衰减**: 客户报价按「7 天新鲜度窗口」衰减 —— 超过 7 天视为过期意向价格，降权参与意向评分并在画像/原因中显式标注「已过期」。
