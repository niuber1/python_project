from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import STATIC_DIR, TASKS, get_settings
from .database import Database
from .engine import RunConflictError, RunManager
from .events import EventStore
from . import kms_kb
from .logging_config import configure_logging
from .models import KmsAuthConfigRequest, PushArticlesRequest, ReCrawlRequest, ScheduleConfigRequest, StartRunRequest, UpdateArticlesRequest, UrlRunRequest


settings = get_settings()
configure_logging(settings)
database = Database(settings)
events = EventStore(database)
manager = RunManager(settings, database, events)
scheduler: BackgroundScheduler | None = None
logger = logging.getLogger(__name__)


def scheduled_run(task_codes: list[str]) -> None:
    try:
        manager.start(task_codes, dry_run=False, trigger_type="schedule", phase="crawl")
    except RunConflictError:
        return


def close_stale_runs() -> None:
    """只收束已没有内存活动线程的陈旧批次，避免误停正在执行的任务。"""
    if manager.active_run:
        return
    try:
        affected = database.finish_stale_runs(settings.run_stale_minutes)
        if affected:
            logger.warning("已自动收束 %s 个超过 %s 分钟未更新的任务", affected, settings.run_stale_minutes)
    except Exception:
        logger.exception("陈旧任务自动收束失败")


def default_schedule_config() -> dict[str, Any]:
    return {"enabled": True, "hour": settings.schedule_hour, "minute": settings.schedule_minute, "task_codes": list(TASKS)}


def stored_schedule_config() -> dict[str, Any]:
    """读取持久化设置；升级未执行迁移时退回旧的 .env 调度，避免中断抓取。"""
    try:
        config = database.get_schedule_config()
        return config or database.save_schedule_config(**default_schedule_config())
    except Exception:
        logger.exception("定时配置表不可用，暂时使用 .env 默认调度；请执行 sql/002_schedule_config.sql")
        return default_schedule_config()


def apply_schedule(config: dict[str, Any]) -> None:
    if scheduler is None:
        return
    if scheduler.get_job("daily-policy-crawler"):
        scheduler.remove_job("daily-policy-crawler")
    if config["enabled"]:
        scheduler.add_job(
            scheduled_run, args=[config["task_codes"]],
            trigger=CronTrigger(hour=config["hour"], minute=config["minute"], timezone="Asia/Shanghai"),
            id="daily-policy-crawler", coalesce=True, max_instances=1, misfire_grace_time=3600,
        )


def schedule_response(config: dict[str, Any]) -> dict[str, Any]:
    job = scheduler.get_job("daily-policy-crawler") if scheduler else None
    return {**config, "next_run_time": job.next_run_time if job else None}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global scheduler
    if settings.bind_host not in {"127.0.0.1", "localhost", "::1"} and not (settings.admin_user and settings.admin_password):
        raise RuntimeError("非本机监听必须配置 CRAWLER_ADMIN_USER 和 CRAWLER_ADMIN_PASSWORD")
    interrupted = database.finish_interrupted_runs()
    if interrupted:
        logger.warning("服务启动时已结束 %s 个因重启中断的旧任务", interrupted)
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    apply_schedule(stored_schedule_config())
    scheduler.add_job(
        close_stale_runs,
        trigger=IntervalTrigger(minutes=5, timezone="Asia/Shanghai"),
        id="stale-policy-crawler-run-reaper",
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    scheduler = None


app = FastAPI(title="政策抓取入库运维工具", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    if settings.bind_host in {"127.0.0.1", "localhost", "::1"}:
        return await call_next(request)
    header = request.headers.get("Authorization", "")
    valid = False
    if header.startswith("Basic "):
        try:
            user, password = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)
            valid = secrets.compare_digest(user, settings.admin_user) and secrets.compare_digest(password, settings.admin_password)
        except (ValueError, UnicodeDecodeError):
            pass
    if not valid:
        return JSONResponse({"detail": "需要运维账号认证"}, status_code=401, headers={"WWW-Authenticate": "Basic"})
    return await call_next(request)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(STATIC_DIR) / "index.html")


@app.get("/api/tasks")
def get_tasks():
    try:
        runs = database.list_runs(15)
        pending = database.count_pending()
    except Exception:
        runs = []
        pending = 0
    return {"tasks": list(TASKS.values()), "active_run": manager.active_run, "recent_runs": runs, "pending_count": pending}


@app.get("/api/schedule")
def get_schedule():
    return schedule_response(stored_schedule_config())


@app.put("/api/schedule")
def set_schedule(body: ScheduleConfigRequest):
    unknown = sorted(set(body.task_codes) - set(TASKS))
    if unknown:
        raise HTTPException(400, f"未知定时任务: {', '.join(unknown)}")
    task_codes = list(dict.fromkeys(body.task_codes))
    if body.enabled and not task_codes:
        raise HTTPException(400, "启用定时抓取时至少选择一个任务")
    try:
        config = database.save_schedule_config(body.enabled, body.hour, body.minute, task_codes)
    except Exception as exc:
        raise HTTPException(503, "定时配置表尚未初始化，请先执行 sql/002_schedule_config.sql") from exc
    apply_schedule(config)
    return schedule_response(config)


@app.get("/api/kms-auth")
def get_kms_auth_status():
    """只返回是否已配置，绝不向页面回传鉴权原文。"""
    return {
        "has_application_credentials": bool(settings.kms_application_key and settings.kms_application_secret),
        "has_access_token": bool(settings.kms_access_token),
        "has_authorization": bool(settings.kms_authorization),
    }


@app.get("/api/features")
def get_features():
    return {"content_update_enabled": settings.enable_content_update}


@app.put("/api/kms-auth")
def set_kms_auth(body: KmsAuthConfigRequest):
    """更新当前进程的 KMS 鉴权；重启后恢复 .env / 环境变量的原始配置。"""
    if not settings.enable_content_update:
        raise HTTPException(404, "正文更新功能当前已关闭")
    settings.kms_access_token = body.access_token.strip()
    settings.kms_authorization = body.authorization.strip()
    settings.kms_cookie = ""
    return {
        "has_access_token": bool(settings.kms_access_token),
        "has_authorization": bool(settings.kms_authorization),
        "message": "KMS 鉴权已保存到当前服务内存，重启服务后需重新配置。",
    }


@app.post("/api/kms-auth/test")
def test_kms_auth(body: KmsAuthConfigRequest):
    """优先校验页面输入；留空时按文档 2.1 自动获取应用令牌。"""
    if not settings.enable_content_update:
        raise HTTPException(404, "正文更新功能当前已关闭")
    from .kms_client import KmsClient

    kms = KmsClient(settings)
    try:
        result = kms.test_auth(body.access_token, body.authorization)
    finally:
        kms.close()
    return {"ok": result.success, "code": result.code, "message": result.message}


@app.get("/api/articles")
def list_articles(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    status: str | None = None,
    source_code: str | None = None,
    keyword: str | None = None,
    update_status: str | None = None,
    policy_type: str | None = None,
    policy_level: str | None = None,
    crawl_status: str | None = None,
):
    return database.list_articles(
        page, size,
        status=(status or "").strip() or None,
        source_code=(source_code or "").strip() or None,
        keyword=(keyword or "").strip() or None,
        update_status=(update_status or "").strip() or None,
        policy_type=(policy_type or "").strip() or None,
        policy_level=(policy_level or "").strip() or None,
        crawl_status=(crawl_status or "").strip() or None,
    )


_counts_cache: dict[str, Any] = {"at": 0.0, "data": None}
_COUNTS_TTL_SECONDS = 3.0


@app.get("/api/articles/counts")
def article_counts():
    now = time.monotonic()
    if _counts_cache["data"] is not None and now - _counts_cache["at"] < _COUNTS_TTL_SECONDS:
        return _counts_cache["data"]
    try:
        data = database.article_status_counts()
        if settings.enable_content_update:
            data.update({f"update_{key}": value for key, value in database.content_update_counts().items()})
    except Exception:
        data = {"pending": 0, "processing": 0, "success": 0, "failed": 0, "classification_failed": 0, "update_pending": 0, "update_failed": 0, "update_unmatched": 0, "update_success": 0, "update_not_needed": 0}
    _counts_cache.update(at=now, data=data)
    return data


@app.post("/api/articles/reconcile")
def reconcile_articles():
    """用 KMS 有效文档精确标题对账，修复旧批次未回写的本地状态。"""
    try:
        pending = database.list_pending_articles()
        existing_titles = kms_kb.existing_titles(settings, [row["title"] for row in pending])
        reconciled = database.mark_articles_reconciled([
            row["policy_crawler_article_id"] for row in pending if row["title"] in existing_titles
        ])
        _counts_cache.update(at=0.0, data=None)
        return {"reconciled": reconciled, "remaining_pending": len(pending) - reconciled}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"KMS 状态对账失败：{exc}") from exc


@app.post("/api/articles/push", status_code=202)
def push_articles(body: PushArticlesRequest):
    if not body.dry_run and not body.confirm_write:
        raise HTTPException(400, "正式入库必须设置 confirm_write=true")
    ids = list(dict.fromkeys(body.article_ids))
    if database.active_classification_failures(ids):
        raise HTTPException(400, "抓取失败记录尚未完成智能体分类，不能保存到知识库")
    rows = database.get_articles_by_ids(ids)
    found = {row["policy_crawler_article_id"] for row in rows}
    missing = sorted(set(ids) - found)
    if missing:
        raise HTTPException(400, f"以下记录不存在: {', '.join(missing[:5])}")
    try:
        run_id = manager.push_articles(ids, body.dry_run)
    except RunConflictError as exc:
        raise HTTPException(409, {"message": "已有任务运行中", "active_run": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _counts_cache.update(at=0.0, data=None)
    return {"run_id": run_id, "status": "queued"}


@app.post("/api/articles/re-crawl", status_code=202)
def recrawl_articles(body: ReCrawlRequest):
    failure_ids = list(dict.fromkeys(body.failure_ids))
    rows = database.active_classification_failures(failure_ids)
    found = {row["policy_crawler_classification_failure_id"] for row in rows}
    missing = sorted(set(failure_ids) - found)
    if missing:
        raise HTTPException(400, f"以下抓取失败记录不存在或已处理: {', '.join(missing[:5])}")
    try:
        run_id = manager.recrawl_classification_failures(failure_ids)
    except RunConflictError as exc:
        raise HTTPException(409, {"message": "已有任务运行中", "active_run": str(exc)}) from exc
    return {"run_id": run_id, "status": "queued"}


@app.post("/api/articles/re-crawl-all", status_code=202)
def recrawl_all_articles():
    total = database.active_classification_failure_count()
    if total == 0:
        raise HTTPException(status_code=400, detail="当前没有待重新抓取的失败记录")
    try:
        run_id = manager.recrawl_classification_failures(None)
    except RunConflictError as exc:
        raise HTTPException(409, {"message": "已有任务运行中", "active_run": str(exc)}) from exc
    return {"run_id": run_id, "status": "queued", "total": total}


@app.post("/api/articles/update", status_code=202)
def update_articles(body: UpdateArticlesRequest):
    if not settings.enable_content_update:
        raise HTTPException(404, "正文更新功能当前已关闭")
    if not body.dry_run and not body.confirm_write:
        raise HTTPException(400, "正式覆盖更新必须设置 confirm_write=true")
    ids = list(dict.fromkeys(body.article_ids))
    rows = database.get_articles_by_ids(ids)
    found = {row["policy_crawler_article_id"] for row in rows}
    missing = sorted(set(ids) - found)
    if missing:
        raise HTTPException(400, f"以下记录不存在: {', '.join(missing[:5])}")
    try:
        run_id = manager.update_articles(ids, body.dry_run)
    except RunConflictError as exc:
        raise HTTPException(409, {"message": "已有任务运行中", "active_run": str(exc)}) from exc
    return {"run_id": run_id, "status": "queued"}


@app.post("/api/runs", status_code=202)
def start_run(body: StartRunRequest):
    if not body.dry_run and not body.confirm_write:
        raise HTTPException(400, "正式执行必须设置 confirm_write=true")
    try:
        run_id = manager.start(body.task_codes, body.dry_run, phase=body.phase, auto_sync=body.auto_sync, refresh_existing=body.refresh_existing)
    except RunConflictError as exc:
        raise HTTPException(409, {"message": "已有任务运行中", "active_run": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"run_id": run_id, "status": "queued"}


@app.post("/api/url-runs", status_code=202)
def start_url_run(body: UrlRunRequest):
    if not body.dry_run and not body.confirm_write:
        raise HTTPException(400, "正式 URL 抓取必须设置 confirm_write=true")
    try:
        run_id = manager.start_url_run(body.urls, body.dry_run)
    except RunConflictError as exc:
        raise HTTPException(409, {"message": "已有任务运行中", "active_run": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    row = database.get_run(run_id)
    if not row:
        raise HTTPException(404, "批次不存在")
    return row


@app.get("/api/runs/{run_id}/items")
def get_run_items(
    run_id: str,
    page: int = Query(1, ge=1),
    size: int = Query(30, ge=1, le=200),
    status: str | None = None,
):
    if not database.get_run(run_id):
        raise HTTPException(404, "批次不存在")
    return database.list_run_items(run_id, page, size, status)


@app.get("/api/runs/{run_id}/events")
async def run_events(request: Request, run_id: str):
    if not database.get_run(run_id):
        raise HTTPException(404, "批次不存在")
    try:
        last_id = int(request.headers.get("Last-Event-ID", "0"))
    except ValueError:
        last_id = 0

    async def stream():
        cursor = last_id
        if cursor == 0:
            history = await asyncio.to_thread(events.recent, run_id)
            for event in history:
                cursor = event["id"]
                yield events.sse(event)
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(events.wait_after, run_id, cursor, 15)
            if not batch:
                yield ": keepalive\n\n"
                continue
            for event in batch:
                cursor = event["id"]
                yield events.sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/retry-failed", status_code=202)
def retry_failed(run_id: str):
    try:
        new_run_id = manager.retry_failed(run_id)
    except KeyError as exc:
        raise HTTPException(404, "批次不存在") from exc
    except RunConflictError as exc:
        raise HTTPException(409, {"message": "已有任务运行中", "active_run": str(exc)}) from exc
    return {"run_id": new_run_id, "status": "queued"}


@app.post("/api/runs/{run_id}/stop", status_code=202)
def stop_run(run_id: str):
    if not manager.stop(run_id):
        raise HTTPException(409, "该任务未在运行")
    return {"run_id": run_id, "status": "stopping"}


@app.get("/api/health")
def health():
    sources = {}
    for name, url in {
        "suishenban": "https://zwdt.sh.gov.cn/qykj/shell_oc_policy_zq/policy/index",
        "qifuyun": "https://shpolicy.ssme.sh.gov.cn/knowledge/",
        "shanghai_policy_platform": "https://www.shanghai.gov.cn/zhengce/more?level=city",
    }.items():
        try:
            response = httpx.get(url, timeout=10, follow_redirects=True)
            sources[name] = {"ok": response.status_code < 500, "status_code": response.status_code}
        except httpx.HTTPError as exc:
            sources[name] = {"ok": False, "message": type(exc).__name__}
    db_health = database.health()
    config_ok = bool(settings.db_user and settings.kms_base_url)
    return {"ok": db_health["ok"] and config_ok and all(item["ok"] for item in sources.values()), "database": db_health, "configuration": {"ok": config_ok}, "sources": sources}
