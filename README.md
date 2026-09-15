# 政策抓取入库运维工具

独立部署在 `E:\python_project\crawlerToBase`，不修改 KMS 或现有 Policy 代码。工具抓取申报类和非申报类政策：随申办非免申项目、随申办免申项目、企服云申报中项目及上海市统一政策发布平台；标准化后写入 `dsfa_policy` 独立表，再调用 `POST /kms/api/etl/dg/crawlerToBase`。

## 关键行为

- 随申办：只保留“进行中/即将开始”且明确 `freeEnjoy=false`（免申为“否”）的申报项目；字段缺失时不推断，直接排除。
- 随申办非申报类：同样只保留“进行中/即将开始”，但固定 `freeEnjoy=true`（免申为“是”），并保存到非申报类知识库 `c24a793eec08458f873d263a090361d0`。
- 企服云：请求条件固定为上海市、申报中；详情 `dataList` 或正文为空时记为 `source_empty`。
- 唯一键 `(source_code, source_item_id, base_id)`；列表发现后批量去重，成功记录不请求详情。
- 每个任务批次开始时，会按目标知识库一次性预加载 KMS 有效政策标题到内存；详情解析后同标题命中即跳过，不写入本地台账或再次入库。任务结束后该标题集合自动释放；KMS 查询异常时保守放行并继续本地去重。
- KMS 失败记录使用数据库中经过 Pydantic 校验的 `kms_payload_json` 重推，不重新抓站点。
- KMS `1` 和 `7` 都视为成功；网络错误和 5xx 指数退避重试三次，其他业务码不自动重试。
- 附件作为正文绝对链接，不发送 `attaches`（KMS 的该字段是文件服务 ID）。
- 定时抓取默认每天 Asia/Shanghai 01:00 执行全部任务；可在任务中心动态启停、修改时间和选择任务，定时任务仅抓取到本地台账。
- 上海市统一政策发布平台：按市级、市级部门、各区入口的全部分页抓取。标题、发文单位、发布日期和文号取列表项；详情仅取 `txt` 正文。清洗正文后调用 Policy 分类服务，只有“惠企 + 申报通知类/申报类/非申报通知类/非申报类”进入相应待入库知识库；其中“申报类”与“申报通知类”均进入申报知识库，“非申报类”与“非申报通知类”均进入非申报知识库。
- 平台政策常规抓取会在详情请求前，按稳定来源 ID、本地两类政策标题、KMS 两套知识库标题及本批次标题依次提前去重；命中后不再抓详情或调用智能体。标题按去除首尾空白后的完整值精确匹配，缓存仅在本批次内有效；主动“重新抓取并比对”不使用该提前跳过逻辑。智能体超时、异常或返回格式不正确会进入独立“抓取失败”台账，可在数据处理页选择并重新抓取，不能直接入库。

## 安装

要求 Python 3.9+、MySQL 可访问、KMS 网关可访问。

1. 双击 `start-with-venv.bat` 创建项目内 `.venv` 并安装依赖。
2. 将 `.env.example` 复制为 `.env`，填写数据库账号、密码和 KMS 地址。不要把 `.env` 提交到版本库。
3. 在 `dsfa_policy` 依次执行 `sql/001_init.sql` 至 `sql/004_run_observability.sql`。部署用 SQL 也同步保存在 `F:\coding\dsfa-product-plus\policy\be\dsfa-policy-starter\deploy\sql`。
4. 双击 `start.bat`，浏览器访问 `http://127.0.0.1:8000`。

也可以手工启动：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe main.py
```

默认只监听 `127.0.0.1`。若将 `CRAWLER_BIND_HOST` 改为非本机地址，必须同时设置 `CRAWLER_ADMIN_USER` 和 `CRAWLER_ADMIN_PASSWORD`，否则应用拒绝启动。生产上建议再置于 HTTPS 反向代理之后。

## Linux 部署

在目标目录完成代码和 `.env` 配置后，执行一次初始化：

```bash
cd /data/crawlerToBase
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x start.sh stop.sh
```

数据库首次部署时依次执行 `sql/001_init.sql` 至 `sql/004_run_observability.sql`，再运行 `./start.sh`。脚本会在重启前停止旧进程，后台输出追加到项目根目录的 `nohup.log`；使用 `./stop.sh` 可停止服务。

## 运维 API

- `GET /api/tasks`：任务和最近批次。
- `GET /api/schedule`、`PUT /api/schedule`：读取或更新定时抓取开关、每日时间和任务列表；配置保存后立即生效。
- `POST /api/runs`：启动；预检示例 `{"task_codes":[],"dry_run":true}`，正式执行还须 `confirm_write=true`。
- `GET /api/runs/{run_id}`、`GET /api/runs/{run_id}/items`：进度和明细。
- `GET /api/runs/{run_id}/events`：SSE，支持 `Last-Event-ID` 续传；刷新页面会回放最近 300 条持久化日志。
- `POST /api/runs/{run_id}/retry-failed`：仅重推失败 payload。
- `POST /api/runs/{run_id}/stop`：当前记录完成后停止。
- `POST /api/articles/re-crawl`：仅重新抓取智能体分类失败台账，成功后转为待入库或标记为跳过。
- `GET /api/health`：只读检查数据库、配置和两站连通性。

## KMS 契约

最终请求固定由 `CrawlerPayload` 生成，包含 `id/bt/url/pubDate/wh/content/source/baseId/metadata`。`id` 为来源编码、来源 ID、知识库 ID 生成的 UUIDv5 32 位 hex，重试不变；`content` 必须有可见文本。标准化 HTML 删除脚本、样式、iframe、音视频等危险内容，保留标题、段落、列表、表格、图片、链接，且相对地址转绝对地址。

两站脱敏示例位于 `examples/`。

## 日志与排障

文件日志写入 `logs/crawler.log`，单文件 20 MB、保留 10 份。定位时使用 `run_id`、`task_code`、`source_item_id`、`kms_document_id`、`base_id` 和 `kms_result_code`；不会写正文、Cookie、Token 或数据库密码。页面显示脱敏错误摘要，文件保留异常堆栈。批次每次状态或进度更新都会刷新心跳；若服务中没有活动线程且超过 `CRAWLER_RUN_STALE_MINUTES` 未更新，系统每 5 分钟自动将其标记为中断。

分类服务通过 `CRAWLER_POLICY_BASE_URL` 配置，默认 `https://aies.dreamdt.cn`；内网可设为 `http://10.1.3.144:20002`，路径为 `CRAWLER_POLICY_CLASSIFY_PATH=/policy/api/policy-classification/classify`。来源接口结构变化会明确失败，不会把未知结构静默写入 KMS。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

上线前先在测试环境执行预检，再把 `CRAWLER_MAX_ITEMS_PER_TASK=1`，各站正式冒烟一条；确认 KMS 展示正确后恢复为 `0`。重复运行应只出现“已存在且 KMS 已成功”。

## Windows 计划任务（可选）

应用自身已包含每日调度。若需要随 Windows 启动，可在任务计划程序中创建“计算机启动时”任务，程序填 `E:\python_project\crawlerToBase\start.bat`，起始位置填项目目录。不要再额外创建每日抓取任务，以免重复触发；唯一索引虽然能兜底，但会产生无意义的冲突日志。
