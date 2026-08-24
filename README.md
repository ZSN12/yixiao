# 销售线索智能分析与分发助手 (Sales Lead AI Analyzer & Dispatcher)

> 一句话定位: 每日定时把"客户会话画像 → 意向分层 → RAG 线索匹配 → 钉钉触达"串成一条可闭环、可降级、开箱即跑的 B 端销售线索智能流水线。

## 架构图 (ASCII)

```
                        ┌──────────────────────────────────────────────┐
                        │                 main.py (门面)                 │
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
              │ profile_analyzer│ │ lead_assigner   │  │ dingtalk_notifier│
              │ (LLM/规则双引擎) │  │ (规则+RAG+负载) │  │ (日报+分配明细)   │
              └────────────┬───┘  └───────┬─────────┘  └┬────────────────┘
                           │              │              │
              ┌────────────▼──────────────▼──────────────▼──────────────┐
              │  数据层 data_loader: Customer / ChatRecord / Sales /     │
              │  SalesExperience + SQLite 分析历史                        │
              └─────────────────────────────────────────────────────────┘
```

## 模块清单

| 模块 | 职责 | 关键接口 |
| --- | --- | --- |
| `config/settings.py` | 全项目配置中心(LLM/钉钉/DB/调度/mock) | `settings` 单例 |
| `modules/data_loader.py` | 数据契约 + JSON 加载 + SQLite 历史 | `load_all / build_chat_map / init_db` |
| `modules/profile_analyzer.py` | 客户画像/意向分层/流失风险(LLM 或规则引擎) | `analyze_customers_batch` |
| `modules/rag_retriever.py` | 销售经验 RAG 检索(embedding 或本地 n-gram) | `match_customer_to_sales` |
| `modules/lead_assigner.py` | 规则硬约束 + RAG 软排序 + 负载均衡分配 | `assign_leads` |
| `modules/dingtalk_notifier.py` | 钉钉自定义机器人日报/明细推送(标准库加签) | `send_daily_report` |
| `orchestrator/language_graph_flow.py` | LangGraph 多智能体状态图编排 + 顺序降级 | `run_pipeline_graph` |
| `main.py` | 门面: 一次性执行 / APScheduler 定时闭环 | `python main.py` |

## 快速开始

> 运行环境要求: **Python 3.13**(langgraph 1.2.x 在 3.10/3.11/3.12 上会因
> `TypedDict(extra_items=...)` 不兼容而导入失败)。推荐使用 uv:

```bash
# 创建 Python 3.13 虚拟环境并安装依赖
uv venv .venv --python 3.13
uv pip install -p .venv/bin/python -r requirements.txt -r requirements-dev.txt
source .venv/bin/activate

# 一次性跑通完整流水线(mock 模式, 无需任何 API Key)
python main.py --run-once

# 跳过推送环节(数据仍落库)
python main.py --run-once --skip-push

# 每日定时模式(按 config/settings.py 的 daily_run_time, 默认 08:30)
python main.py

# LangGraph 多智能体层独立运行/自检
python -m orchestrator.language_graph_flow
```

mock 模式下(默认 `MOCK_MODE=True`): 画像分析走规则引擎、钉钉推送未配置时打印提示并跳过 —— 不花一分钱、开箱即跑。

## 目录结构

```
sales-agent/
├── main.py                          # 门面: --run-once / APScheduler 定时
├── requirements.txt
├── config/
│   ├── settings.py                  # pydantic-settings 配置中心
│   └── .env.example                 # 环境变量示例(LLM/钉钉/调度)
├── data/                            # mock 数据(客户/会话/销售/经验)
├── modules/                         # 业务模块(数据/分析/RAG/分配/推送)
│   ├── data_loader.py
│   ├── profile_analyzer.py
│   ├── rag_retriever.py
│   ├── lead_assigner.py
│   └── dingtalk_notifier.py
├── orchestrator/
│   └── language_graph_flow.py       # LangGraph 多智能体状态图
└── tests/                           # 各模块自检脚本
```

## 面试亮点

- **LangGraph 多智能体状态图**: 分析师 → 匹配师 → 推送员三个专职节点共享状态对象, 节点间只通过 state 流转, 可直接可视化、可单独 import 测试。
- **零 langchain 依赖的模型调用**: 节点内直接用 openai SDK(settings.LLM_API_BASE/KEY/MODEL), 规避 openai 3.x 与 requirements 锁定 openai==1.51.0 的版本冲突。
- **三级降级**: langgraph 不可用 → 顺序直调; LLM 失败 → 规则引擎; 单环节失败 → 记错误继续, 全流程不崩溃。
- **钉钉加签正确实现**: HMAC-SHA256 + Base64 + URL 编码, 纯标准库, 见到"timestamp\nsecret"顺序的考点即得分。
- **确定性可复现**: mock 模式全规则引擎, 意向分层(高/中/低)与流失分层完全确定, 便于验收与回归。

## 配置说明

### settings 字段一览

项目所有配置集中在 `config/settings.py`(pydantic-settings 单例 `settings`), 每个字段均可被 `config/.env` 或同名环境变量覆盖:

| 环境变量 | settings 字段 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_BASE` | `llm_api_base` | `https://api.deepseek.com/v1` | 大模型 OpenAI 兼容 Base URL(DeepSeek / 通义等) |
| `LLM_API_KEY` | `llm_api_key` | `""` | 大模型 API Key; 为空 + `MOCK_MODE=True` 时画像分析走规则引擎 |
| `LLM_MODEL` | `llm_model` | `deepseek-chat` | 模型名(如 `deepseek-chat` / `qwen-plus`) |
| `LLM_TIMEOUT` | `llm_timeout` | `60.0` | 大模型调用超时(秒) |
| `DINGTALK_WEBHOOK_URL` | `dingtalk_webhook_url` | `""` | 钉钉自定义机器人 Webhook; 未配置则打印提示并跳过推送 |
| `DINGTALK_SECRET` | `dingtalk_secret` | `""` | 机器人加签密钥(SEC 开头, 可选) |
| `DB_PATH` | `db_path` | `data/sales_agent.db` | SQLite 数据库路径(相对项目根) |
| `DAILY_RUN_TIME` | `daily_run_time` | `08:30` | 每日定时运行时间(HH:MM) |
| `MOCK_MODE` | `mock_mode` | `True` | `True` 时画像分析走规则引擎; `LLM_API_KEY` 为空时自动视为 mock |
| `EMBEDDING_API_BASE` | `embedding_api_base` | `""` | Embedding API 端点(可选; 配置了走向量检索) |
| `EMBEDDING_API_KEY` | `embedding_api_key` | `""` | Embedding API Key |
| `EMBEDDING_MODEL` | `embedding_model` | `""` | Embedding 模型名(如 `text-embedding-3-small`) |

### .env 用法

```bash
# 复制示例文件为正式配置(默认即可实现 mock 零成本跑通)
cp config/.env.example config/.env
# 编辑 config/.env 填入真实值(LLM / 钉钉 / embedding 三组按需填写)
```

`config/settings.py` 在 import 时自动加载 `config/.env`(utf-8 编码); 同名环境变量优先级更高, 可直接覆盖, 无需改代码。

### Mock 模式与真实模式的切换

- **Mock 模式(默认, 开箱即跑)**: `MOCK_MODE=True`(或 `LLM_API_KEY` 为空)。画像分析走规则引擎(关键词打分), RAG 走零依赖本地字符 n-gram 相似度, 钉钉未配置时打印提示并跳过推送 —— 不花一分钱, 全部确定性可复现。
- **真实模式**: 在 `config/.env` 中设 `MOCK_MODE=False` 并填入 `LLM_API_KEY`, 画像分析与分析师/匹配师节点即调用大模型(OpenAI 兼容接口, 强制 JSON 输出); 填入 `EMBEDDING_*` 后 RAG 走 API 向量检索; 填入 `DINGTALK_*` 后日报与分配明细推送到钉钉群。
- **自动降级**: 任意 LLM / embedding 调用失败(网络 / 超时 / 格式异常)都会记日志并自动降级到规则引擎 / 本地相似度, 整条流水线不中断。

## API 一览

各模块对外公开函数(签名 + 一句话职责):

| 模块 | 函数 | 签名 | 职责 |
| --- | --- | --- | --- |
| `modules/data_loader` | `load_all` | `() -> (list[Customer], list[ChatRecord], list[Sales])` | 一次加载客户 / 会话 / 销售三份 mock 数据 |
| | `build_chat_map` | `(records: list[ChatRecord]) -> dict[str, list[ChatRecord]]` | 会话按 `customer_id` 分组, 供画像分析使用 |
| | `init_db` | `(db_path: str \| None = None) -> None` | 初始化 SQLite 并建 `analysis_history` 表(幂等) |
| `modules/profile_analyzer` | `analyze_customer` | `(customer: Customer, chat_records: list[ChatRecord]) -> AnalysisResult` | 单客户画像分析(LLM 优先, 失败自动降级规则引擎) |
| | `analyze_customers_batch` | `(customers: list[Customer], chat_map: dict) -> dict[str, AnalysisResult]` | 批量画像分析, 单条失败不中断 |
| `modules/rag_retriever` | `match_customer_to_sales` | `(customer_profile: str, sales_list, experiences, top_k=5) -> list[SalesMatch]` | 画像文本匹配 Top-K 销售(语义粗排 + 规则精排) |
| | `embed_texts` | `(texts: list[str]) -> list[list[float]]` | 文本向量化(embedding API 或本地 2-gram 兜底) |
| `modules/lead_assigner` | `assign_leads` | `(unassigned_customers, sales_list, experiences, analysis_results=None, top_k=5) -> list[AssignmentResult]` | 混合分配: 规则硬约束 + RAG 软排序 + 负载均衡 |
| | `build_customer_query_text` | `(customer: Customer, analysis_result=None) -> str` | 客户基础信息 + 画像文本拼成检索 query |
| `modules/dingtalk_notifier` | `send_daily_report` | `(summary_text: str, title="销售线索智能日报") -> bool` | 推送 markdown 日报到钉钉机器人 |
| | `send_assignment_batch` | `(assignments: list) -> bool` | 推送分配明细表格(客户、推荐销售、理由) |
| | `build_daily_report_text` | `(customer_count, profile_stats, assignment_summary, updated_at) -> str` | 构造日报 markdown 正文 |
| | `build_assignment_table_text` | `(assignments: list) -> str` | 构造分配明细 markdown 表格 |
| `orchestrator/language_graph_flow` | `run_pipeline_graph` | `(customers, chat_map, sales, experiences) -> dict` | 多智能体流水线主入口: 优先 LangGraph 状态图, 失败降级顺序直调 |
| | `run_pipeline_sequential` | `(customers, chat_map, sales, experiences) -> dict` | 降级路径: 顺序直调各模块, 输出与图模式同构 |

> **HTTP 服务**: 当前仓库没有 `api.py` / `app.py` FastAPI 应用文件(`requirements.txt` 已预置 `fastapi==0.115.0` / `uvicorn==0.30.6` 备用)。画像分析历史已通过 `init_db()` 落库 SQLite(`analysis_history` 表, 配套 `save_analysis_record` / `get_analysis_history` 查询接口), 后续可在此基础上接 FastAPI 暴露 `GET /history` 等只读端点。

## FAQ

**Q1: mock 模式不花钱, 是怎么做到的?**

答案引用本项目真实实现: 画像分析不调大模型 —— `MOCK_MODE=True`(或 `LLM_API_KEY` 为空)时, `profile_analyzer` 走规则引擎, 按聊天记录关键词命中计数打分(意向分 = 意向加分 − 意向减分 − 流失加分; 流失分 = 流失词命中数), 阈值映射为 高/中/低, 并附带画像文本 / 核心需求 / 跟进建议, 全部确定性可复现; RAG 匹配用零依赖字符 2-gram 本地相似度; 钉钉未配置 webhook 时打印提示并跳过推送(数据仍落库)。实测 `python main.py --run-once` 输出: 客户 **9** 家, 意向 **高6/中1/低2**, 流失 **高2/中2/低5**, 分配 **5** 条, 待人工 **0** 家 —— 与 mock 数据完全对应。

**Q2: 匹配度是怎么算的?**

`rag_retriever` 采用两级检索: ① 粗排 —— 配置了 `EMBEDDING_*` 走 API 余弦相似度, 否则用本地字符 2-gram 布尔向量的 Jaccard / 余弦相似度, 取 Top-K; ② 精排 —— 规则加权(行业命中销售擅长行业 +0.15、命中经验片段 industry +0.10、领域关键词命中 0.02/个封顶 0.10), 综合分 = similarity × (1 + rule_bonus) 封顶 1.0。`lead_assigner` 再叠加硬约束(行业 ∈ 擅长行业 → 候选1, 否则城市 ∈ 负责城市 → 候选2)与 RAG Top1 融合决策, 同分取 `current_load` 最小者做负载均衡。

**Q3: 钉钉加签是怎么实现的?**

`dingtalk_notifier._generate_sign` 严格按钉钉开放平台规范: 取毫秒时间戳与 secret 拼出字符串 `"timestamp\nsecret"`(注意 timestamp 在前), HMAC-SHA256(key=secret) 摘要 → Base64 编码 → `urllib.parse.quote_plus` URL 编码, 追加 `&timestamp=...&sign=...` 到 webhook。全程只用标准库 `hmac / hashlib / base64 / urllib`, 无第三方 HTTP 依赖。

**Q4: langgraph 不可用会怎样?**

`orchestrator` 对 langgraph 做宽松导入: 未安装 / 导入失败 / 图编译或调用异常时, `run_pipeline_graph` 自动降级 `run_pipeline_sequential` 顺序直调(分析师 → 匹配师 → 推送员 → 汇总), 输出状态与图模式完全同构, `main.py` 无需感知。`requirements.txt` 已锁 `langgraph==1.2.11`, `pip install -r requirements.txt` 后即恢复多智能体状态图模式。

**Q5: 怎么接真实 CRM?**

`data_loader` 预留 4 个对接接口: `fetch_crm_customers`(CRM 客户列表)、`fetch_wework_chat`(企微会话存档)、`fetch_crm_deals`(成交商机, 供经验沉淀)、`generate_sales_experiences`(LLM 提炼经验片段)。对接时把接口返回字段映射为 `Customer` / `ChatRecord` / `SalesExperience` 模型即可; 企微会话需配置可信 IP 与私钥解密。当前 mock 阶段这些接口返回空列表, 流水线仍基于 `data/` 下的 mock JSON 运行。

**真实数据适配**: `adapters/crm_data_adapter.py` 提供可运行实证 —— `load_customers_from_csv`(CRM 导出 CSV → `Customer`, 脏数据兜底: 缺城市→"未知"、空/未知行业→"其他"、缺时间→当天)、`load_chat_records_from_export`(企微会话存档 JSON → `ChatRecord`, 角色 员工→销售 / 外部联系人→客户)、`run_real_data_demo`(端到端: 真实来源数据经 `analyze_customers_batch` + `assign_leads` 同一调用路径消化, "调用方零改动")。演示数据在 `data/real/`(crm_customers.csv + wework_chat_export.json), 运行 `python adapters/crm_data_adapter.py` 可重新生成并实证。

## 测试

```bash
pip install pytest && pytest -v
```

pytest 单测全量跑(全部 mock 模式、临时 DB 隔离、不依赖真实 LLM/网络/钉钉); GitHub Actions 已在 push/PR 时自动执行(Python 3.10 / 3.11 矩阵)。`tests/` 下另有两个历史"自检脚本"(`test_*_selfcheck.py`, 独立 `python tests/xxx.py` 运行), 已由 `tests/conftest.py` 的 `collect_ignore` 排除出 pytest, 保留不删。