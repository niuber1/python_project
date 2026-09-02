from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from .adapters import QifuyunAdapter, SuishenbanAdapter
from .adapters.base import SourceEmptyError
from .config import TASKS, Settings
from .database import Database
from .events import EventStore
from .html_utils import compose_document_content, content_sha256
from . import kms_kb
from .kms_client import KmsClient
from .models import CrawlerPayload, KmsResult, PolicyArticle, deterministic_document_id
from .url_inputs import candidate_from_url, normalize_urls


logger = logging.getLogger(__name__)


class RunConflictError(RuntimeError):
    pass


def _error_summary(exc: Exception) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:500]}"


class RunManager:
    def __init__(self, settings: Settings, database: Database, events: EventStore):
        self.settings = settings
        self.db = database
        self.events = events
        self._guard = threading.Lock()
        self._active_run: str | None = None
        self._stop_flags: dict[str, threading.Event] = {}

    @property
    def active_run(self) -> str | None:
        with self._guard:
            return self._active_run

    def start(self, task_codes: list[str], dry_run: bool, trigger_type: str = "manual", phase: str = "all", auto_sync: bool = False, refresh_existing: bool = False) -> str:
        if phase not in {"all", "crawl", "push"}:
            raise ValueError(f"未知阶段: {phase}")
        if refresh_existing and not self.settings.enable_content_update:
            raise ValueError("正文更新功能当前已关闭，不能重新抓取并比对已存在政策")
        task_codes = task_codes or list(TASKS)
        unknown = sorted(set(task_codes) - set(TASKS))
        if unknown:
            raise ValueError(f"未知任务: {', '.join(unknown)}")
        with self._guard:
            if self._active_run:
                raise RunConflictError(self._active_run)
            run_id = uuid.uuid4().hex
            self._active_run = run_id
            self._stop_flags[run_id] = threading.Event()
        recorded_trigger = trigger_type if phase == "all" else f"{trigger_type}-{phase}"
        try:
            self.db.create_run(run_id, recorded_trigger, task_codes, dry_run)
        except Exception:
            self._release(run_id)
            raise
        threading.Thread(target=self._execute, args=(run_id, task_codes, dry_run, phase, auto_sync, refresh_existing), daemon=True, name=f"crawler-{run_id[:8]}").start()
        return run_id

    def start_url_run(self, urls: list[str], source_code: str, dry_run: bool) -> str:
        """只抓取输入的来源详情 URL，成功数据进入本地待入库列表。"""
        normalized_urls = normalize_urls(urls)
        task_code = f"{source_code}_declare"
        if task_code not in TASKS:
            raise ValueError(f"不支持的 URL 来源：{source_code}")
        with self._guard:
            if self._active_run:
                raise RunConflictError(self._active_run)
            run_id = uuid.uuid4().hex
            self._active_run = run_id
            self._stop_flags[run_id] = threading.Event()
        try:
            self.db.create_run(run_id, "manual-url", [f"url:{source_code}", *normalized_urls], dry_run)
        except Exception:
            self._release(run_id)
            raise
        threading.Thread(
            target=self._execute_url, args=(run_id, task_code, normalized_urls, dry_run),
            daemon=True, name=f"url-crawler-{run_id[:8]}",
        ).start()
        return run_id

    def retry_failed(self, original_run_id: str) -> str:
        if not self.db.get_run(original_run_id):
            raise KeyError(original_run_id)
        with self._guard:
            if self._active_run:
                raise RunConflictError(self._active_run)
            run_id = uuid.uuid4().hex
            self._active_run = run_id
            self._stop_flags[run_id] = threading.Event()
        try:
            self.db.create_run(run_id, "retry", [f"retry:{original_run_id}"], False)
        except Exception:
            self._release(run_id)
            raise
        threading.Thread(target=self._execute_retry, args=(run_id, original_run_id), daemon=True).start()
        return run_id

    def stop(self, run_id: str) -> bool:
        flag = self._stop_flags.get(run_id)
        if not flag:
            return False
        flag.set()
        self.events.emit(run_id, "warning", "已收到停止请求，当前记录处理完成后停止")
        return True

    def _release(self, run_id: str) -> None:
        with self._guard:
            if self._active_run == run_id:
                self._active_run = None
            self._stop_flags.pop(run_id, None)

    def _adapter(self, task_code: str, client: httpx.Client):
        return SuishenbanAdapter(client) if task_code == "suishenban_declare" else QifuyunAdapter(client)

    @staticmethod
    def _payload(article: PolicyArticle, content: str, base_id: str) -> CrawlerPayload:
        metadata = {
            "项目名称": article.project_name,
            "政策层级": article.policy_level or "",
            "发文部门": article.publish_dept or "",
            "申报开始时间": article.apply_start.isoformat() if article.apply_start else "",
            "申报结束时间": article.apply_end.isoformat() if article.apply_end else "",
            "文档来源": article.source_name,
        }
        return CrawlerPayload(
            id=deterministic_document_id(article.source_code, article.source_item_id, base_id),
            bt=article.title, url=article.original_url,
            pubDate=article.publish_date.isoformat() if article.publish_date else None,
            wh=article.document_no, content=content, source=article.source_name,
            baseId=base_id, metadata=metadata,
        )

    def _execute(self, run_id: str, task_codes: list[str], dry_run: bool, phase: str = "all", auto_sync: bool = False, refresh_existing: bool = False) -> None:
        if phase == "push":
            self._execute_push(run_id, task_codes, dry_run)
            return
        counts = {"total": 0, "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0}
        stop_flag = self._stop_flags[run_id]
        push_kms = phase != "crawl"
        seen_titles: set[str] = set()
        self.db.update_run(run_id, status="running", started_at=datetime.now(), message="任务运行中")
        self.events.emit(run_id, "status", "任务开始" + ("（重新抓取并比对已存在政策）" if refresh_existing else ""), dry_run=dry_run)
        kms: KmsClient | None = None
        try:
            with httpx.Client(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://zwdt.sh.gov.cn/qykj/shell_oc_policy_zq/policy/index?from=zwfw",
                },
            ) as client:
                if push_kms:
                    kms = KmsClient(self.settings)
                for task_code in task_codes:
                    if stop_flag.is_set():
                        break
                    task = TASKS[task_code]
                    adapter = self._adapter(task_code, client)
                    self.events.emit(run_id, "log", f"开始发现：{task['name']}", task_code=task_code)
                    candidates = adapter.discover()
                    # 同一批次内按项目名去重，避免同一项目被重复抓取
                    seen_names: set[str] = set()
                    unique: list[Any] = []
                    for item in candidates:
                        key = (item.project_name or "").strip()
                        if key and key in seen_names:
                            continue
                        seen_names.add(key)
                        unique.append(item)
                    candidates = unique
                    if self.settings.max_items_per_task:
                        candidates = candidates[:self.settings.max_items_per_task]
                    counts["total"] += len(candidates)
                    self.db.update_run(run_id, total=counts["total"])
                    existing = self.db.find_existing(task["source_code"], [item.source_item_id for item in candidates], task["base_id"])
                    for candidate in candidates:
                        if stop_flag.is_set():
                            break
                        outcome = self._process_candidate(run_id, task_code, task, candidate, existing.get(candidate.source_item_id), dry_run, adapter, kms, push_kms, seen_titles, refresh_existing)
                        counts["processed"] += 1
                        counts[outcome] += 1
                        self.db.update_run(run_id, **counts, current_item=candidate.source_item_id)
                        self.events.emit(run_id, "progress", candidate.project_name, **counts)
                        if self.settings.item_delay_seconds:
                            time.sleep(self.settings.item_delay_seconds)
            status = "stopped" if stop_flag.is_set() else "completed"
            message = "任务已停止" if status == "stopped" else ("抓取完成" if not push_kms else "任务完成")
            self.db.update_run(run_id, status=status, finished_at=datetime.now(), message=message, **counts)
            next_run_id = None
            # 重新抓取产生的是待人工确认的覆盖更新，不能被自动同步直接推送到 KMS。
            if status == "completed" and not push_kms and auto_sync and not refresh_existing and not dry_run:
                next_run_id = self._start_auto_push(run_id)
                if next_run_id:
                    message = f"{message}；自动同步知识库：已开始入库"
            self.events.emit(run_id, "complete", message, status=status, next_run_id=next_run_id or "", **counts)
        except Exception as exc:
            logger.exception("run_id=%s task failed", run_id)
            self.db.update_run(run_id, status="failed", finished_at=datetime.now(), message=_error_summary(exc), **counts)
            self.events.emit(run_id, "error", _error_summary(exc), **counts)
        finally:
            if kms:
                kms.close()
            self._release(run_id)

    def _execute_url(self, run_id: str, task_code: str, urls: list[str], dry_run: bool) -> None:
        """URL 批次沿用候选处理、去重和台账，不创建 KMS 客户端。"""
        counts = {"total": len(urls), "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0}
        stop_flag = self._stop_flags[run_id]
        task = TASKS[task_code]
        self.db.update_run(run_id, status="running", started_at=datetime.now(), message="URL 抓取运行中", total=len(urls))
        self.events.emit(run_id, "status", "开始 URL 抓取", dry_run=dry_run, **counts)
        try:
            with httpx.Client(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://zwdt.sh.gov.cn/qykj/shell_oc_policy_zq/policy/index?from=zwfw",
                },
            ) as client:
                adapter = self._adapter(task_code, client)
                seen_titles: set[str] = set()
                for url in urls:
                    if stop_flag.is_set():
                        break
                    try:
                        candidate = candidate_from_url(task["source_code"], url)
                    except ValueError as exc:
                        item_id = uuid.uuid4().hex
                        self.db.create_run_item(
                            run_item_id=item_id, run_id=run_id, task_code=task_code, source_code=task["source_code"],
                            source_item_id=url, status="failed", phase="validate", message=str(exc), error_detail=str(exc), finished_at=datetime.now(),
                        )
                        outcome = "failed"
                    else:
                        existing = self.db.find_existing(task["source_code"], [candidate.source_item_id], task["base_id"])
                        outcome = self._process_candidate(
                            run_id, task_code, task, candidate, existing.get(candidate.source_item_id), dry_run,
                            adapter, None, push_kms=False, seen_titles=seen_titles,
                        )
                    counts["processed"] += 1
                    counts[outcome] += 1
                    self.db.update_run(run_id, **counts, current_item=url)
                    self.events.emit(run_id, "progress", url, **counts)
                    if self.settings.item_delay_seconds:
                        time.sleep(self.settings.item_delay_seconds)
            status = "stopped" if stop_flag.is_set() else "completed"
            message = "URL 抓取已停止" if status == "stopped" else "URL 抓取完成；成功记录已进入待入库"
            self.db.update_run(run_id, status=status, finished_at=datetime.now(), message=message, **counts)
            self.events.emit(run_id, "complete", message, status=status, **counts)
        except Exception as exc:
            logger.exception("run_id=%s URL task failed", run_id)
            self.db.update_run(run_id, status="failed", finished_at=datetime.now(), message=_error_summary(exc), **counts)
            self.events.emit(run_id, "error", _error_summary(exc), **counts)
        finally:
            self._release(run_id)

    def _start_auto_push(self, crawl_run_id: str) -> str | None:
        """爬取批次完成后，将本次新抓取的文章自动链式推送到 KMS（等效「保存到知识库」）。"""
        try:
            article_ids = self.db.list_run_article_ids(crawl_run_id)
        except Exception:
            logger.exception("run_id=%s auto_sync: list new articles failed", crawl_run_id)
            return None
        if not article_ids:
            return None
        with self._guard:
            if self._active_run != crawl_run_id:
                return None
            self._active_run = None
            self._stop_flags.pop(crawl_run_id, None)
            push_run_id = uuid.uuid4().hex
            self._active_run = push_run_id
            self._stop_flags[push_run_id] = threading.Event()
        try:
            self.db.create_run(push_run_id, "manual-push", ["push-selected"], False)
        except Exception:
            logger.exception("run_id=%s auto_sync: create push run failed", crawl_run_id)
            self._release(push_run_id)
            return None
        threading.Thread(target=self._execute_push_ids, args=(push_run_id, article_ids, False), daemon=True, name=f"push-{push_run_id[:8]}").start()
        logger.info("run_id=%s auto_sync: push run %s started with %d articles", crawl_run_id, push_run_id, len(article_ids))
        return push_run_id

    def _process_candidate(self, run_id: str, task_code: str, task: dict[str, Any], candidate, existing, dry_run: bool, adapter, kms: KmsClient | None, push_kms: bool = True, seen_titles: set[str] | None = None, refresh_existing: bool = False) -> str:
        started = time.monotonic()
        item_id = uuid.uuid4().hex
        self.db.create_run_item(
            run_item_id=item_id, run_id=run_id, task_code=task_code, source_code=candidate.source_code,
            source_item_id=candidate.source_item_id, status="running", phase="discover", message=candidate.project_name,
        )
        try:
            if existing and not refresh_existing:
                article_id = existing["policy_crawler_article_id"]
                if not push_kms:
                    self.db.update_run_item(item_id, policy_crawler_article_id=article_id, phase="deduplicate", status="skipped", message="已抓取过；如需入库请使用「仅入库」", finished_at=datetime.now())
                    return "skipped"
                code = str(existing.get("kms_result_code") or "")
                if existing.get("kms_status") == "success" or code == "7":
                    self.db.update_run_item(item_id, policy_crawler_article_id=article_id, phase="deduplicate", status="skipped", message="已存在且 KMS 已成功", finished_at=datetime.now())
                    return "skipped"
                if dry_run:
                    self.db.update_run_item(item_id, policy_crawler_article_id=article_id, phase="deduplicate", status="dry_run", message="已存在；正式运行时将只重推 payload", finished_at=datetime.now())
                    return "skipped"
                payload = CrawlerPayload.model_validate(json.loads(existing["kms_payload_json"]))
                logger.info(
                    "run_id=%s task_code=%s source_item_id=%s kms_document_id=%s base_id=%s action=kms_repush",
                    run_id, task_code, candidate.source_item_id, payload.id, payload.base_id,
                )
                result = kms.push(payload)
                self.db.update_article_kms(article_id, result)
                self.db.update_run_item(
                    item_id, policy_crawler_article_id=article_id, kms_document_id=payload.id, phase="kms", status="success" if result.success else "failed",
                    kms_result_code=result.code, retry_count=result.attempts, message=result.message,
                    duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now(),
                )
                logger.info("run_id=%s task_code=%s source_item_id=%s kms_document_id=%s base_id=%s result_code=%s", run_id, task_code, candidate.source_item_id, payload.id, payload.base_id, result.code)
                return "succeeded" if result.success else "failed"
            article = adapter.fetch(candidate)
            try:
                content = compose_document_content(article)
            except ValueError as exc:
                # 原文只含媒体、脚本等被剔除内容时，不作为失败记录入库。
                raise SourceEmptyError("政策正文清洗后为空") from exc
            payload = self._payload(article, content, task["base_id"])
            if existing:
                # 重新抓取仅比对本地已入库快照，绝不在此步骤调用 KMS。
                article_id = existing["policy_crawler_article_id"]
                changed = content_sha256(content) != str(existing.get("content_hash") or "")
                if dry_run:
                    message = "原文有变化；正式运行将保存最新快照并标记待更新" if changed else "原文无变化；不会更新本地或 KMS"
                    self.db.update_run_item(item_id, policy_crawler_article_id=article_id, phase="validate", status="dry_run" if changed else "skipped", message=message, duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now())
                    return "succeeded" if changed else "skipped"
                if not changed:
                    self.db.update_run_item(item_id, policy_crawler_article_id=article_id, phase="deduplicate", status="skipped", message="原文无变化，已跳过", duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now())
                    return "skipped"
                mark_for_update = existing.get("kms_status") == "success"
                self.db.refresh_existing_article(article_id, article, content, payload, content_sha256(content), mark_for_update)
                message = "原文有变化，已保存最新快照；请在“待更新”中确认覆盖 KMS" if mark_for_update else "原文有变化，已保存最新快照；该记录尚未入库 KMS"
                self.db.update_run_item(item_id, policy_crawler_article_id=article_id, kms_document_id=payload.id, phase="store", status="success", message=message, duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now())
                return "succeeded"
            # 标题去重：同批次内重复标题，或库中已存在同标题，直接跳过（不重复抓取入库）
            title_key = (article.title or "").strip()
            if title_key and seen_titles is not None:
                if title_key in seen_titles or self.db.find_existing_by_title(title_key):
                    seen_titles.add(title_key)
                    self.db.update_run_item(item_id, phase="deduplicate", status="skipped", message="标题重复（本批次或库中已存在），跳过", duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now())
                    logger.info("run_id=%s task_code=%s source_item_id=%s skipped, duplicate title", run_id, task_code, candidate.source_item_id)
                    return "skipped"
                seen_titles.add(title_key)
            if dry_run:
                self.db.update_run_item(item_id, phase="validate", status="dry_run", message="抓取及 KMS payload 校验通过，未写数据库/KMS", duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now())
                return "succeeded"
            article_id = str(uuid.uuid4())
            inserted = self.db.insert_article(article_id, article, content, payload, content_sha256(content))
            if not inserted:
                self.db.update_run_item(item_id, phase="store", status="skipped", message="并发唯一键命中，已跳过", finished_at=datetime.now())
                return "skipped"
            if not push_kms:
                self.db.update_run_item(
                    item_id, policy_crawler_article_id=article_id, kms_document_id=payload.id, phase="store", status="success",
                    message="已抓取入库本地库，待推 KMS", duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now(),
                )
                logger.info("run_id=%s task_code=%s source_item_id=%s stored, awaiting push", run_id, task_code, candidate.source_item_id)
                return "succeeded"
            result = kms.push(payload)
            self.db.update_article_kms(article_id, result)
            self.db.update_run_item(
                item_id, policy_crawler_article_id=article_id, kms_document_id=payload.id, phase="kms", status="success" if result.success else "failed",
                kms_result_code=result.code, retry_count=result.attempts, message=result.message,
                duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now(),
            )
            logger.info("run_id=%s task_code=%s source_item_id=%s kms_document_id=%s base_id=%s result_code=%s", run_id, task_code, candidate.source_item_id, payload.id, payload.base_id, result.code)
            return "succeeded" if result.success else "failed"
        except SourceEmptyError as exc:
            self.db.update_run_item(item_id, phase="fetch", status="source_empty", message=str(exc), duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now())
            return "skipped"
        except Exception as exc:
            logger.exception("run_id=%s task_code=%s source_item_id=%s failed", run_id, task_code, candidate.source_item_id)
            self.db.update_run_item(item_id, phase="failed", status="failed", message=_error_summary(exc), error_detail=_error_summary(exc), duration_ms=int((time.monotonic() - started) * 1000), finished_at=datetime.now())
            return "failed"

    def _execute_push(self, run_id: str, task_codes: list[str], dry_run: bool) -> None:
        counts = {"total": 0, "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0}
        stop_flag = self._stop_flags[run_id]
        source_codes: list[str] = []
        for code in task_codes:
            source = TASKS.get(code, {}).get("source_code")
            if source and source not in source_codes:
                source_codes.append(source)
        self.db.update_run(run_id, status="running", started_at=datetime.now(), message="入库运行中")
        self.events.emit(run_id, "status", "开始入库", dry_run=dry_run)
        kms: KmsClient | None = None
        try:
            rows = self.db.list_pending_articles(source_codes or None)
            counts["total"] = len(rows)
            self.db.update_run(run_id, total=counts["total"])
            if dry_run:
                self.db.update_run(run_id, status="completed", finished_at=datetime.now(), message=f"预检：待入库 {len(rows)} 条（未调用 KMS）", **counts)
                self.events.emit(run_id, "complete", f"预检完成：待入库 {len(rows)} 条，未调用 KMS", status="completed", **counts)
                return
            kms = KmsClient(self.settings)
            for row in rows:
                if stop_flag.is_set():
                    break
                item_id = uuid.uuid4().hex
                article_id = row["policy_crawler_article_id"]
                self.db.create_run_item(
                    run_item_id=item_id, run_id=run_id, task_code="push", source_code=row["source_code"],
                    source_item_id=row["source_item_id"], policy_crawler_article_id=article_id,
                    kms_document_id=row["kms_document_id"], status="running", phase="kms", message="从本地库待入库记录推送",
                )
                try:
                    payload = CrawlerPayload.model_validate(json.loads(row["kms_payload_json"]))
                    result = kms.push(payload)
                    self.db.update_article_kms(article_id, result)
                    self.db.update_run_item(
                        item_id, status="success" if result.success else "failed", kms_result_code=result.code,
                        retry_count=result.attempts, message=result.message, finished_at=datetime.now(),
                    )
                    outcome = "succeeded" if result.success else "failed"
                    logger.info("run_id=%s task_code=push source_item_id=%s kms_document_id=%s base_id=%s result_code=%s", run_id, row["source_item_id"], row["kms_document_id"], row["base_id"], result.code)
                except Exception as exc:
                    logger.exception("push run_id=%s article_id=%s failed", run_id, article_id)
                    self.db.update_run_item(item_id, status="failed", message=_error_summary(exc), error_detail=_error_summary(exc), finished_at=datetime.now())
                    outcome = "failed"
                counts["processed"] += 1
                counts[outcome] += 1
                self.db.update_run(run_id, **counts, current_item=row["source_item_id"])
                self.events.emit(run_id, "progress", row["title"], **counts)
                if self.settings.item_delay_seconds:
                    time.sleep(self.settings.item_delay_seconds)
            status = "stopped" if stop_flag.is_set() else "completed"
            message = "入库已停止" if status == "stopped" else "入库完成"
            self.db.update_run(run_id, status=status, finished_at=datetime.now(), message=message, **counts)
            self.events.emit(run_id, "complete", message, status=status, **counts)
        except Exception as exc:
            logger.exception("push run_id=%s failed", run_id)
            self.db.update_run(run_id, status="failed", finished_at=datetime.now(), message=_error_summary(exc), **counts)
            self.events.emit(run_id, "error", _error_summary(exc), **counts)
        finally:
            if kms:
                kms.close()
            self._release(run_id)

    def push_articles(self, article_ids: list[str], dry_run: bool) -> str:
        with self._guard:
            if self._active_run:
                raise RunConflictError(self._active_run)
            run_id = uuid.uuid4().hex
            self._active_run = run_id
            self._stop_flags[run_id] = threading.Event()
        try:
            self.db.create_run(run_id, "manual-push", ["push-selected"], dry_run)
        except Exception:
            self._release(run_id)
            raise
        threading.Thread(target=self._execute_push_ids, args=(run_id, article_ids, dry_run), daemon=True, name=f"push-{run_id[:8]}").start()
        return run_id

    def _execute_push_ids(self, run_id: str, article_ids: list[str], dry_run: bool) -> None:
        counts = {"total": 0, "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0}
        stop_flag = self._stop_flags[run_id]
        self.db.update_run(run_id, status="running", started_at=datetime.now(), message="入库运行中")
        self.events.emit(run_id, "status", f"开始入库（选中 {len(article_ids)} 条）", dry_run=dry_run)
        kms: KmsClient | None = None
        try:
            rows = self.db.get_articles_by_ids(list(dict.fromkeys(article_ids)))
            rows = sorted(rows, key=lambda r: r.get("crawled_at") or datetime.min)
            counts["total"] = len(rows)
            self.db.update_run(run_id, total=counts["total"])
            if dry_run:
                self.db.update_run(run_id, status="completed", finished_at=datetime.now(), message=f"预检：将入库 {len(rows)} 条（未调用 KMS）", **counts)
                self.events.emit(run_id, "complete", f"预检完成：将入库 {len(rows)} 条，未调用 KMS", status="completed", **counts)
                return
            kms = KmsClient(self.settings)
            for row in rows:
                if stop_flag.is_set():
                    break
                item_id = uuid.uuid4().hex
                article_id = row["policy_crawler_article_id"]
                self.db.create_run_item(
                    run_item_id=item_id, run_id=run_id, task_code="push", source_code=row["source_code"],
                    source_item_id=row["source_item_id"], policy_crawler_article_id=article_id,
                    kms_document_id=row["kms_document_id"], status="running", phase="kms", message="勾选入库推送",
                )
                try:
                    payload = CrawlerPayload.model_validate(json.loads(row["kms_payload_json"]))
                    # 知识库查重：同库同标题已存在则不再调用 crawlerToBase，直接标记已同步
                    if kms_kb.title_exists_in_base(self.settings, row["title"], row["base_id"]):
                        result = KmsResult(success=True, code="7", message="知识库中已存在同标题政策，标记已同步", attempts=1)
                        logger.info("run_id=%s article_id=%s title already in KMS KB, marked synced", run_id, article_id)
                    else:
                        result = kms.push(payload)
                    self.db.update_article_kms(article_id, result)
                    self.db.update_run_item(
                        item_id, status="success" if result.success else "failed", kms_result_code=result.code,
                        retry_count=result.attempts, message=result.message, finished_at=datetime.now(),
                    )
                    outcome = "succeeded" if result.success else "failed"
                    logger.info("run_id=%s task_code=push source_item_id=%s kms_document_id=%s base_id=%s result_code=%s", run_id, row["source_item_id"], row["kms_document_id"], row["base_id"], result.code)
                except Exception as exc:
                    logger.exception("push run_id=%s article_id=%s failed", run_id, article_id)
                    self.db.update_run_item(item_id, status="failed", message=_error_summary(exc), error_detail=_error_summary(exc), finished_at=datetime.now())
                    outcome = "failed"
                counts["processed"] += 1
                counts[outcome] += 1
                self.db.update_run(run_id, **counts, current_item=row["source_item_id"])
                self.events.emit(run_id, "progress", row["title"], **counts)
                if self.settings.item_delay_seconds:
                    time.sleep(self.settings.item_delay_seconds)
            status = "stopped" if stop_flag.is_set() else "completed"
            message = "入库已停止" if status == "stopped" else "入库完成"
            self.db.update_run(run_id, status=status, finished_at=datetime.now(), message=message, **counts)
            self.events.emit(run_id, "complete", message, status=status, **counts)
        except Exception as exc:
            logger.exception("push run_id=%s failed", run_id)
            self.db.update_run(run_id, status="failed", finished_at=datetime.now(), message=_error_summary(exc), **counts)
            self.events.emit(run_id, "error", _error_summary(exc), **counts)
        finally:
            if kms:
                kms.close()
            self._release(run_id)

    def update_articles(self, article_ids: list[str], dry_run: bool) -> str:
        """将历史抓取的纯正文安全覆盖更新到 KMS。"""
        if not self.settings.enable_content_update:
            raise ValueError("正文更新功能当前已关闭")
        with self._guard:
            if self._active_run:
                raise RunConflictError(self._active_run)
            run_id = uuid.uuid4().hex
            self._active_run = run_id
            self._stop_flags[run_id] = threading.Event()
        try:
            self.db.create_run(run_id, "manual-update", ["content-update-selected"], dry_run)
        except Exception:
            self._release(run_id)
            raise
        threading.Thread(target=self._execute_content_update, args=(run_id, article_ids, dry_run), daemon=True, name=f"update-{run_id[:8]}").start()
        return run_id

    def _execute_content_update(self, run_id: str, article_ids: list[str], dry_run: bool) -> None:
        counts = {"total": 0, "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0}
        stop_flag = self._stop_flags[run_id]
        self.db.update_run(run_id, status="running", started_at=datetime.now(), message="正文覆盖更新运行中")
        self.events.emit(run_id, "status", f"开始正文覆盖更新（选中 {len(article_ids)} 条）", dry_run=dry_run)
        kms: KmsClient | None = None
        try:
            rows = self.db.get_articles_by_ids(list(dict.fromkeys(article_ids)))
            rows = sorted(rows, key=lambda row: row.get("crawled_at") or datetime.min)
            counts["total"] = len(rows)
            self.db.update_run(run_id, total=counts["total"])
            if not dry_run:
                kms = KmsClient(self.settings)
            for row in rows:
                if stop_flag.is_set():
                    break
                started = time.monotonic()
                article_id = row["policy_crawler_article_id"]
                item_id = uuid.uuid4().hex
                self.db.create_run_item(
                    run_item_id=item_id, run_id=run_id, task_code="content_update", source_code=row["source_code"],
                    source_item_id=row["source_item_id"], policy_crawler_article_id=article_id,
                    kms_document_id=row["kms_document_id"], status="running", phase="content_update", message="校验 KMS 文档来源",
                )
                try:
                    if row.get("content_update_status") not in {"pending", "failed"}:
                        message = "该记录不在待更新/更新失败状态，已跳过"
                        self.db.update_run_item(item_id, phase="content_update", status="skipped", message=message, finished_at=datetime.now())
                        outcome = "skipped"
                    else:
                        target = kms_kb.resolve_document_for_content_update(
                            self.settings, document_id=row["kms_document_id"], base_id=row["base_id"],
                            title=row["title"], original_url=row["original_url"],
                        )
                        if not target:
                            message = "KMS 未找到唯一且来源链接一致的文档，未执行覆盖更新"
                            if not dry_run:
                                self.db.mark_article_content_update_unmatched(article_id, message)
                            self.db.update_run_item(item_id, phase="content_update", status="skipped", message=message, finished_at=datetime.now())
                            outcome = "skipped"
                        else:
                            content = row["content_html"]
                            if dry_run:
                                message = "预检通过：KMS 文档来源匹配，正式执行将覆盖正文并重新处理"
                                self.db.update_run_item(item_id, kms_document_id=target["kms_hub_document_id"], phase="validate", status="dry_run", message=message, duration_ms=int((time.monotonic()-started)*1000), finished_at=datetime.now())
                                outcome = "succeeded"
                            else:
                                publish_date = row.get("publish_date")
                                result = kms.update_content(
                                    target["kms_hub_document_id"], content,
                                    publish_date=publish_date.isoformat() if publish_date else None,
                                )
                                if result.success:
                                    payload = json.loads(row["kms_payload_json"])
                                    payload["content"] = content
                                    self.db.update_article_content_snapshot(article_id, content, json.dumps(payload, ensure_ascii=False))
                                else:
                                    self.db.mark_article_content_update_failed(article_id, result.message)
                                self.db.update_run_item(
                                    item_id, kms_document_id=target["kms_hub_document_id"], phase="content_update",
                                    status="success" if result.success else "failed", kms_result_code=result.code,
                                    retry_count=result.attempts, message=result.message,
                                    duration_ms=int((time.monotonic()-started)*1000), finished_at=datetime.now(),
                                )
                                outcome = "succeeded" if result.success else "failed"
                except Exception as exc:
                    logger.exception("content update run_id=%s article_id=%s failed", run_id, article_id)
                    message = _error_summary(exc)
                    if not dry_run:
                        self.db.mark_article_content_update_failed(article_id, message)
                    self.db.update_run_item(item_id, phase="content_update", status="failed", message=message, error_detail=message, duration_ms=int((time.monotonic()-started)*1000), finished_at=datetime.now())
                    outcome = "failed"
                counts["processed"] += 1
                counts[outcome] += 1
                self.db.update_run(run_id, **counts, current_item=row["source_item_id"])
                self.events.emit(run_id, "progress", row["title"], **counts)
                if self.settings.item_delay_seconds:
                    time.sleep(self.settings.item_delay_seconds)
            status = "stopped" if stop_flag.is_set() else "completed"
            message = "正文覆盖更新已停止" if status == "stopped" else ("正文更新预检完成" if dry_run else "正文覆盖更新完成")
            self.db.update_run(run_id, status=status, finished_at=datetime.now(), message=message, **counts)
            self.events.emit(run_id, "complete", message, status=status, **counts)
        except Exception as exc:
            logger.exception("content update run_id=%s failed", run_id)
            self.db.update_run(run_id, status="failed", finished_at=datetime.now(), message=_error_summary(exc), **counts)
            self.events.emit(run_id, "error", _error_summary(exc), **counts)
        finally:
            if kms:
                kms.close()
            self._release(run_id)

    def _execute_retry(self, run_id: str, original_run_id: str) -> None:
        rows = self.db.failed_payloads_for_run(original_run_id)
        counts = {"total": len(rows), "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0}
        flag = self._stop_flags[run_id]
        self.db.update_run(run_id, status="running", started_at=datetime.now(), **counts)
        self.events.emit(run_id, "status", f"开始重推 {len(rows)} 条失败 payload")
        kms: KmsClient | None = None
        try:
            kms = KmsClient(self.settings)
            for row in rows:
                if flag.is_set():
                    break
                item_id = uuid.uuid4().hex
                article_id = row["policy_crawler_article_id"]
                self.db.create_run_item(
                    run_item_id=item_id, run_id=run_id, task_code="retry_failed", source_code=row["source_code"],
                    source_item_id=row["source_item_id"], policy_crawler_article_id=article_id,
                    kms_document_id=row["kms_document_id"], status="running", phase="kms", message="从数据库快照重推",
                )
                result = kms.push(CrawlerPayload.model_validate(row["kms_payload"]))
                self.db.update_article_kms(article_id, result)
                self.db.update_run_item(item_id, status="success" if result.success else "failed", kms_result_code=result.code, retry_count=result.attempts, message=result.message, finished_at=datetime.now())
                counts["processed"] += 1
                counts["succeeded" if result.success else "failed"] += 1
                self.db.update_run(run_id, **counts, current_item=row["source_item_id"])
                self.events.emit(run_id, "progress", row["title"], **counts)
                logger.info("run_id=%s task_code=retry_failed source_item_id=%s kms_document_id=%s base_id=%s result_code=%s", run_id, row["source_item_id"], row["kms_document_id"], row["base_id"], result.code)
            status = "stopped" if flag.is_set() else "completed"
            self.db.update_run(run_id, status=status, finished_at=datetime.now(), **counts)
            self.events.emit(run_id, "complete", "失败记录重推结束", status=status, **counts)
        except Exception as exc:
            logger.exception("retry run_id=%s failed", run_id)
            self.db.update_run(run_id, status="failed", finished_at=datetime.now(), message=_error_summary(exc), **counts)
            self.events.emit(run_id, "error", _error_summary(exc), **counts)
        finally:
            if kms:
                kms.close()
            self._release(run_id)
