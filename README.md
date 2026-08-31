# 政策抓取入库运维工具

独立部署在 `E:\python_project\crawlerToBase`，不修改 KMS 或现有 Policy 代码。工具只抓取申报类政策，包括随申办非免申项目和企服云申报中项目；标准化后写入 `dsfa_policy` 独立表，再调用 `POST /kms/api/etl/dg/crawlerToBase`。

## 关键行为

- 随申办：只保留“进行中/即将开始”且明确 `freeEnjoy=false`（免申为“否”）的申报项目；字段缺失时不推断，直接排除。
- 企服云：请求条件固定为上海市、申报中；详情 `dataList` 或正文为空时记为 `source_empty`。
- 唯一键 `(source_code, source_item_id, base_id)`；列表发现后批量去重，成功记录不请求详情。
- KMS 失败记录使用数据库中经过 Pydantic 校验的 `kms_payload_json` 重推，不重新抓站点。
- KMS `1` 和 `7` 都视为成功；网络错误和 5xx 指数退避重试三次，其他业务码不自动重试。
- 附件作为正文绝对链接，不发送 `attaches`（KMS 的该字段是文件服务 ID）。
- 每天 Asia/Shanghai 01:00 将两个任务放在同一串行批次执行。

## 安装

要求 Python 3.11、MySQL 可访问、KMS 网关可访问。

1. 双击 `start-with-venv.bat` 创建项目内 `.venv` 并安装依赖。
2. 将 `.env.example` 复制为 `.env`，填写数据库账号、密码和 KMS 地址。不要把 `.env` 提交到版本库。
3. 在 `dsfa_policy` 执行 `sql/001_init.sql`。
4. 双击 `start.bat`，浏览器访问 `http://127.0.0.1:8000`。

也可以手工启动：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe main.py
```

默认只监听 `127.0.0.1`。若将 `CRAWLER_BIND_HOST` 改为非本机地址，必须同时设置 `CRAWLER_ADMIN_USER` 和 `CRAWLER_ADMIN_PASSWORD`，否则应用拒绝启动。生产上建议再置于 HTTPS 反向代理之后。

## 运维 API

- `GET /api/tasks`：任务和最近批次。
- `POST /api/runs`：启动；预检示例 `{"task_codes":[],"dry_run":true}`，正式执行还须 `confirm_write=true`。
- `GET /api/runs/{run_id}`、`GET /api/runs/{run_id}/items`：进度和明细。
- `GET /api/runs/{run_id}/events`：SSE，支持 `Last-Event-ID` 续传。
- `POST /api/runs/{run_id}/retry-failed`：仅重推失败 payload。
- `POST /api/runs/{run_id}/stop`：当前记录完成后停止。
- `GET /api/health`：只读检查数据库、配置和两站连通性。

## KMS 契约

最终请求固定由 `CrawlerPayload` 生成，包含 `id/bt/url/pubDate/wh/content/source/baseId/metadata`。`id` 为来源编码、来源 ID、知识库 ID 生成的 UUIDv5 32 位 hex，重试不变；`content` 必须有可见文本。标准化 HTML 删除脚本、样式、iframe、音视频等危险内容，保留标题、段落、列表、表格、图片、链接，且相对地址转绝对地址。

两站脱敏示例位于 `examples/`。

## 日志与排障

文件日志写入 `logs/crawler.log`，单文件 20 MB、保留 10 份。定位时使用 `run_id`、`task_code`、`source_item_id`、`kms_document_id`、`base_id` 和 `kms_result_code`；不会写正文、Cookie、Token 或数据库密码。页面显示脱敏错误摘要，文件保留异常堆栈。

常见检查顺序：`/api/health` → 批次明细 → `logs/crawler.log` → 数据库文章的 `kms_result_code/last_error`。来源接口结构变化会明确失败，不会把未知结构静默写入 KMS。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

上线前先在测试环境执行预检，再把 `CRAWLER_MAX_ITEMS_PER_TASK=1`，各站正式冒烟一条；确认 KMS 展示正确后恢复为 `0`。重复运行应只出现“已存在且 KMS 已成功”。

## Windows 计划任务（可选）

应用自身已包含每日调度。若需要随 Windows 启动，可在任务计划程序中创建“计算机启动时”任务，程序填 `E:\python_project\crawlerToBase\start.bat`，起始位置填项目目录。不要再额外创建每日抓取任务，以免重复触发；唯一索引虽然能兜底，但会产生无意义的冲突日志。
