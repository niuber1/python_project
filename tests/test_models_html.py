from datetime import date

import pytest

from crawler_tool.html_utils import compose_document_content, normalize_article_html
from crawler_tool.models import Attachment, CrawlerPayload, PolicyArticle, deterministic_document_id


def article(**overrides):
    data = dict(
        source_code="demo", source_item_id="123", source_name="来源", title="标题", project_name="项目",
        original_url="https://example.com/a/detail", raw_content_html='<p>中文</p><img src="../a.png"><table><tr><td>值</td></tr></table><script>alert(1)</script><video src="x"></video>',
        attachments=[Attachment(name="附件", url="https://example.com/a.pdf")],
    )
    data.update(overrides)
    return PolicyArticle(**data)


def test_html_normalization_keeps_useful_content_and_removes_dangerous_tags():
    content = normalize_article_html(article())
    assert "中文" in content and "<table>" in content
    assert 'src="https://example.com/a.png"' in content
    assert "script" not in content and "video" not in content and "alert(1)" not in content
    assert "https://example.com/a.pdf" in content


def test_normalization_outputs_pure_body_without_metadata_block():
    content = normalize_article_html(article())
    assert "政策信息" not in content
    assert "政策正文" not in content
    assert "<p>中文</p>" in content


def test_normalization_strips_source_side_policy_info_block():
    raw = '<section><h2>政策信息</h2><dl><dt><strong>项目名称</strong></dt><dd>某项目</dd></dl></section><section><h2>政策正文</h2><p>各区经委：正文内容</p></section>'
    content = normalize_article_html(article(raw_content_html=raw))
    assert "政策信息" not in content and "某项目" not in content
    assert "各区经委：正文内容" in content


def test_composed_content_prefixes_agent_metadata_and_keeps_rich_body():
    content = compose_document_content(article(title="政策标题", publish_date=date(2023, 5, 19), document_no="奉人社〔2023〕9号"))
    assert "<h1>政策标题</h1>" in content
    assert "发文日期" in content and "2023-05-19" in content and "奉人社〔2023〕9号" in content
    assert "<table>" in content and "https://example.com/a.pdf" in content


def test_media_only_content_is_not_a_valid_policy_body():
    with pytest.raises(ValueError):
        compose_document_content(article(raw_content_html='<video src="a"></video><audio src="b"></audio>', attachments=[]))


def test_payload_aliases_and_dates():
    payload = CrawlerPayload(id="a" * 32, bt="标题", url="https://example.com", pubDate="2026-08-01", content="<p>正文</p>", source="来源", baseId="base")
    assert payload.to_kms_json()["bt"] == "标题"
    assert payload.to_kms_json()["pubDate"] == "2026-08-01"


def test_payload_rejects_empty_html_and_bad_url():
    with pytest.raises(ValueError):
        CrawlerPayload(id="a" * 32, bt="标题", url="/relative", content="<p> </p>", source="来源", baseId="base")


def test_document_id_is_deterministic_and_32_hex():
    first = deterministic_document_id("source", "id", "base")
    assert first == deterministic_document_id("source", "id", "base")
    assert len(first) == 32
