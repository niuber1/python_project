import json

import httpx
import pytest

from crawler_tool.config import Settings
from crawler_tool.policy_classifier import ClassificationError, ClassificationSkipped, PolicyClassifier


def _settings():
    return Settings(db_user="x", db_password="x")


def test_classifier_routes_both_allowed_types():
    def handler(request):
        assert request.url == "https://aies.dreamdt.cn/policy/api/policy-classification/classify"
        assert json.loads(request.content) == {"content": "正文"}
        return httpx.Response(200, json={"success": True, "data": {"applicable_type": "惠企", "policy_type": "非申报通知类"}}, request=request)
    classifier = PolicyClassifier(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    result = classifier.classify("<p>正文</p>", "declare", "non")
    assert result.base_id == "non" and result.applicable_type == "惠企" and result.policy_type == "非申报通知类"


def test_classifier_routes_non_declare_alias_to_non_declare_base():
    def handler(request):
        return httpx.Response(200, json={"success": True, "data": {"applicable_type": "惠企", "policy_type": "非申报类"}}, request=request)
    result = PolicyClassifier(_settings(), httpx.Client(transport=httpx.MockTransport(handler))).classify("<p>正文</p>", "declare", "non-declare")
    assert result.base_id == "non-declare" and result.policy_type == "非申报类"


def test_classifier_routes_declare_alias_to_declare_base():
    def handler(request):
        return httpx.Response(200, json={"success": True, "data": {"applicable_type": "惠企", "policy_type": "申报类"}}, request=request)
    result = PolicyClassifier(_settings(), httpx.Client(transport=httpx.MockTransport(handler))).classify("<p>正文</p>", "declare", "non-declare")
    assert result.base_id == "declare" and result.policy_type == "申报类"


def test_classifier_rejects_non_eligible_and_invalid_response():
    def skipped(request):
        return httpx.Response(200, json={"success": True, "data": {"applicable_type": "个人", "policy_type": "申报通知类"}}, request=request)
    with pytest.raises(ClassificationSkipped):
        PolicyClassifier(_settings(), httpx.Client(transport=httpx.MockTransport(skipped))).classify_base_id("<p>x</p>", "a", "b")
    def invalid(request):
        return httpx.Response(200, json={"success": False}, request=request)
    with pytest.raises(ClassificationError):
        PolicyClassifier(_settings(), httpx.Client(transport=httpx.MockTransport(invalid))).classify_base_id("<p>x</p>", "a", "b")
