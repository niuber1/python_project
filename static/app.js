let currentRun = null,
  eventSource = null,
  selectedTasks = null,
  kmsAutoAuth = false;
let currentRunTarget = "tasks";
let articlePage = 1,
  articleSize = 20,
  articleSource = "",
  articleTotal = 0,
  dataView = "pending",
  selectedArticles = new Set();
let contentUpdateEnabled = false;
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
async function json(url, options) {
  const r = await fetch(url, options);
  let v;
  try {
    v = await r.json();
  } catch {
    v = { detail: await r.text() };
  }
  if (!r.ok)
    throw new Error(
      typeof v.detail === "string" ? v.detail : JSON.stringify(v.detail),
    );
  return v;
}
const runElements = {
  tasks: { runId: "runId", stop: "stop", logs: "logs", total: "total", succeeded: "succeeded", skipped: "skipped", failed: "failed", bar: "bar" },
  url: { runId: "urlRunId", stop: "urlStop", logs: "urlLogs", total: "urlTotal", succeeded: "urlSucceeded", skipped: "urlSkipped", failed: "urlFailed", bar: "urlBar" },
};
function runElement(target, key) {
  return $(runElements[target][key]);
}
function log(message, time = "", target = "tasks") {
  const box = runElement(target, "logs");
  box.textContent += `\n${time ? `[${time}] ` : ""}${message}`;
  box.scrollTop = box.scrollHeight;
}
function stats(v, target = "tasks") {
  for (const key of ["total", "succeeded", "skipped", "failed"])
    if (v[key] != null) runElement(target, key).textContent = v[key];
  const total = v.total || 0,
    done = v.processed || 0;
  runElement(target, "bar").style.width = `${total ? Math.min(100, (done / total) * 100) : 0}%`;
}
function watch(id, append = false, target = "tasks") {
  currentRun = id;
  currentRunTarget = target;
  runElement(target, "runId").textContent = id;
  runElement(target, "stop").disabled = false;
  if (!append) runElement(target, "logs").textContent = "连接实时日志…";
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/runs/${id}/events`);
  eventSource.onmessage = (e) => log(e.data, "", target);
  for (const type of [
    "status",
    "log",
    "warning",
    "error",
    "progress",
    "complete",
  ])
    eventSource.addEventListener(type, (e) => {
      const v = JSON.parse(e.data);
      log(v.message, v.time, target);
      stats(v, target);
      if (type === "complete" || type === "error") {
        eventSource.close();
        runElement(target, "stop").disabled = true;
        load();
        if ($("panelArticles").style.display !== "none") loadArticles();
      }
    });
}
function taskLabel(type) {
  return (
    {
      schedule: "定时抓取",
      manual: "手动任务",
      retry: "重试失败",
      "manual-crawl": "手动抓取",
      "manual-push": "入库",
      "manual-url": "URL 抓取",
    }[type] || type
  );
}
async function load() {
  try {
    const v = await json("/api/tasks");
    const checked =
      selectedTasks === null
        ? new Set(v.tasks.map((t) => t.code))
        : new Set(
            [...document.querySelectorAll(".task input:checked")].map(
              (x) => x.value,
            ),
          );
    selectedTasks = checked;
    $("tasks").innerHTML = v.tasks
      .map(
        (t) =>
          `<label class="task"><input type="checkbox" value="${esc(t.code)}"${checked.has(t.code) ? " checked" : ""}><b>${esc(t.name)}</b><small>${esc(t.rule)}</small></label>`,
      )
      .join("");
    $("pendingBadge").textContent = v.pending_count
      ? `${v.pending_count} 条待入库`
      : "";
    const runRow = (r, target = "tasks") =>
      `<tr><td><code>${esc(r.run_id.slice(0, 10))}…</code></td><td>${esc(taskLabel(r.trigger_type))}</td><td>${r.dry_run ? "预检" : "正式"}</td><td>${esc(r.status)}</td><td>${r.succeeded}/${r.skipped}/${r.failed}</td><td>${esc(r.started_at || "-")}</td><td><button class="secondary" onclick="viewRun('${esc(r.run_id)}','${target}')">查看</button>${r.failed && target === "tasks" ? ` <button class="secondary" onclick="retryRun('${esc(r.run_id)}')">重试</button>` : ""}</td></tr>`;
    $("runs").innerHTML = v.recent_runs
      .filter((r) => r.trigger_type !== "manual-url")
      .map(
        (r) => runRow(r),
      )
      .join("");
    $("urlRuns").innerHTML = v.recent_runs
      .filter((r) => r.trigger_type === "manual-url")
      .map((r) => runRow(r, "url"))
      .join("") || '<tr><td colspan="6" class="muted">暂无 URL 抓取记录</td></tr>';
  } catch (e) {
    log(`加载失败：${e.message}`);
  }
}
async function checkHealth() {
  try {
    const v = await json("/api/health");
    $("health").textContent = v.ok ? "服务正常" : "配置或依赖异常";
    $("health").classList.toggle("ok", v.ok);
  } catch {
    $("health").textContent = "检查失败";
  }
}
async function loadKmsAuthStatus() {
  try {
    const v = await json("/api/kms-auth");
    kmsAutoAuth = !!v.has_application_credentials;
    const names = [
      kmsAutoAuth ? "应用凭据（自动取令牌）" : "",
      v.has_access_token ? "临时 access_token" : "",
      v.has_authorization ? "Authorization" : "",
    ].filter(Boolean);
    $("kmsAuthStatus").textContent = names.length
      ? `已配置：${names.join("、")}`
      : "正文更新鉴权未配置";
  } catch {
    $("kmsAuthStatus").textContent = "配置状态读取失败";
  }
}
function syncContentUpdateVisibility() {
  document.querySelectorAll("[data-content-update]").forEach((element) => {
    element.hidden = !contentUpdateEnabled;
  });
}
async function loadFeatures() {
  try {
    const features = await json("/api/features");
    contentUpdateEnabled = !!features.content_update_enabled;
  } catch {
    contentUpdateEnabled = false;
  }
  if (!contentUpdateEnabled && ["update", "updateFailed"].includes(dataView)) {
    dataView = "pending";
  }
  syncContentUpdateVisibility();
  if (contentUpdateEnabled) loadKmsAuthStatus();
}
function kmsInputError(access_token) {
  if (!access_token) return "请先输入 144 OpenAPI 的 access_token";
  return "";
}
async function saveKmsAuth() {
  const access_token = $("kmsAccessToken").value.trim(),
    authorization = $("kmsAuthorization").value.trim(),
    error = kmsInputError(access_token);
  if (error) {
    $("kmsAuthMessage").textContent = error;
    return;
  }
  try {
    const v = await json("/api/kms-auth", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_token, authorization }),
    });
    $("kmsAccessToken").value = "";
    $("kmsAuthorization").value = "";
    $("kmsAuthMessage").textContent = v.message;
    loadKmsAuthStatus();
  } catch (e) {
    $("kmsAuthMessage").textContent = `保存失败：${e.message}`;
  }
}
async function testKmsAuth() {
  const access_token = $("kmsAccessToken").value.trim(),
    authorization = $("kmsAuthorization").value.trim();
  if (!access_token && !kmsAutoAuth) {
    $("kmsAuthMessage").textContent =
      "尚未配置应用凭据，请先配置或输入临时 access_token";
    return;
  }
  const button = $("testKmsAuth");
  button.disabled = true;
  button.textContent = "测试中…";
  $("kmsAuthMessage").textContent = access_token
    ? "正在验证临时令牌…"
    : "正在按接口文档 2.1 自动获取并验证应用令牌…";
  try {
    const v = await json("/api/kms-auth/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_token, authorization }),
    });
    $("kmsAuthMessage").textContent = v.ok
      ? "自动鉴权有效，可以执行正文覆盖更新。"
      : `鉴权无效：${v.message}`;
  } catch (e) {
    $("kmsAuthMessage").textContent = `测试失败：${e.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "测试正文更新鉴权";
  }
}
function viewQuery() {
  if (!contentUpdateEnabled) return dataView === "pending" ? { status: "pending" } : {};
  if (dataView === "pending") return { status: "pending" };
  if (dataView === "update") return { update_status: "pending" };
  if (dataView === "updateFailed") return { update_status: "failed" };
  return {};
}
function updateActionControls() {
  const n = selectedArticles.size,
    actionable = dataView !== "all",
    isUpdate = contentUpdateEnabled && (dataView === "update" || dataView === "updateFailed");
  $("selectionHint").textContent = actionable
    ? n
      ? `已选择 ${n} 条记录`
      : `请选择需要${isUpdate ? "更新正文" : "入库"}的记录`
    : "“全部记录”仅用于查询；请切换到待入库后执行操作。";
  $("previewSelected").disabled = !actionable || n === 0;
  $("executeSelected").disabled = !actionable || n === 0;
  $("checkVisible").disabled = !actionable;
  $("previewSelected").textContent = isUpdate ? "预检匹配" : "预检入库";
  $("executeSelected").textContent = isUpdate ? "覆盖更新正文" : "保存到知识库";
}
function setDataView(next) {
  dataView = next;
  articlePage = 1;
  selectedArticles.clear();
  [...$("dataTabs").children].forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.view === next),
  );
  loadArticles();
}
async function loadArticles() {
  try {
    const q = viewQuery(),
      params = new URLSearchParams({ page: articlePage, size: articleSize });
    Object.entries(q).forEach(([k, v]) => params.set(k, v));
    if (articleSource) params.set("source_code", articleSource);
    const keyword = $("keyword").value.trim();
    if (keyword) params.set("keyword", keyword);
    const [v, c] = await Promise.all([
      json("/api/articles?" + params),
      json("/api/articles/counts"),
    ]);
    articleTotal = v.total;
    $("countPending").textContent = c.pending || 0;
    $("countUpdate").textContent = c.update_pending || 0;
    $("countUpdateFailed").textContent = c.update_failed || 0;
    $("articleStats").textContent =
      contentUpdateEnabled
        ? `待入库 ${c.pending || 0} · 已同步 ${c.success || 0} · 待更新 ${c.update_pending || 0} · 更新失败 ${c.update_failed || 0} · 无法匹配 ${c.update_unmatched || 0}`
        : `待入库 ${c.pending || 0} · 已同步 ${c.success || 0} · 同步失败 ${c.failed || 0}`;
    $("articlesBody").innerHTML =
      v.items
        .map((a) => {
          const sync =
            a.kms_status === "success"
              ? '<span class="status success">已同步</span>'
              : a.kms_status === "failed"
                ? `<span class="status failed" title="${esc(a.last_error || a.kms_result_code || "同步失败")}">同步失败</span>`
                : '<span class="status pending">待入库</span>';
          const update =
            a.content_update_status === "success"
              ? '<span class="status success">已更新</span>'
              : a.content_update_status === "failed"
                ? `<span class="status failed" title="${esc(a.content_update_error || "更新失败")}">更新失败</span>`
                : a.content_update_status === "unmatched"
                  ? `<span class="status failed" title="${esc(a.content_update_error || "无法安全匹配 KMS 文档")}">无法匹配</span>`
                  : a.content_update_status === "pending"
                    ? '<span class="status pending">待更新</span>'
                    : '<span class="muted">无需更新</span>';
          const fmt = (x) => String(x || "").slice(0, 16),
            apply =
              a.apply_start && a.apply_end
                ? `${String(a.apply_start).slice(5)} ~ ${String(a.apply_end).slice(5)}`
                : fmt(a.apply_start) || fmt(a.apply_end) || "-";
          return `<tr><td><input type="checkbox" class="row-check" value="${esc(a.policy_crawler_article_id)}"${selectedArticles.has(a.policy_crawler_article_id) ? " checked" : ""}${dataView === "all" ? " disabled" : ""}></td><td class="title" title="${esc(a.title)}">${esc(a.title)}</td><td>${a.source_code === "qifuyun" ? "企服云" : "随申办"}</td><td>${esc(a.publish_date || "-")}</td><td>${esc(fmt(a.crawled_at))}</td><td>${esc(apply)}</td><td>${sync}</td><td data-content-update${contentUpdateEnabled ? "" : " hidden"}>${update}</td><td>${esc(fmt(a.pushed_at) || "-")}</td></tr>`;
        })
        .join("") ||
      '<tr><td colspan="9" class="muted">暂无符合条件的数据</td></tr>';
    const pages = Math.max(1, Math.ceil(v.total / articleSize));
    $("pageInfo").textContent = `共 ${v.total} 条`;
    $("pageNo").textContent = `${articlePage} / ${pages}`;
    $("prevPage").disabled = articlePage <= 1;
    $("nextPage").disabled = articlePage >= pages;
    $("checkAll").checked =
      v.items.length > 0 &&
      dataView !== "all" &&
      [...document.querySelectorAll(".row-check")].every((x) => x.checked);
    syncContentUpdateVisibility();
    updateActionControls();
  } catch (e) {
    log(`加载数据失败：${e.message}`);
  }
}
async function startCrawl(dryRun) {
  const codes = [...document.querySelectorAll(".task input:checked")].map(
    (x) => x.value,
  );
  if (!codes.length) return alert("请至少选择一个抓取来源");
  const refresh_existing = contentUpdateEnabled && $("refreshExisting").checked,
    auto_sync = $("autoSync").checked;
  const message = dryRun
    ? "将获取并检查政策，不写入本地库，也不调用 KMS。确认继续？"
    : refresh_existing
      ? "将重新抓取已有政策并比较原文；有变化时仅进入待更新，不会自动覆盖 KMS。确认继续？"
      : "将抓取政策并保存到本地库。确认继续？";
  if (!confirm(message)) return;
  try {
    const v = await json("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_codes: codes,
        dry_run: dryRun,
        confirm_write: !dryRun,
        phase: "crawl",
        auto_sync,
        refresh_existing,
      }),
    });
    watch(v.run_id);
  } catch (e) {
    alert(e.message);
  }
}
async function processSelected(dryRun) {
  const ids = [...selectedArticles],
    isUpdate = contentUpdateEnabled && (dataView === "update" || dataView === "updateFailed");
  if (!ids.length) return;
  const action = isUpdate
    ? "覆盖更新 KMS 正文并重新处理"
    : "保存到 KMS 知识库并拆解";
  const message = dryRun
    ? `将预检 ${ids.length} 条记录，不调用 KMS。确认继续？`
    : `将${action} ${ids.length} 条记录。确认继续？`;
  if (!confirm(message)) return;
  try {
    const v = await json(
      isUpdate ? "/api/articles/update" : "/api/articles/push",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          article_ids: ids,
          dry_run: dryRun,
          confirm_write: !dryRun,
        }),
      },
    );
    watch(v.run_id);
  } catch (e) {
    alert(e.message);
  }
}
function showTab(name) {
  const tasks = name === "tasks";
  const urls = name === "urls";
  $("panelTasks").style.display = tasks ? "" : "none";
  $("panelUrls").style.display = urls ? "" : "none";
  $("panelArticles").style.display = name === "articles" ? "" : "none";
  $("tabTasksBtn").classList.toggle("active", tasks);
  $("tabArticlesBtn").classList.toggle("active", name === "articles");
  $("tabUrlsBtn").classList.toggle("active", urls);
  if (name === "articles") loadArticles();
}
$("testKmsAuth").onclick = testKmsAuth;
$("saveKmsAuth").onclick = saveKmsAuth;
$("previewCrawl").onclick = () => startCrawl(true);
$("startCrawl").onclick = () => startCrawl(false);
async function startUrlCrawl(dryRun) {
  const raw = $("urlInput").value;
  const urls = [...new Set(raw.split(/[;\r\n]+/).map((item) => item.trim()).filter(Boolean))];
  if (!urls.length) return alert("请至少输入一条 URL");
  for (const url of urls) {
    try {
      const parsed = new URL(url);
      if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
    } catch {
      return alert(`URL 格式无效：${url}`);
    }
  }
  const message = dryRun
    ? `将自动识别并预检 ${urls.length} 条 URL，不写入本地库或 KMS。确认继续？`
    : `将自动识别并抓取 ${urls.length} 条 URL，写入本地待入库列表，不调用 KMS。确认继续？`;
  if (!confirm(message)) return;
  try {
    const v = await json("/api/url-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, dry_run: dryRun, confirm_write: !dryRun }),
    });
    watch(v.run_id, false, "url");
  } catch (e) {
    alert(e.message);
  }
}
$("previewUrlCrawl").onclick = () => startUrlCrawl(true);
$("startUrlCrawl").onclick = () => startUrlCrawl(false);
$("stop").onclick = async () => {
  if (currentRun && currentRunTarget === "tasks")
    try {
      await json(`/api/runs/${currentRun}/stop`, { method: "POST" });
    } catch (e) {
      alert(e.message);
    }
};
$("urlStop").onclick = async () => {
  if (currentRun && currentRunTarget === "url")
    try {
      await json(`/api/runs/${currentRun}/stop`, { method: "POST" });
    } catch (e) {
      alert(e.message);
    }
};
$("previewSelected").onclick = () => processSelected(true);
$("executeSelected").onclick = () => processSelected(false);
$("checkVisible").onclick = () => {
  document.querySelectorAll(".row-check:not(:checked)").forEach((x) => {
    x.checked = true;
    selectedArticles.add(x.value);
  });
  $("checkAll").checked = true;
  updateActionControls();
};
$("articlesBody").addEventListener("change", (e) => {
  const box = e.target;
  if (!box.classList.contains("row-check")) return;
  box.checked
    ? selectedArticles.add(box.value)
    : selectedArticles.delete(box.value);
  updateActionControls();
});
$("checkAll").onchange = (e) => {
  document.querySelectorAll(".row-check").forEach((x) => {
    x.checked = e.target.checked;
    e.target.checked
      ? selectedArticles.add(x.value)
      : selectedArticles.delete(x.value);
  });
  updateActionControls();
};
$("dataTabs").onclick = (e) => {
  const button = e.target.closest(".data-tab");
  if (button) setDataView(button.dataset.view);
};
$("sourceFilter").onclick = (e) => {
  const button = e.target.closest(".opt");
  if (!button) return;
  articleSource = button.dataset.source || "";
  articlePage = 1;
  [...$("sourceFilter").children].forEach((x) =>
    x.classList.toggle("on", x === button),
  );
  loadArticles();
};
$("keyword").onkeydown = (e) => {
  if (e.key === "Enter") {
    articlePage = 1;
    loadArticles();
  }
};
$("refreshArticles").onclick = () => {
  articlePage = 1;
  loadArticles();
};
$("prevPage").onclick = () => {
  if (articlePage > 1) {
    articlePage--;
    loadArticles();
  }
};
$("nextPage").onclick = () => {
  if (articlePage < Math.ceil(articleTotal / articleSize)) {
    articlePage++;
    loadArticles();
  }
};
$("tabTasksBtn").onclick = () => showTab("tasks");
$("tabArticlesBtn").onclick = () => showTab("articles");
$("tabUrlsBtn").onclick = () => showTab("urls");
async function retryRun(id) {
  if (!confirm("将仅重推该批次中 KMS 失败的数据，确认继续？")) return;
  try {
    const v = await json(`/api/runs/${id}/retry-failed`, { method: "POST" });
    watch(v.run_id);
  } catch (e) {
    alert(e.message);
  }
}
async function viewRun(id, target = "tasks") {
  try {
    const run = await json(`/api/runs/${id}`);
    runElement(target, "runId").textContent = id;
    stats(run, target);
    const running = run.status === "queued" || run.status === "running";
    runElement(target, "stop").disabled = !running;
    if (running) return watch(id, false, target);
    let text = `批次 ${run.run_id}（${taskLabel(run.trigger_type)} · ${run.dry_run ? "预检" : "正式"} · ${run.status}）\n总数 ${run.total} · 成功 ${run.succeeded} · 跳过 ${run.skipped} · 失败 ${run.failed}\n开始 ${run.started_at || "-"} · 结束 ${run.finished_at || "-"}\n${run.message || ""}\n\n—— 明细（最近 100 条）——`;
    const items = await json(`/api/runs/${id}/items?page=1&size=100`);
    items.items.forEach((it) => {
      text += `\n[${it.phase}] ${it.status} ${esc(it.article_title || it.source_item_id || "")}${it.message ? ` ${esc(it.message)}` : ""}`;
    });
    runElement(target, "logs").textContent = text;
    runElement(target, "logs").scrollTop = runElement(target, "logs").scrollHeight;
  } catch (e) {
    alert(`加载任务失败：${e.message}`);
  }
}
window.viewRun = viewRun;
window.retryRun = retryRun;
loadFeatures().then(load);
checkHealth();
setInterval(() => {
  load();
  if ($("panelArticles").style.display !== "none") loadArticles();
}, 30000);
