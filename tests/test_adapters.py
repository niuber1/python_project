import base64
import json

import httpx
import pytest

from crawler_tool.adapters.base import SourceEmptyError
from crawler_tool.adapters.qifuyun import QifuyunAdapter
from crawler_tool.adapters.suishenban import SuishenbanAdapter


def response(request, payload):
    return httpx.Response(200, json=payload, request=request)


def test_qifuyun_discover_and_fetch():
    def handler(request):
        if request.url.path.endswith("/policy"):
            return response(request, {"data":{"respData":{"total":1,"dataList":[{"id":"q1","name":"项目","applicationStatus":"申报中"}]}}})
        return response(request, {"data":{"respData":{"dataList":[{"name":"原文标题","content":"<p>正文</p>","originalUrl":"https://example.com/original","attachments":[{"fileName":"表格","filePath":"https://example.com/a.xlsx"}]}]}}})
    adapter = QifuyunAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    candidates = adapter.discover()
    article = adapter.fetch(candidates[0])
    assert article.title == "原文标题" and article.attachments[0].name == "表格"


def test_qifuyun_empty_original_is_skipped():
    request = httpx.Request("GET", "https://x")
    adapter = QifuyunAdapter(httpx.Client(transport=httpx.MockTransport(lambda _: response(request, {"data":{"respData":{"dataList":[]}}}))))
    from crawler_tool.models import PolicyCandidate
    with pytest.raises(SourceEmptyError):
        adapter.fetch(PolicyCandidate(source_code="qifuyun", source_item_id="1", project_name="p", detail_ref="1"))


def test_suishenban_filters_and_decodes_base64():
    encoded = base64.b64encode("<p>政策正文</p>".encode()).decode()
    def handler(request):
        if request.url.path.endswith("hqPolicy/projects"):
            body = json.loads(request.content)
            assert body["applyState"] == "1,2" and body["freeEnjoy"] is False
            return response(request, {"data": {"total": 2, "list": [
                {"id": "s1", "name": "保留", "freeEnjoy": False, "applyState": 2},
                {"id": "s2", "name": "排除免申项目", "freeEnjoy": True, "applyState": 2},
            ]}})
        if request.url.path.endswith("questions"):
            return response(request, {"policyProject": {"sourcePolicy": {"id": "p1"}}})
        return response(request, {"policy": {"id": "p1", "name": "政策", "content": encoded, "url": "https://example.com/p", "level": "ZCJB0001005", "publishDepartment": "SHHQGW", "pubDeptName": "上海市经济和信息化委员会"}})
    adapter = SuishenbanAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    candidates = adapter.discover()
    assert [item.source_item_id for item in candidates] == ["s1"]
    article = adapter.fetch(candidates[0])
    assert "政策正文" in article.raw_content_html
    # 层级/发文单位只取展示名：pubDeptName 生效，level 编码不落库
    assert article.publish_dept == "上海市经济和信息化委员会"
    assert article.policy_level is None


def test_suishenban_district_department_is_blanked():
    encoded = base64.b64encode("<p>政策正文</p>".encode()).decode()
    def handler(request):
        if request.url.path.endswith("hqPolicy/projects"):
            return response(request, {"data": {"total": 1, "list": [{"id": "s1", "name": "区级政策", "freeEnjoy": False, "applyState": 2}]}})
        if request.url.path.endswith("questions"):
            return response(request, {"policyProject": {"sourcePolicy": {"id": "p1"}}})
        return response(request, {"policy": {"id": "p1", "name": "区级政策", "content": encoded, "url": "https://example.com/p", "pubDeptName": "闵行区"}})
    adapter = SuishenbanAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    article = adapter.fetch(adapter.discover()[0])
    assert article.publish_dept is None
