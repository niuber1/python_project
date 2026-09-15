from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import crawler_tool.app as app_module
from crawler_tool.models import KmsAuthConfigRequest, UpdateArticlesRequest
from crawler_tool.models import UrlRunRequest


def test_finish_interrupted_runs_marks_only_incomplete_runs(monkeypatch):
    from crawler_tool.database import Database
    from crawler_tool.config import Settings

    calls = []

    class Cursor:
        rowcount = 3
        def execute(self, sql): calls.append(sql)
        def __enter__(self): return self
        def __exit__(self, *_): pass

    class Connection:
        def cursor(self): return Cursor()
        def commit(self): calls.append("commit")

    database = Database(Settings(_env_file=None))

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(database, "connection", connection)
    assert database.finish_interrupted_runs() == 3
    assert "status IN ('queued', 'running')" in calls[0]
    assert "status='stopped'" in calls[0]
    assert "kms_status='processing'" in calls[1]


def test_claim_articles_for_push_atomically_marks_rows_processing(monkeypatch):
    from crawler_tool.database import Database
    from crawler_tool.config import Settings

    calls = []

    class Cursor:
        rowcount = 0
        def execute(self, sql, params=None):
            calls.append((sql, params))
            if "SET kms_status='processing'" in sql:
                self.rowcount = 2
        def fetchall(self):
            return [
                {"policy_crawler_article_id": "a1", "kms_status": "pending"},
                {"policy_crawler_article_id": "a2", "kms_status": "failed"},
            ]
        def __enter__(self): return self
        def __exit__(self, *_): pass

    class Connection:
        def cursor(self): return Cursor()
        def commit(self): calls.append(("commit", None))
        def rollback(self): calls.append(("rollback", None))

    database = Database(Settings(_env_file=None))

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(database, "connection", connection)
    previous = database.claim_articles_for_push(["a1", "a2"])

    assert previous == {"a1": "pending", "a2": "failed"}
    assert "FOR UPDATE" in calls[0][0]
    assert "kms_status='processing'" in calls[1][0]
    assert calls[-1][0] == "commit"


def test_claim_articles_for_push_rejects_already_processing_row(monkeypatch):
    from crawler_tool.database import Database
    from crawler_tool.config import Settings

    calls = []

    class Cursor:
        rowcount = 0
        def execute(self, sql, params=None): calls.append((sql, params))
        def fetchall(self): return [{"policy_crawler_article_id": "a1", "kms_status": "processing"}]
        def __enter__(self): return self
        def __exit__(self, *_): pass

    class Connection:
        def cursor(self): return Cursor()
        def commit(self): calls.append(("commit", None))
        def rollback(self): calls.append(("rollback", None))

    database = Database(Settings(_env_file=None))

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(database, "connection", connection)
    with pytest.raises(ValueError, match="不能重复提交"):
        database.claim_articles_for_push(["a1"])

    assert calls[-1][0] == "rollback"


def test_content_update_api_is_unavailable_when_feature_is_disabled(monkeypatch):
    monkeypatch.setattr(app_module.settings, "enable_content_update", False)
    assert app_module.get_features() == {"content_update_enabled": False}

    with pytest.raises(HTTPException) as auth_error:
        app_module.set_kms_auth(KmsAuthConfigRequest(access_token="token", authorization="authorization"))
    assert auth_error.value.status_code == 404

    with pytest.raises(HTTPException) as update_error:
        app_module.update_articles(UpdateArticlesRequest(article_ids=["article"], confirm_write=True))
    assert update_error.value.status_code == 404


def test_url_run_api_queues_local_only_crawl(monkeypatch):
    received = {}
    monkeypatch.setattr(app_module.manager, "start_url_run", lambda urls, dry_run: received.update(urls=urls, dry_run=dry_run) or "url-run")
    result = app_module.start_url_run(UrlRunRequest(urls=["https://example.com/?policyId=1"], confirm_write=True))
    assert result == {"run_id": "url-run", "status": "queued"}
    assert received == {"urls": ["https://example.com/?policyId=1"], "dry_run": False}


def test_recrawl_all_articles_queues_every_active_failure(monkeypatch):
    received = {}
    monkeypatch.setattr(
        app_module.database, "active_classification_failure_count", lambda: 8143
    )
    monkeypatch.setattr(
        app_module.manager,
        "recrawl_classification_failures",
        lambda failure_ids: received.update({"failure_ids": failure_ids}) or "retry-all-run",
    )

    result = app_module.recrawl_all_articles()

    assert result == {"run_id": "retry-all-run", "status": "queued", "total": 8143}
    assert received == {"failure_ids": None}


def test_recrawl_all_articles_rejects_empty_ledger(monkeypatch):
    monkeypatch.setattr(app_module.database, "active_classification_failure_count", lambda: 0)

    with pytest.raises(HTTPException) as error:
        app_module.recrawl_all_articles()

    assert error.value.status_code == 400
