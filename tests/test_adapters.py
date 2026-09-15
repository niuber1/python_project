import base64
import json

import httpx
import pytest

from crawler_tool.adapters.base import SourceEmptyError
from crawler_tool.adapters.qifuyun import QifuyunAdapter
from crawler_tool.adapters.suishenban import SuishenbanAdapter
from crawler_tool.adapters.shanghai_policy_platform import ShanghaiPolicyPlatformAdapter


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


def test_suishenban_non_declare_uses_free_enjoy_filter():
    def handler(request):
        assert request.url.path.endswith("hqPolicy/projects")
        body = json.loads(request.content)
        assert body["applyState"] == "1,2" and body["freeEnjoy"] is True
        return response(request, {"data": {"total": 2, "list": [
            {"id": "free-1", "name": "保留免申政策", "freeEnjoy": True},
            {"id": "normal-1", "name": "排除申报政策", "freeEnjoy": False},
        ]}})
    adapter = SuishenbanAdapter(httpx.Client(transport=httpx.MockTransport(handler)), free_enjoy=True)
    assert [item.source_item_id for item in adapter.discover()] == ["free-1"]


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


def test_shanghai_policy_platform_uses_list_metadata_and_detail_txt_only():
    row = {"siteId": "0001", "businessId": "b1", "title": "列表标题", "displayDate": "2026-09-03", "docType": "沪府", "docYear": "2026", "docNo": "1", "attrs": {"agency": "列表发文单位"}}
    def handler(request):
        if request.url.path.endswith("/page"):
            return response(request, {"data": {"records": [row], "totalPage": 1}})
        return response(request, {"data": {"txt": "<h1>详情标题不使用</h1><p>详情正文</p>", "title": "详情标题"}})
    adapter = ShanghaiPolicyPlatformAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.GROUPS = (("市级", ["0001"]),)
    candidate = adapter.discover()[0]
    article = adapter.fetch(candidate)
    assert article.title == "列表标题"
    assert article.publish_dept == "列表发文单位"
    assert article.document_no == "沪府〔2026〕1号"
    assert article.raw_content_html == "<h1>详情标题不使用</h1><p>详情正文</p>"


def test_shanghai_policy_platform_discovery_reports_page_progress():
    rows = [
        {"siteId": "0001", "businessId": "b1", "title": "第一个"},
        {"siteId": "0001", "businessId": "b2", "title": "第二个"},
    ]

    def handler(request):
        page = request.read().decode()
        if '"pageNo":1' in page:
            return response(request, {"data": {"records": rows[:1], "totalPage": 2}})
        return response(request, {"data": {"records": rows[1:], "totalPage": 2}})

    adapter = ShanghaiPolicyPlatformAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.GROUPS = (("市级", ["0001"]),)
    progress: list[str] = []
    assert [item.project_name for item in adapter.discover(progress.append)] == ["第一个", "第二个"]
    assert progress == [
        "正在发现：市级，请求第 1 页（当前累计 0 条）",
        "发现进度：市级，第 1/2 页，本页 1 条，累计 1 条",
        "正在发现：市级，请求第 2 页（当前累计 1 条）",
        "发现进度：市级，第 2/2 页，本页 1 条，累计 2 条",
    ]


def test_shanghai_policy_platform_skips_timed_out_page_after_retries():
    calls: dict[int, int] = {}

    def handler(request):
        page = json.loads(request.content)["pageNo"]
        calls[page] = calls.get(page, 0) + 1
        if page == 2:
            raise httpx.ReadTimeout("temporary timeout", request=request)
        row = {"siteId": "0001", "businessId": f"b{page}", "title": f"第{page}页"}
        return response(request, {"data": {"records": [row], "totalPage": 3}})

    adapter = ShanghaiPolicyPlatformAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.GROUPS = (("各区", ["0070"]),)
    adapter.PAGE_RETRY_DELAYS = (0, 0)
    progress: list[str] = []

    candidates = adapter.discover(progress.append)

    assert [item.project_name for item in candidates] == ["第1页", "第3页"]
    assert calls == {1: 1, 2: 3, 3: 1}
    assert any("各区第 2/3 页连续重试失败" in message for message in progress)
    assert any("共跳过 1 个连续重试失败的页面：各区第2页" in message for message in progress)
