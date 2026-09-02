import pytest

from crawler_tool.url_inputs import candidate_from_url, normalize_urls


def test_normalize_urls_splits_newlines_semicolons_and_deduplicates():
    assert normalize_urls([" https://example.com/a;https://example.com/b\nhttps://example.com/a "]) == [
        "https://example.com/a", "https://example.com/b"
    ]


def test_normalize_urls_rejects_non_http_and_empty_input():
    with pytest.raises(ValueError, match="http"):
        normalize_urls(["ftp://example.com/a"])
    with pytest.raises(ValueError, match="至少"):
        normalize_urls([" ;\n "])


def test_candidate_from_url_extracts_query_and_hash_ids():
    suishenban = candidate_from_url("suishenban", "https://zwdt.sh.gov.cn/policy/project?policyProjectId=p-1")
    assert suishenban.source_item_id == "p-1" and suishenban.raw["url_reference"] == "project"
    qifuyun = candidate_from_url("qifuyun", "https://shpolicy.ssme.sh.gov.cn/knowledge/#/policy?policyId=q-1")
    assert qifuyun.source_item_id == "q-1"


def test_candidate_from_url_recognizes_suishenban_direct_policy_and_reports_missing_id():
    direct = candidate_from_url("suishenban", "https://zwdt.sh.gov.cn/policy/detail?policyId=policy-1")
    assert direct.source_item_id == "policy-1" and direct.raw["url_reference"] == "policy"
    with pytest.raises(ValueError, match="识别"):
        candidate_from_url("qifuyun", "https://shpolicy.ssme.sh.gov.cn/knowledge/")
