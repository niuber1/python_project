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
    result = KmsClient(settings(), httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None).update_content("a" * 32, "<p>正文</p>", publish_date="2023-05-19")
    assert result.success and result.code == "UPDATE_OK"
    assert seen["path"] == "/kms/openapi/knowledge/document/update"
    assert seen["json"]["documentId"] == "a" * 32
    assert seen["json"]["cwrq"] == "2023-05-19" and seen["json"]["permission"] == []
    assert "knowledgeBaseId" not in seen["json"]


def test_update_content_sends_configured_authorization_token_and_cookie():
    seen = {}
    def handler(request):
        seen["authorization_token"] = request.headers.get("authorization-token")
        seen["access_token"] = request.headers.get("access_token")
        seen["authorization"] = request.headers.get("authorization")
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, json={"data": True})
    configured = Settings(_env_file=None, kms_base_url="https://kms.test", kms_authorization_token="token-value", kms_cookie="session=value")
    result = KmsClient(configured, httpx.Client(transport=httpx.MockTransport(handler)), lambda _: None).update_content("a" * 32, "<p>正文</p>")
    assert result.success
    assert seen == {"authorization_token": "token-value", "access_token": "token-value", "authorization": "token-value", "cookie": "session=value"}
