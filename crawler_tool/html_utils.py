from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
from datetime import date
from typing import Iterable
from urllib.parse import urljoin

import bleach
from bs4 import BeautifulSoup

from .models import Attachment, PolicyArticle


ALLOWED_TAGS = [
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "col", "colgroup",
    "dd", "div", "dl", "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "img", "li", "ol", "p", "pre", "section", "small", "span",
    "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "u", "ul",
]
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan", "scope"],
    "col": ["span"],
    "colgroup": ["span"],
}
FORBIDDEN_TAGS = ["script", "style", "iframe", "video", "audio", "object", "embed", "form"]
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def decode_possible_base64_html(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if "<" in value and ">" in value:
        return value
    if len(value) < 16 or not BASE64_RE.fullmatch(value):
        return value
    try:
        decoded = base64.b64decode(value, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value
    return decoded if "<" in decoded and ">" in decoded else value


def _absolute_links(soup: BeautifulSoup, base_url: str) -> None:
    for tag, attr in (("a", "href"), ("img", "src")):
        for element in soup.find_all(tag):
            current = element.get(attr)
            if current:
                element[attr] = urljoin(base_url, current.strip())
            if tag == "a":
                element["target"] = "_blank"
                element["rel"] = "noopener noreferrer"


def _attachments_html(attachments: Iterable[Attachment]) -> str:
    items = "".join(
        f'<li><a href="{html.escape(item.url, quote=True)}" target="_blank" rel="noopener noreferrer">'
        f"{html.escape(item.name)}</a></li>"
        for item in attachments
    )
    return f"<section><h2>附件</h2><ul>{items}</ul></section>" if items else ""


def normalize_article_html(article: PolicyArticle) -> str:
    raw = decode_possible_base64_html(article.raw_content_html)
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup.find_all(FORBIDDEN_TAGS):
        tag.decompose()
    _absolute_links(soup, article.original_url)
    # 纯正文要求：剔除源站正文中自带的「政策信息」等元数据块（元数据由 payload 单独携带，正文不重复）
    for section in soup.find_all(["section", "div"]):
        heading = section.find(["h2", "h3"])
        if heading and "政策信息" in heading.get_text():
            section.decompose()
    body = soup.body.decode_contents() if soup.body else str(soup)
    body_cleaned = bleach.clean(
        body,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    body_soup = BeautifulSoup(body_cleaned, "lxml")
    # 附件不是政策原文：音视频等被清除后原文为空时，即使有附件也不入库。
    if not body_soup.get_text(" ", strip=True) and not body_soup.find("img"):
        raise ValueError("normalized article content is empty")
    return body_cleaned + _attachments_html(article.attachments)


def prepend_document_metadata(
    content: str,
    *,
    title: str,
    publish_date: date | None = None,
    document_no: str | None = None,
    publish_dept: str | None = None,
    policy_level: str | None = None,
) -> str:
    """将供智能体提取的政策关键信息置于正文最前，正文和附件内容保持不变。"""
    fields = [
        ("发文日期", publish_date.isoformat() if publish_date else None),
        ("文号", document_no),
        ("发文部门", publish_dept),
        ("政策层级", policy_level),
    ]
    details = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(str(value).strip())}</dd>"
        for label, value in fields if value and str(value).strip()
    )
    header = f"<section><h1>{html.escape(title.strip())}</h1>"
    if details:
        header += f"<dl>{details}</dl>"
    return f"{header}</section><hr>{content}"


def compose_document_content(article: PolicyArticle) -> str:
    """生成发送 KMS 与智能体的完整正文：前置元数据、正文、附件。"""
    return prepend_document_metadata(
        normalize_article_html(article),
        title=article.title,
        publish_date=article.publish_date,
        document_no=article.document_no,
        publish_dept=article.publish_dept,
        policy_level=article.policy_level,
    )


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


SHANGHAI_DISTRICTS = {
    "黄浦区", "徐汇区", "长宁区", "静安区", "普陀区", "虹口区", "杨浦区", "闵行区",
    "宝山区", "嘉定区", "浦东新区", "金山区", "松江区", "青浦区", "奉贤区", "崇明区",
}


def is_shanghai_district(value: object) -> bool:
    """源接口对区级政策只提供区名级别的『部门』（如 闵行区），不视为真实发文单位。"""
    return str(value or "").strip() in SHANGHAI_DISTRICTS


def parse_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:10].replace("/", "-")
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if not match:
            return None
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
