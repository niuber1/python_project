# 政策抓取入库运维工具（crawlerToBase）交接文档

> 适用范围：`E:\python_project\crawlerToBase`。本工具**仅本机运行**（Windows + git-bash），是 DSF 政策知识库的抓取入库前端。
> 职责：从「随申办 / 企服云」抓取申报类政策 → 存入本地库 `dsfa_policy`（记账）→ 按需调用 DSF 平台 KMS 接口入库并拆解。
> 后端 FastAPI + uvicorn，前端为无构建步骤的静态 HTML/JS/CSS。

---

## 1. 项目概述

| 项 | 值 |
| --- | --- |
| 服务 | FastAPI，`main.py` → uvicorn → `crawler_tool.app:app`，默认 `127.0.0.1:8000` |
| 入口脚本 | `start.bat`（**GBK/ANSI + CRLF 编码，勿用 UTF-8 保存**，否则 cmd 解析乱码） |
| 抓取源 | 随申办 `https://zwdt.sh.gov.cn/qykj/shspace/`、企服云 `https://shpolicy.ssme.sh.gov.cn/governmentCloudApi/` |
| 目标 | DSF 平台 KMS `POST {kms_base_url}/kms/api/etl/dg/crawlerToBase`（默认 `http://10.1.3.144:20002/...`） |
| 目标知识库 | `TARGET_BASE_ID = 395cbf7152564184ad7c701beaf80cc5`（config.py；注意 KMS 侧可能经智能体选库重映射） |
| 定时任务 | APScheduler 每天 `01:00` 自动**仅抓取**（不自动入库），`CRAWLER_SCHEDULE_HOUR/MINUTE` 可调 |

## 2. 运行环境与依赖

- Python 3.13，虚拟环境 `.venv`（`start.bat` 强制要求存在）
- 依赖（requirements.txt）：fastapi、uvicorn[standard]、httpx、pymysql、APScheduler、beautifulsoup4、lxml、bleach、pydantic-settings、pytest、respx
- 运行前需 `.env`（复制 `.env.example`，配好 `CRAWLER_DB_USER/PASSWORD` 等）
- **网络**：远程 MySQL/KMS 在 `10.1.3.144`，需 OpenVPN 连通（本机 OpenVPN TAP `10.100.0.x`）；**直连源站时不要走代理**（v2rayN 未启动时 shell 里残留的 `HTTPS_PROXY=127.0.0.1:10808` 会导致连接被拒；引擎 httpx 客户端会继承环境代理，直连测试请 `unset HTTPS_PROXY HTTP_PROXY ALL_PROXY`）
- 浏览器自动化（页面排障/截图）用 Hermes venv 的 Playwright + 系统 Chrome，非本项目 venv

## 3. 架构总览

```
浏览器(static/) ──HTTP──▶ FastAPI(crawler_tool/app.py)
                            │
        ┌───────────────────┼────────────────────────────┐
        ▼                   ▼                            ▼
  /api/runs(任务)    /api/articles(已抓取数据)     /api/health
        │                   │
  RunManager(engine.py)     │
    │ 线程执行             Database(database.py, pymysql 直连, 每次操作新建连接)
    ▼                     ▼
  Adapters(随申办/企服云)  dsfa_policy(policy_crawler_article/run/run_item)
    │ 抓取+纯正文清洗       ▲
    ▼                     │
  本地库记账(kms_status=pending) ──▶ 保存到知识库/自动同步
                                      │
                               KmsClient(kms_client.py) ──POST──▶ DSF KMS crawlerToBase
                                      │
                               kms_kb.py 直连 kms_kms 标题查重(命中→标记已同步,不调KMS)
```

- 运行批次由 `RunManager._active_run` 单例互斥（同一时间只跑一个批次）
- 进度通过 SSE `/api/runs/{id}/events` 推送（**事件在内存 EventStore，服务重启后旧批次无事件**，前端「查看」已改为读库快照）

## 4. 后端模块速查（crawler_tool/）

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI 路由、APScheduler 调度、counts 3s TTL 缓存 |
| `engine.py` | `RunManager`：start/stop/retry_failed/push_articles；`_execute`(crawl/all)、`_execute_push_ids`(勾选入库)、`_execute_push`(按任务来源推，前端已不用)、`_execute_retry`、`_start_auto_push`(自动同步链式) |
| `database.py` | 全部 MySQL 访问（pymysql，每次操作新建连接）；见 §6 |
| `adapters/base.py` | `CrawlerAdapter` 抽象（discover/fetch/health_url） |
| `adapters/suishenban.py` | 随申办：列表 POST `policy_center/hqPolicy/projects`（applyState=1,2+freeEnjoy=false 服务端筛选）；详情 `questions`→`policyDetail` |
| `adapters/qifuyun.py` | 企服云：列表 POST `chatSNet/policy`（area=上海市、applicationStatus=申报中）；详情 `chatSNet/policyInfo` |
| `kms_client.py` | KMS 推送：POST crawlerToBase，业务码 `1/5/7`=成功（1 入库成功、5 知识库处理中、7 文档已存在），HTTP 5xx/网络异常重试 3 次（1s/2s 退避） |
| `kms_kb.py` | 直连 `kms_kms.kms_hub_document` 按 `g_objectname + kms_hub_base_id` 标题查重；异常放行 |
| `html_utils.py` | `normalize_article_html`（输出**纯正文** HTML：剔除「政策信息」块/「政策正文」外壳、script/video 等、链接绝对化、保留附件段）；`decode_possible_base64_html`、`parse_date`、`content_sha256`、`is_shanghai_district` |
| `config.py` | `Settings`（env 前缀 `CRAWLER_`，读 .env）；`TASKS`（suishenban_declare / qifuyun_declare，均指向 TARGET_BASE_ID） |
| `models.py` | PolicyCandidate/PolicyArticle/CrawlerPayload(别名 bt/wh/pubDate/baseId)/KmsResult/StartRunRequest(phase、auto_sync)/PushArticlesRequest |
| `events.py` | 内存 EventStore（SSE 用） |
| `logging_config.py` | 日志（logs/crawler.log） |

## 5. 前端结构（static/）

双 Tab 单页（无构建）：

| Tab | 内容 |
| --- | --- |
| **任务** | 任务勾选（复选框状态跨刷新保留）、`预检`、`自动同步知识库`（默认不勾）、`启动抓取`（固定 phase=crawl：抓取→本地库，不调 KMS）、停止、当前批次 SSE 日志、最近批次表（**上限 15 条**，`查看`=viewRun 读库快照、`重试失败`=重推 KMS failed） |
| **已抓取数据** | 状态/来源/关键字筛选、分页列表（标题/来源/抓取时间/申报起止/同步状态徽标/同步日期）、勾选 + `保存到知识库 (N)`（POST /api/articles/push → SSE 进度日志）、`预检(不推送)`、停止 |

- `app.js` 关键：`watch(id, append)`（SSE；complete 事件带 `next_run_id` 时自动切到自动同步的入库批次）、`viewRun`（已结束批次读 `/api/runs/{id}` + `/items` 渲染快照）、`loadArticles`、`selectedArticles` Set 勾选态
- 同步状态徽标：pending=待入库🟡 / success=已同步🟢 / failed=同步失败🔴（title 悬浮显示 last_error）

## 6. 数据模型（dsfa_policy，SQL 见 sql/001_init.sql）

| 表 | 要点 |
| --- | --- |
| `policy_crawler_article` | 抓取文章记账。唯一键 `(source_code, source_item_id, base_id)` 与 `(kms_document_id, base_id)`；`crawl_status`(success)、`kms_status`(pending/success/failed)、`kms_result_code`、`kms_payload_json`(入库快照)、`pushed_at`、索引 `(kms_status, updated_at)` |
| `policy_crawler_run` | 批次。`trigger_type`：manual/manual-crawl/manual-push/schedule/retry；`task_codes_json`、`dry_run`、计数、status(queued/running/completed/stopped/failed) |
| `policy_crawler_run_item` | 批次明细。`phase`(discover/fetch/store/deduplicate/kms/validate/failed)、`status`、`policy_crawler_article_id`、`kms_result_code`、`message`、`duration_ms` |

KMS 侧只读查重库：`kms_kms.kms_hub_document`（`g_objectname`=标题、`kms_hub_base_id`=知识库ID、`cm_status_text`=处理状态）。

## 7. 核心业务链路与规则

1. **抓取（phase=crawl）**：discover（服务端筛选：随申办 applyState=1,2+freeEnjoy=false / 企服云 申报中+上海市）→ 批次内按项目名去重 → fetch 详情 → `normalize_article_html` 纯正文 → **标题去重**（本批次 seen_titles 或本地库 `find_existing_by_title` 命中→跳过）→ insert（kms_status=pending）
2. **入库（/api/articles/push 勾选 或 auto_sync 链式）**：按 article_id 精确取数 → 逐条：**kms_kb 标题查重**（同库同标题命中→code=7 直接标记已同步，不调 KMS）→ 否则 KmsClient.push（用 kms_payload_json 快照）→ update_article_kms 回写 → SSE 进度
3. **auto_sync**：爬取批次完成且勾选时，`_start_auto_push` 收集本批次 phase=store&success 的文章 → 释放爬取锁 → 链式启动 manual-push 批次；complete 事件带 `next_run_id` 前端无缝切换
4. **元数据口径**：政策层级/发文单位只取**展示名**（随申办 levelName / pubDeptName），编码（ZCJB0001005/SHHQGW）与**纯区名**（闵行区等，`is_shanghai_district`）一律置空；KMS payload metadata 携带 项目名称/政策层级/发文部门/申报起止/文档来源

## 8. 接口清单

| 接口 | 说明 |
| --- | --- |
| `GET /` | 前端页面（static/index.html） |
| `GET /api/health` | DB 连接+3 张表 + 两源站可达性 + 配置 |
| `GET /api/tasks` | 任务列表 + 最近 15 批次 + `pending_count` |
| `POST /api/runs` | 启动批次 `{task_codes, dry_run, confirm_write, phase: all\|crawl\|push, auto_sync}`；正式执行必须 confirm_write=true |
| `GET /api/runs/{id}` `/items` `/events` `/stop` | 批次状态 / 明细(LEFT JOIN 带 article_title) / SSE / 停止 |
| `POST /api/runs/{id}/retry-failed` | 重推该批次 KMS failed 的 payload |
| `GET /api/articles` | 分页 `?page&size&status&source_code&keyword` |
| `GET /api/articles/counts` | pending/success/failed 计数（单 GROUP BY + **3s TTL 缓存**） |
| `POST /api/articles/push` | 勾选入库 `{article_ids, dry_run, confirm_write}`；未知 id→400 |

## 9. 配置项

- `.env`（env 前缀 `CRAWLER_`，环境变量优先于 .env）：`DB_HOST/PORT/USER/PASSWORD/NAME`、`KMS_BASE_URL/KMS_PATH/KMS_AUTHORIZATION/KMS_COOKIE`、`BIND_HOST/BIND_PORT`、`SCHEDULE_HOUR/MINUTE`、`MAX_ITEMS_PER_TASK`、`ITEM_DELAY_SECONDS`、`REQUEST/KMS_TIMEOUT_SECONDS`、`LOG_LEVEL`
- 测试/临时覆盖常用：`CRAWLER_BIND_PORT=8001`（避开 8000 主服务）、`CRAWLER_MAX_ITEMS_PER_TASK=2`（限抓取条数）、`CRAWLER_KMS_BASE_URL=http://127.0.0.1:9`（假 KMS 防真实推送）
- `kms_db_name` 默认 `kms_kms`（复用 DB 账号连接知识库查重）

## 10. 重要注意事项（坑）

1. **start.bat 必须 GBK/ANSI + CRLF**：UTF-8（即使 chcp 65001）或 LF-only 会导致 cmd 解析错乱（`if not exist` 被吞、`'exist' 不是命令`）。改它只能用 GBK 编码保存。
2. **SSE 事件在内存**：服务重启后旧批次无实时事件，「查看」已改读库快照（`/api/runs/{id}`+`/items`）；运行中批次才连 SSE。
3. **远程库每次操作新建连接**（VPN 下约 0.6s/连接）：counts 已合并单查询+缓存；其他接口冷查询约 0.8s 属正常。
4. **KMS 消费端依赖 `kms.mq.enabled=true`**（KMS 服务 Nacos `pem/kms-prod.yaml`，或环境变量 `KMS_CONSUMER_ENABLED=true`）：未开启时文档入库后卡「待处理」（无拆解）。这是 KMS 平台侧配置，不在本项目。
5. **KMS 可能经智能体重选知识库**（`getBaseIdByAI`）：payload 传的 baseId 不一定是最终落库 base；标题查重按 `kms_hub_base_id` 匹配。
6. **接口偶发返回裸 `false`**（随申办列表分页边界）：adapter 已防御（非 dict 抛 AdapterError 或重试）。
7. **端口冲突**：8000 被占用时 start.bat 直接退出（bind 错误）；多实例测试用 8001+。
8. **80xx 服务重启后生效**：改代码后需重启服务（前台 start.bat 或 kill 8000 后重起）。
9. 企服云/随申办源站为政府站点，无登录态裸 GET 可能 403/限流；抓取间隔由 `ITEM_DELAY_SECONDS` 控制。

## 11. 测试与验证

- `pytest`：29 个用例全绿（tests/：test_adapters、test_engine_events、test_kms_client、test_models_html）。engine 测试用 `FakeDb` + monkeypatch `KmsClient`/`kms_kb`，**不要发真实请求**
- 前端语法：`node --check static/app.js`
- 实跑验证套路：`CRAWLER_BIND_PORT=8001 CRAWLER_MAX_ITEMS_PER_TASK=2 cmd //c start.bat` → curl 接口 → 验证后清理 `policy_crawler_run_item/run/article` 三表
- 浏览器验证：Hermes venv Playwright + 系统 Chrome 打开 `http://127.0.0.1:8001/`

## 12. 受控环境凭证（仅本机使用，禁止外发/提交/截图/聊天）

> 本项目仅本地运行，用户授权明文维护。涉及共享平台，仍按最低暴露原则使用。

| 项 | 值 |
| --- | --- |
| 爬虫库 MySQL | `10.1.3.144:3306/dsfa_policy`，root / Dreamsoft2025 |
| KMS 查重库 MySQL | `10.1.3.144:3306/kms_kms`（同账号） |
| KMS 入库接口 | `http://10.1.3.144:20002/kms/api/etl/dg/crawlerToBase`（网关 20002 转发到 KMS 服务 20008） |
| 目标知识库 baseId | `395cbf7152564184ad7c701beaf80cc5`（config.py TARGET_BASE_ID） |
| 随申办接口 | `https://zwdt.sh.gov.cn/qykj/shspace/`（公开，无鉴权） |
| 企服云接口 | `https://shpolicy.ssme.sh.gov.cn/governmentCloudApi/`（公开，无鉴权） |
| DSF 平台 Nacos（排障参考） | `10.1.3.144:9050`，nacos / Dreamsoft2025；KMS 配置 `pem/kms-prod.yaml`（group prod） |
| KMS 服务 | `10.1.3.144`，HTTP 20008（目录 `/data/kms/application/kms/service/202608051840/20008`），DB/Redis/ES 密码均 Dreamsoft2025 体系 |

> 若 `.env` 或线上配置变更，及时同步本节；本节内容禁止出现在提交记录、工单、外部聊天中。
