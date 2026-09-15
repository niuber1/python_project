from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

from .config import NON_DECLARE_BASE_ID, TARGET_BASE_ID, Settings, policy_type_for_base, policy_type_name
from .html_utils import content_sha256
from .models import CrawlerPayload, KmsResult, PolicyArticle


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connection(self) -> Iterator[pymysql.Connection]:
        connection = pymysql.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password,
            database=self.settings.db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
        )
        try:
            yield connection
        finally:
            connection.close()

    def health(self) -> dict[str, Any]:
        try:
            with self.connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name LIKE 'policy_crawler_%%'",
                    (self.settings.db_name,),
                )
                count = cursor.fetchone()["cnt"]
            return {"ok": count >= 6, "tables": count, "message": "ok" if count >= 6 else "请先执行 sql/001_init.sql 至 sql/004_run_observability.sql"}
        except Exception as exc:
            return {"ok": False, "tables": 0, "message": str(exc)}

    def create_run(self, run_id: str, trigger_type: str, task_codes: list[str], dry_run: bool) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO policy_crawler_run "
                "(run_id, trigger_type, task_codes_json, dry_run, status, created_at, heartbeat_at) "
                "VALUES (%s,%s,%s,%s,'queued',NOW(),NOW())",
                (run_id, trigger_type, _json(task_codes), int(dry_run)),
            )
            conn.commit()

    def update_run(self, run_id: str, **values: Any) -> None:
        allowed = {
            "status", "total", "processed", "succeeded", "skipped",
            "failed", "current_item", "message", "started_at", "finished_at", "heartbeat_at",
        }
        data = {key: value for key, value in values.items() if key in allowed}
        data.setdefault("heartbeat_at", datetime.now())
        if not data:
            return
        assignments = ",".join(f"{key}=%s" for key in data)
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"UPDATE policy_crawler_run SET {assignments} WHERE run_id=%s", (*data.values(), run_id))
            conn.commit()

    def finish_interrupted_runs(self) -> int:
        """服务进程重启后，收束不会再有后台线程继续处理的旧批次。"""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE policy_crawler_run "
                "SET status='stopped', finished_at=NOW(), "
                "message='服务重启，任务已中断；请重新发起任务' "
                "WHERE status IN ('queued', 'running')"
            )
            affected = cursor.rowcount
            cursor.execute(
                "UPDATE policy_crawler_article SET kms_status='failed',kms_result_code='interrupted',"
                "last_error='服务重启，入库任务已中断；可重新勾选入库',updated_at=NOW() "
                "WHERE kms_status='processing'"
            )
            conn.commit()
        return affected

    def finish_stale_runs(self, stale_minutes: int) -> int:
        """收束没有活动线程且长期未更新心跳的遗留批次。"""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE policy_crawler_run "
                "SET status='stopped', finished_at=NOW(), heartbeat_at=NOW(), "
                "message=CONCAT('任务心跳超过 ', %s, ' 分钟未更新，已自动标记中断；请重新发起任务') "
                "WHERE status IN ('queued', 'running') "
                "AND COALESCE(heartbeat_at, created_at) < DATE_SUB(NOW(), INTERVAL %s MINUTE)",
                (stale_minutes, stale_minutes),
            )
            affected = cursor.rowcount
            conn.commit()
        return affected

    def append_run_event(
        self, run_id: str, event_type: str, message: str, data: dict[str, Any], created_at: datetime
    ) -> int:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO policy_crawler_run_event "
                "(run_id,event_type,message,data_json,created_at) VALUES (%s,%s,%s,%s,%s)",
                (run_id, event_type, message, _json(data), created_at),
            )
            event_id = int(cursor.lastrowid)
            conn.commit()
        return event_id

    def list_run_events(self, run_id: str, after_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT run_event_id,event_type,message,data_json,created_at "
                "FROM policy_crawler_run_event WHERE run_id=%s AND run_event_id>%s "
                "ORDER BY run_event_id ASC LIMIT %s",
                (run_id, after_id, limit),
            )
            rows = cursor.fetchall()
        return [self._event_row(row) for row in rows]

    def recent_run_events(self, run_id: str, limit: int = 300) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT run_event_id,event_type,message,data_json,created_at "
                "FROM policy_crawler_run_event WHERE run_id=%s "
                "ORDER BY run_event_id DESC LIMIT %s",
                (run_id, limit),
            )
            rows = cursor.fetchall()
        return [self._event_row(row) for row in reversed(rows)]

    @staticmethod
    def _event_row(row: dict[str, Any]) -> dict[str, Any]:
        try:
            data = json.loads(row.get("data_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            data = {}
        return {
            "id": int(row["run_event_id"]), "type": row["event_type"],
            "message": row["message"], "time": row["created_at"].isoformat(timespec="seconds"), **data,
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT * FROM policy_crawler_run WHERE run_id=%s", (run_id,))
            row = cursor.fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT * FROM policy_crawler_run ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
        return [self._run_row(row) for row in rows]

    def get_schedule_config(self) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT enabled, schedule_hour, schedule_minute, task_codes_json, updated_at FROM policy_crawler_schedule_config WHERE config_id=1")
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "enabled": bool(row["enabled"]), "hour": int(row["schedule_hour"]),
            "minute": int(row["schedule_minute"]), "task_codes": json.loads(row["task_codes_json"] or "[]"),
            "updated_at": row["updated_at"],
        }

    def save_schedule_config(self, enabled: bool, hour: int, minute: int, task_codes: list[str]) -> dict[str, Any]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO policy_crawler_schedule_config (config_id,enabled,schedule_hour,schedule_minute,task_codes_json,updated_at) "
                "VALUES (1,%s,%s,%s,%s,NOW()) ON DUPLICATE KEY UPDATE enabled=VALUES(enabled),schedule_hour=VALUES(schedule_hour),schedule_minute=VALUES(schedule_minute),task_codes_json=VALUES(task_codes_json),updated_at=NOW()",
                (int(enabled), hour, minute, _json(task_codes)),
            )
            conn.commit()
        return self.get_schedule_config() or {"enabled": enabled, "hour": hour, "minute": minute, "task_codes": task_codes}

    @staticmethod
    def _run_row(row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["task_codes"] = json.loads(row.pop("task_codes_json") or "[]")
        row["dry_run"] = bool(row["dry_run"])
        return row

    def find_existing(self, source_code: str, source_ids: list[str], base_id: str) -> dict[str, dict[str, Any]]:
        if not source_ids:
            return {}
        result: dict[str, dict[str, Any]] = {}
        with self.connection() as conn, conn.cursor() as cursor:
            for start in range(0, len(source_ids), 500):
                chunk = source_ids[start:start + 500]
                marks = ",".join(["%s"] * len(chunk))
                cursor.execute(
                    f"SELECT * FROM policy_crawler_article WHERE source_code=%s AND base_id=%s "
                    f"AND source_item_id IN ({marks})",
                    (source_code, base_id, *chunk),
                )
                result.update({row["source_item_id"]: row for row in cursor.fetchall()})
        return result

    def find_existing_by_title(self, title: str, base_id: str) -> bool:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM policy_crawler_article WHERE title=%s AND base_id=%s LIMIT 1", (title, base_id))
            return cursor.fetchone() is not None

    def find_existing_any_base(self, source_code: str, source_item_id: str) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT * FROM policy_crawler_article WHERE source_code=%s AND source_item_id=%s LIMIT 1", (source_code, source_item_id))
            return cursor.fetchone()

    def existing_source_ids(self, source_code: str) -> set[str]:
        """返回指定来源已经写入普通政策台账的稳定来源 ID。"""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT source_item_id FROM policy_crawler_article WHERE source_code=%s",
                (source_code,),
            )
            return {
                str(row["source_item_id"]).strip()
                for row in cursor.fetchall()
                if row.get("source_item_id") and str(row["source_item_id"]).strip()
            }

    def article_titles_in_bases(self, base_ids: list[str]) -> set[str]:
        """一次性加载候选知识库在本地普通政策台账中的标题。"""
        values = list(dict.fromkeys(base_id for base_id in base_ids if base_id))
        if not values:
            return set()
        marks = ",".join(["%s"] * len(values))
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT title FROM policy_crawler_article WHERE base_id IN ({marks}) AND title IS NOT NULL",
                values,
            )
            return {
                str(row["title"]).strip()
                for row in cursor.fetchall()
                if row.get("title") and str(row["title"]).strip()
            }

    def save_classification_failure(self, article: PolicyArticle, candidate: dict[str, Any], message: str) -> str:
        failure_id = str(uuid.uuid4())
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO policy_crawler_classification_failure (policy_crawler_classification_failure_id,source_code,source_item_id,source_name,title,project_name,policy_level,publish_dept,document_no,publish_date,original_url,candidate_json,last_error,retry_count,failure_status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,'active',NOW(),NOW()) "
                "ON DUPLICATE KEY UPDATE source_name=VALUES(source_name),title=VALUES(title),project_name=VALUES(project_name),policy_level=VALUES(policy_level),publish_dept=VALUES(publish_dept),document_no=VALUES(document_no),publish_date=VALUES(publish_date),original_url=VALUES(original_url),candidate_json=VALUES(candidate_json),last_error=VALUES(last_error),retry_count=retry_count+1,failure_status='active',resolved_at=NULL,updated_at=NOW()",
                (failure_id, article.source_code, article.source_item_id, article.source_name, article.title, article.project_name,
                 article.policy_level, article.publish_dept, article.document_no, article.publish_date, article.original_url,
                 _json(candidate), message),
            )
            cursor.execute("SELECT policy_crawler_classification_failure_id FROM policy_crawler_classification_failure WHERE source_code=%s AND source_item_id=%s", (article.source_code, article.source_item_id))
            row = cursor.fetchone()
            conn.commit()
            return row["policy_crawler_classification_failure_id"]

    def active_classification_failures(
        self, failure_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """查询仍待处理的智能体分类失败记录。

        failure_ids 为 None 时返回全部活动失败记录，供批量重新抓取使用。
        """
        if failure_ids == []:
            return []
        where = "WHERE failure_status='active'"
        params: list[str] = []
        if failure_ids is not None:
            marks = ",".join(["%s"] * len(failure_ids))
            where += f" AND policy_crawler_classification_failure_id IN ({marks})"
            params = failure_ids
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM policy_crawler_classification_failure {where}", params
            )
            return cursor.fetchall()

    def active_classification_failure_count(self) -> int:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM policy_crawler_classification_failure "
                "WHERE failure_status='active'"
            )
            row = cursor.fetchone()
            return int(row["count"] if row else 0)

    def resolve_classification_failure(self, failure_id: str, message: str) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("UPDATE policy_crawler_classification_failure SET failure_status='resolved',last_error=%s,resolved_at=NOW(),updated_at=NOW() WHERE policy_crawler_classification_failure_id=%s", (message, failure_id))
            conn.commit()

    def list_run_article_ids(self, run_id: str) -> list[str]:
        """返回某批次中本次新抓取入库（phase=store 且 success）的文章ID，供自动同步使用。"""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT policy_crawler_article_id FROM policy_crawler_run_item "
                "WHERE run_id=%s AND phase='store' AND status='success' AND policy_crawler_article_id IS NOT NULL",
                (run_id,),
            )
            return [row["policy_crawler_article_id"] for row in cursor.fetchall()]

    def insert_article(
        self,
        article_id: str,
        article: PolicyArticle,
        content_html: str,
        payload: CrawlerPayload,
        content_hash: str,
    ) -> bool:
        sql = """
            INSERT INTO policy_crawler_article (
              policy_crawler_article_id,source_code,source_item_id,source_name,base_id,kms_document_id,title,project_name,
              policy_level,publish_dept,document_no,publish_date,apply_start,apply_end,content_html,
              original_url,attachments_json,raw_json,content_hash,kms_payload_json,crawl_status,kms_status,
              content_update_status,
              created_at,updated_at
            ) VALUES (
              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'success','pending','not_needed',NOW(),NOW()
            )
        """
        values = (
            article_id, article.source_code, article.source_item_id, article.source_name, payload.base_id, payload.id,
            article.title, article.project_name, article.policy_level, article.publish_dept,
            article.document_no, article.publish_date, article.apply_start, article.apply_end,
            content_html, article.original_url, _json([a.model_dump() for a in article.attachments]),
            _json(article.raw), content_hash, _json(payload.to_kms_json()),
        )
        try:
            with self.connection() as conn, conn.cursor() as cursor:
                cursor.execute(sql, values)
                conn.commit()
            return True
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == 1062:
                return False
            raise

    def update_article_kms(self, article_id: str, result: KmsResult) -> None:
        status = "success" if result.success else "failed"
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE policy_crawler_article SET kms_status=%s,kms_result_code=%s,retry_count=retry_count+%s,"
                "last_error=%s,pushed_at=IF(%s='success',NOW(),pushed_at),updated_at=NOW() WHERE policy_crawler_article_id=%s",
                (status, result.code, result.attempts, None if result.success else result.message, status, article_id),
            )
            conn.commit()

    def claim_articles_for_push(self, article_ids: list[str]) -> dict[str, str]:
        """原子占用待入库/同步失败记录，返回占用前状态，阻止重复提交。"""
        article_ids = list(dict.fromkeys(article_ids))
        if not article_ids:
            return {}
        marks = ",".join(["%s"] * len(article_ids))
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"SELECT policy_crawler_article_id,kms_status FROM policy_crawler_article "
                f"WHERE policy_crawler_article_id IN ({marks}) FOR UPDATE",
                article_ids,
            )
            rows = cursor.fetchall()
            found = {row["policy_crawler_article_id"]: row["kms_status"] for row in rows}
            unavailable = [article_id for article_id in article_ids if found.get(article_id) not in {"pending", "failed"}]
            if unavailable:
                conn.rollback()
                raise ValueError(f"以下记录已在入库中或已完成，不能重复提交: {', '.join(unavailable[:5])}")
            cursor.execute(
                "UPDATE policy_crawler_article SET kms_status='processing',kms_result_code=NULL,"
                "last_error=NULL,updated_at=NOW() "
                f"WHERE policy_crawler_article_id IN ({marks}) AND kms_status IN ('pending','failed')",
                article_ids,
            )
            if cursor.rowcount != len(article_ids):
                conn.rollback()
                raise ValueError("部分记录状态已变化，请刷新页面后重新选择")
            conn.commit()
            return found

    def release_articles_from_processing(self, previous_statuses: dict[str, str], message: str | None = None) -> int:
        """停止或异常退出时，仅释放仍处于入库中的本批记录。"""
        changed = 0
        if not previous_statuses:
            return changed
        with self.connection() as conn, conn.cursor() as cursor:
            for previous_status in {"pending", "failed"}:
                ids = [article_id for article_id, status in previous_statuses.items() if status == previous_status]
                if not ids:
                    continue
                marks = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"UPDATE policy_crawler_article SET kms_status=%s,last_error=COALESCE(%s,last_error),updated_at=NOW() "
                    f"WHERE kms_status='processing' AND policy_crawler_article_id IN ({marks})",
                    (previous_status, message, *ids),
                )
                changed += cursor.rowcount
            conn.commit()
        return changed

    def refresh_existing_article(
        self, article_id: str, article: PolicyArticle, content_html: str,
        payload: CrawlerPayload, content_hash: str, mark_for_update: bool,
    ) -> None:
        """保存重新抓取到的新快照；KMS 已同步文档只进入待更新，不自动覆盖。"""
        update_status = "pending" if mark_for_update else "not_needed"
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE policy_crawler_article SET source_name=%s,title=%s,project_name=%s,policy_level=%s,"
                "publish_dept=%s,document_no=%s,publish_date=%s,apply_start=%s,apply_end=%s,content_html=%s,"
                "original_url=%s,attachments_json=%s,raw_json=%s,content_hash=%s,kms_payload_json=%s,"
                "content_update_status=%s,content_update_error=NULL,content_updated_at=NULL,crawled_at=NOW(),updated_at=NOW() "
                "WHERE policy_crawler_article_id=%s",
                (
                    article.source_name, article.title, article.project_name, article.policy_level, article.publish_dept,
                    article.document_no, article.publish_date, article.apply_start, article.apply_end, content_html,
                    article.original_url, _json([item.model_dump() for item in article.attachments]), _json(article.raw),
                    content_hash, _json(payload.to_kms_json()), update_status, article_id,
                ),
            )
            conn.commit()

    def create_run_item(self, **values: Any) -> str:
        item_id = values.pop("run_item_id")
        columns = ["run_item_id", *values.keys(), "created_at"]
        placeholders = ["%s"] * (len(values) + 1) + ["NOW()"]
        sql = f"INSERT INTO policy_crawler_run_item ({','.join(columns)}) VALUES ({','.join(placeholders)})"
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (item_id, *values.values()))
            conn.commit()
        return item_id

    def create_skipped_run_items(self, rows: list[dict[str, Any]]) -> None:
        """批量保存列表阶段提前去重的批次明细，避免逐条建立数据库连接。"""
        if not rows:
            return
        sql = (
            "INSERT INTO policy_crawler_run_item "
            "(run_item_id,run_id,task_code,source_code,source_item_id,status,phase,message,finished_at,created_at) "
            "VALUES (%s,%s,%s,%s,%s,'skipped','deduplicate',%s,%s,NOW())"
        )
        values = [
            (
                row["run_item_id"], row["run_id"], row["task_code"], row["source_code"],
                row["source_item_id"], row["message"], row["finished_at"],
            )
            for row in rows
        ]
        with self.connection() as conn, conn.cursor() as cursor:
            for start in range(0, len(values), 500):
                cursor.executemany(sql, values[start:start + 500])
            conn.commit()

    def update_run_item(self, item_id: str, **values: Any) -> None:
        allowed = {"policy_crawler_article_id", "kms_document_id", "phase", "status", "duration_ms", "retry_count", "kms_result_code", "message", "error_detail", "finished_at"}
        data = {key: value for key, value in values.items() if key in allowed}
        if not data:
            return
        assignments = ",".join(f"{key}=%s" for key in data)
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE policy_crawler_run_item SET {assignments} WHERE run_item_id=%s",
                (*data.values(), item_id),
            )
            conn.commit()

    def list_run_items(self, run_id: str, page: int, size: int, status: str | None = None) -> dict[str, Any]:
        where = "i.run_id=%s"
        params: list[Any] = [run_id]
        if status:
            where += " AND i.status=%s"
            params.append(status)
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS cnt FROM policy_crawler_run_item i WHERE {where}", params)
            total = cursor.fetchone()["cnt"]
            cursor.execute(
                f"SELECT i.*, a.title AS article_title FROM policy_crawler_run_item i "
                f"LEFT JOIN policy_crawler_article a ON a.policy_crawler_article_id = i.policy_crawler_article_id "
                f"WHERE {where} ORDER BY i.created_at DESC LIMIT %s OFFSET %s",
                (*params, size, (page - 1) * size),
            )
            items = cursor.fetchall()
        return {"total": total, "page": page, "size": size, "items": items}

    def failed_payloads_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT a.* FROM policy_crawler_article a JOIN policy_crawler_run_item i "
                "ON i.policy_crawler_article_id=a.policy_crawler_article_id WHERE i.run_id=%s AND a.kms_status='failed'",
                (run_id,),
            )
            rows = cursor.fetchall()
        for row in rows:
            row["kms_payload"] = json.loads(row["kms_payload_json"])
        return rows

    def count_pending(self, source_codes: list[str] | None = None) -> int:
        with self.connection() as conn, conn.cursor() as cursor:
            if source_codes:
                marks = ",".join(["%s"] * len(source_codes))
                cursor.execute(
                    f"SELECT COUNT(*) AS cnt FROM policy_crawler_article WHERE kms_status='pending' AND source_code IN ({marks})",
                    tuple(source_codes),
                )
            else:
                cursor.execute("SELECT COUNT(*) AS cnt FROM policy_crawler_article WHERE kms_status='pending'")
            return int(cursor.fetchone()["cnt"])

    def list_pending_articles(self, source_codes: list[str] | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cursor:
            if source_codes:
                marks = ",".join(["%s"] * len(source_codes))
                cursor.execute(
                    f"SELECT * FROM policy_crawler_article WHERE kms_status='pending' AND source_code IN ({marks}) ORDER BY updated_at",
                    tuple(source_codes),
                )
            else:
                cursor.execute("SELECT * FROM policy_crawler_article WHERE kms_status='pending' ORDER BY updated_at")
            return cursor.fetchall()

    def list_articles(
        self,
        page: int,
        size: int,
        status: str | None = None,
        source_code: str | None = None,
        keyword: str | None = None,
        update_status: str | None = None,
        policy_type: str | None = None,
        policy_level: str | None = None,
        crawl_status: str | None = None,
    ) -> dict[str, Any]:
        if crawl_status == "classification_failed":
            return self._list_classification_failures(page, size, source_code, keyword, policy_level)
        where, params = [], []
        if status == "pending_or_processing":
            where.append("kms_status IN ('pending','processing')")
        elif status:
            where.append("kms_status=%s")
            params.append(status)
        if source_code:
            where.append("source_code=%s")
            params.append(source_code)
        if keyword:
            where.append("title LIKE %s")
            params.append(f"%{keyword}%")
        if update_status:
            where.append("content_update_status=%s")
            params.append(update_status)
        type_base_ids = {"declare": TARGET_BASE_ID, "non_declare": NON_DECLARE_BASE_ID}
        if policy_type in type_base_ids:
            where.append("base_id=%s")
            params.append(type_base_ids[policy_type])
        if policy_level:
            where.append("policy_level=%s")
            params.append(policy_level)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS cnt FROM policy_crawler_article {clause}", params)
            total = int(cursor.fetchone()["cnt"])
            cursor.execute(
                f"SELECT policy_crawler_article_id, source_code, source_name, source_item_id, base_id, title, project_name, "
                f"crawl_status, kms_status, kms_result_code, last_error, content_update_status, content_update_error, content_updated_at, policy_level, "
                f"crawled_at, pushed_at, publish_date, apply_start, apply_end "
                f"FROM policy_crawler_article {clause} ORDER BY crawled_at DESC, created_at DESC LIMIT %s OFFSET %s",
                (*params, size, (page - 1) * size),
            )
            items = cursor.fetchall()
        for item in items:
            item["policy_type"] = policy_type_for_base(item["base_id"])
            item["policy_type_name"] = policy_type_name(item["policy_type"])
            item["record_type"] = "article"
        return {"total": total, "page": page, "size": size, "items": items}

    def _list_classification_failures(self, page: int, size: int, source_code: str | None, keyword: str | None, policy_level: str | None) -> dict[str, Any]:
        where, params = ["failure_status='active'"], []
        if source_code:
            where.append("source_code=%s")
            params.append(source_code)
        if keyword:
            where.append("title LIKE %s")
            params.append(f"%{keyword}%")
        if policy_level:
            where.append("policy_level=%s")
            params.append(policy_level)
        clause = "WHERE " + " AND ".join(where)
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS cnt FROM policy_crawler_classification_failure {clause}", params)
            total = int(cursor.fetchone()["cnt"])
            cursor.execute(
                f"SELECT policy_crawler_classification_failure_id,source_code,source_name,source_item_id,title,project_name,policy_level,publish_date,last_error,retry_count,created_at,updated_at FROM policy_crawler_classification_failure {clause} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (*params, size, (page - 1) * size),
            )
            items = cursor.fetchall()
        for item in items:
            item.update({"record_type": "classification_failure", "record_id": item["policy_crawler_classification_failure_id"], "policy_type": "pending_classification", "policy_type_name": "待分类", "crawl_status": "classification_failed", "kms_status": None, "pushed_at": None, "crawled_at": item["updated_at"], "apply_start": None, "apply_end": None})
        return {"total": total, "page": page, "size": size, "items": items}

    def get_articles_by_ids(self, article_ids: list[str]) -> list[dict[str, Any]]:
        if not article_ids:
            return []
        marks = ",".join(["%s"] * len(article_ids))
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM policy_crawler_article WHERE policy_crawler_article_id IN ({marks})", article_ids)
            return cursor.fetchall()

    def article_status_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "processing": 0, "success": 0, "failed": 0, "classification_failed": 0}
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT kms_status, COUNT(*) AS cnt FROM policy_crawler_article GROUP BY kms_status")
            for row in cursor.fetchall():
                status = row["kms_status"]
                if status in counts:
                    counts[status] = int(row["cnt"])
            cursor.execute("SELECT COUNT(*) AS cnt FROM policy_crawler_classification_failure WHERE failure_status='active'")
            counts["classification_failed"] = int(cursor.fetchone()["cnt"])
        return counts

    def content_update_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "success": 0, "failed": 0, "unmatched": 0, "not_needed": 0}
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT content_update_status, COUNT(*) AS cnt FROM policy_crawler_article GROUP BY content_update_status")
            for row in cursor.fetchall():
                status = row["content_update_status"]
                if status in counts:
                    counts[status] = int(row["cnt"])
        return counts

    def update_article_content_snapshot(self, article_id: str, content_html: str, payload_json: str) -> None:
        """KMS 覆盖更新成功后，回写本地正文快照，避免下一次重复拼接元数据。"""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE policy_crawler_article SET content_html=%s,content_hash=%s,kms_payload_json=%s,"
                "content_update_status='success',content_update_error=NULL,content_updated_at=NOW(),updated_at=NOW() "
                "WHERE policy_crawler_article_id=%s",
                (content_html, content_sha256(content_html), payload_json, article_id),
            )
            conn.commit()

    def mark_article_content_update_failed(self, article_id: str, message: str) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE policy_crawler_article SET content_update_status='failed',content_update_error=%s,updated_at=NOW() "
                "WHERE policy_crawler_article_id=%s",
                (message[:4000], article_id),
            )
            conn.commit()

    def mark_article_content_update_unmatched(self, article_id: str, message: str) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE policy_crawler_article SET content_update_status='unmatched',content_update_error=%s,updated_at=NOW() "
                "WHERE policy_crawler_article_id=%s",
                (message[:4000], article_id),
            )
            conn.commit()

    def mark_articles_reconciled(self, article_ids: list[str]) -> int:
        """将已由 KMS 确认存在的历史待入库记录回写为已同步。"""
        if not article_ids:
            return 0
        changed = 0
        with self.connection() as conn, conn.cursor() as cursor:
            for start in range(0, len(article_ids), 500):
                batch = article_ids[start:start + 500]
                marks = ",".join(["%s"] * len(batch))
                cursor.execute(
                    "UPDATE policy_crawler_article SET kms_status='success',kms_result_code='7',"
                    "last_error=NULL,pushed_at=NOW(),updated_at=NOW() "
                    f"WHERE kms_status='pending' AND policy_crawler_article_id IN ({marks})",
                    batch,
                )
                changed += cursor.rowcount
            conn.commit()
        return changed
