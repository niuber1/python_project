import json

import httpx

from crawler_tool.config import Settings
from crawler_tool.kms_client import KmsClient
from crawler_tool.models import CrawlerPayload


def payload():
    return CrawlerPayload(id="b" * 32, bt="标题", url="https://example.com", content="<p>正文</p>", source="来源", baseId="base")


def settings():
    return Settings(_env_file=None, kms_base_url="https://kms.test")


def test_code_1_and_wrapped_code_7_are_success():
    responses = iter([httpx.Response(200, json="1"), httpx.Response(200, json={"data": "7"})])
    client = httpx.Client(transport=httpx.MockTransport(lambda _: next(responses)))
    kms = KmsClient(settings(), client=client, sleeper=lambda _: None)
    assert kms.push(payload()).code == "1"
    result = kms.push(payload())
    assert result.success and result.code == "7"


def test_business_failure_is_not_retried():
    calls = 0
    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": "4"})
    result = KmsClient(settings(), httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None).push(payload())
    assert not result.success and result.code == "4" and calls == 1


def test_code_5_processing_is_success():
    result = KmsClient(settings(), httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": "5"}))), lambda _: None).push(payload())
    assert result.success and result.code == "5"


def test_5xx_retries_three_times():
    calls = 0
    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(503)
    result = KmsClient(settings(), httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None).push(payload())
    assert result.code == "HTTP_503" and result.attempts == 3 and calls == 3


def test_http_error_keeps_short_kms_response_detail():
    result = KmsClient(settings(), httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, text="参数无效"))), lambda _: None).push(payload())
    assert result.message == "KMS HTTP 400: 参数无效"


def test_update_content_posts_to_kms_document_update_endpoint():
    seen = {}
    def handler(request):
        seen["path"] = request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"data": True})
    configured = Settings(_env_file=None, kms_base_url="https://kms.test", kms_access_token="access-value")
    result = KmsClient(configured, httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None).update_content("a" * 32, "<p>正文</p>", publish_date="2023-05-19")
    assert result.success and result.code == "UPDATE_OK"
    assert seen["path"] == "/kms/openapi/knowledge/document/update"
    assert seen["json"]["documentId"] == "a" * 32
    assert seen["json"]["cwrq"] == "2023-05-19" and seen["json"]["permission"] == []
    assert "knowledgeBaseId" not in seen["json"]


def test_update_content_sends_configured_access_token_and_authorization():
    seen = {}
    def handler(request):
        seen["access_token"] = request.headers.get("accessToken")
        seen["authorization"] = request.headers.get("authorization")
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, json={"data": True})
    configured = Settings(_env_file=None, kms_base_url="https://kms.test", kms_access_token="access-value", kms_authorization="authorization-value")
    result = KmsClient(configured, httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None).update_content("a" * 32, "<p>正文</p>")
    assert result.success
    assert seen == {
        "access_token": "access-value",
        "authorization": "authorization-value",
        "cookie": None,
    }


def test_auth_uses_read_only_kms_endpoint_and_does_not_persist_credentials():
    seen = {}
    def handler(request):
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["access_token"] = request.headers.get("accessToken")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])
    result = KmsClient(settings(), httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None).test_auth("access-value", "authorization-value")
    assert result.success and result.code == "AUTH_OK"
    assert seen == {
        "path": "/kms/openapi/knowledge/stat/overview",
        "method": "GET",
        "access_token": "access-value",
        "authorization": "authorization-value",
    }


def test_auth_reports_kms_auth_error():
    response = httpx.Response(500, json={"success": False, "state": 21004, "message": "token不能为空"})
    result = KmsClient(settings(), httpx.Client(transport=httpx.MockTransport(lambda _: response)), lambda _: None).test_auth("bad-access", "bad-authorization")
    assert not result.success and result.code == "HTTP_500"


def test_application_credentials_fetch_cache_and_use_access_token_header():
    seen = {"token_calls": 0, "update_tokens": []}

    def handler(request):
        if request.url.path == "/platform/api/v2/auth/getToken":
            seen["token_calls"] += 1
            body = json.loads(request.content)
            assert body == {"app_key": "app-key", "app_secret": "app-secret"}
            return httpx.Response(200, json={
                "success": True,
                "state": 20000,
                "data": {"access_token": "issued-token", "expires_in": 7200},
            })
        seen["update_tokens"].append(request.headers.get("accessToken"))
        return httpx.Response(200, json={"data": True})

    configured = Settings(
        _env_file=None,
        kms_base_url="https://kms.test",
        kms_application_key="app-key",
        kms_application_secret="app-secret",
    )
    kms = KmsClient(configured, httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None)
    assert kms.update_content("a" * 32, "<p>正文一</p>").success
    assert kms.update_content("b" * 32, "<p>正文二</p>").success
    assert seen == {"token_calls": 1, "update_tokens": ["issued-token", "issued-token"]}


def test_application_token_error_does_not_expose_secret():
    configured = Settings(
        _env_file=None,
        kms_base_url="https://kms.test",
        kms_application_key="app-key",
        kms_application_secret="must-not-leak",
    )
    response = httpx.Response(200, json={"success": False, "state": 40100, "message": "认证失败"})
    result = KmsClient(configured, httpx.Client(transport=httpx.MockTransport(lambda _: response)), lambda _: None).test_auth("", "")
    assert result.code == "AUTH_CONFIG_ERROR"
    assert "must-not-leak" not in result.message
