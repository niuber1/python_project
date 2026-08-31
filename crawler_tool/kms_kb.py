from __future__ import annotations

import pymysql

from .config import Settings


def resolve_document_for_content_update(
    settings: Settings, *, document_id: str, base_id: str, title: str, original_url: str,
) -> dict[str, str] | None:
    """安全定位可覆盖更新的 KMS 文档。

    历史查重可能仅按标题标记成功，不能据此覆盖。这里要求 KMS 中的来源链接与
    本地记录完全一致；优先使用原 document_id，失败时仅接受唯一的标题+链接匹配。
    """
    if not (base_id and title and original_url):
        return None
    try:
        connection = pymysql.connect(
            host=settings.db_host, port=settings.db_port, user=settings.db_user,
            password=settings.db_password, database=settings.kms_db_name, charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor, connect_timeout=10, read_timeout=15, write_timeout=15,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT kms_hub_document_id,kms_hub_base_id FROM kms_hub_document "
                    "WHERE ds_deleted='0' AND kms_hub_document_id=%s AND kms_hub_base_id=%s AND p_source_url=%s LIMIT 1",
                    (document_id, base_id, original_url),
                )
                row = cursor.fetchone()
                if row:
                    return row
                cursor.execute(
                    "SELECT kms_hub_document_id,kms_hub_base_id FROM kms_hub_document "
                    "WHERE ds_deleted='0' AND g_objectname=%s AND p_source_url=%s LIMIT 2",
                    (title, original_url),
                )
                rows = cursor.fetchall()
                return rows[0] if len(rows) == 1 else None
        finally:
            connection.close()
    except Exception:
        return None


def title_exists_in_base(settings: Settings, title: str, base_id: str) -> bool:
    """检查政策标题是否已存在于 KMS 知识库（kms_kms.kms_hub_document 同库同标题）。

    命中说明该政策已入库（可能是历史推送或同标题不同来源），无需重复调用 crawlerToBase。
    知识库连接异常时返回 False（放行，由 KMS 自身的文档已存在判定兜底），避免阻断正常入库。
    """
    if not title or not base_id:
        return False
    try:
        connection = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            database=settings.kms_db_name,
            charset="utf8mb4",
            connect_timeout=10,
            read_timeout=15,
            write_timeout=15,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM kms_hub_document WHERE g_objectname=%s AND kms_hub_base_id=%s LIMIT 1",
                    (title, base_id),
                )
                return cursor.fetchone() is not None
        finally:
            connection.close()
    except Exception:
        return False


def existing_titles(settings: Settings, titles: list[str]) -> set[str]:
    """返回 KMS 中仍有效的同名文档标题，用于修复本地历史状态。

    KMS 可能在入库时重新选择知识库，因此此处不限制 ``base_id``。调用方只
    将精确标题命中的本地待入库记录标记为已同步，避免把模糊匹配误判为成功。
    """
    unique_titles = list(dict.fromkeys(title for title in titles if title))
    if not unique_titles:
        return set()
    try:
        connection = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            database=settings.kms_db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.Cursor,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=15,
        )
        try:
            found: set[str] = set()
            with connection.cursor() as cursor:
                for start in range(0, len(unique_titles), 500):
                    batch = unique_titles[start:start + 500]
                    marks = ",".join(["%s"] * len(batch))
                    cursor.execute(
                        "SELECT DISTINCT g_objectname FROM kms_hub_document "
                        f"WHERE ds_deleted='0' AND g_objectname IN ({marks})",
                        batch,
                    )
                    found.update(row[0] for row in cursor.fetchall())
            return found
        finally:
            connection.close()
    except Exception:
        # 对账不可用时不改变本地状态，保留给正常入库流程处理。
        return set()
