# 测试报告

自动化测试覆盖：

- KMS payload 必填字段、别名、日期、绝对 URL、确定性 32 位 ID。
- HTML 中文、图片、表格、附件保留，以及危险标签清除和相对链接绝对化。
- 随申办申报类筛选（进行中/即将开始且免申为否）及 Base64 正文解码。
- 企服云列表/详情映射和空原文跳过。
- KMS 代码 `1`、`7`、业务失败不重试、5xx 三次重试。
- 已抓取但 KMS 失败时只重推数据库 payload，不重新访问详情。
- SSE 按 `Last-Event-ID` 重放。

## 本地验证结果（2026-08-06）

- Python 3.13 兼容性环境（代码目标为 Python 3.11+）：`12 passed`。
- `pip check`：`No broken requirements found`。
- Uvicorn 启动成功；`GET /` 返回 200，`GET /api/tasks` 返回两个任务。
- 真实公开接口只读校验：两站均可发现符合条件的申报类项目，并各抽取一条详情完成 HTML 清洗与 `CrawlerPayload` 校验。具体数量随来源站点实时数据变化。
- 企服云样本正确映射 `originalURL/publishTime/startDate/endDate`；随申办样本通过项目详情的 `sourcePolicy.id` 解析到政策原文。

真实数据库和 KMS 写入冒烟不会由自动化测试擅自执行。部署人员按 README 将 `CRAWLER_MAX_ITEMS_PER_TASK=1` 后完成两站各一条测试环境验收，并把批次 ID、KMS 展示截图和第二次运行的跳过明细附到变更单。
