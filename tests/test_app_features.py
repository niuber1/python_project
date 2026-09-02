import pytest
from fastapi import HTTPException

import crawler_tool.app as app_module
from crawler_tool.models import KmsAuthConfigRequest, UpdateArticlesRequest
from crawler_tool.models import UrlRunRequest


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
