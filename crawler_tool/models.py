from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime
from typing import Any, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator


HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
DOCUMENT_NAMESPACE = uuid.UUID("b6d90bc3-6f51-55f4-a943-b678f9e92a79")


def stable_source_key(source_code: str, source_item_id: str | None, url: str | None) -> str:
    if source_item_id and source_item_id.strip():
        return source_item_id.strip()
    normalized_url = (url or "").strip().lower()
    if not normalized_url:
        raise ValueError("source_item_id and url cannot both be empty")
    return hashlib.sha256(f"{source_code}:{normalized_url}".encode("utf-8")).hexdigest()


def deterministic_document_id(source_code: str, source_item_id: str, base_id: str) -> str:
    return uuid.uuid5(DOCUMENT_NAMESPACE, f"{source_code}:{source_item_id}:{base_id}").hex


class Attachment(BaseModel):
    name: str = "附件"
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if not HTTP_URL.match(value):
            raise ValueError("attachment url must use http or https")
        return value


class PolicyCandidate(BaseModel):
    source_code: str
    source_item_id: str
    project_name: str
    detail_ref: str | None = None
    original_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class PolicyArticle(BaseModel):
    source_code: str
    source_item_id: str
    source_name: str
    title: str
    project_name: str
    policy_level: str | None = None
    publish_dept: str | None = None
    document_no: str | None = None
    publish_date: date | None = None
    apply_start: date | None = None
    apply_end: date | None = None
    original_url: str
    raw_content_html: str
    attachments: list[Attachment] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "project_name")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required text cannot be blank")
        return value

    @field_validator("original_url")
    @classmethod
    def original_url_is_http(cls, value: str) -> str:
        value = value.strip()
        if not HTTP_URL.match(value):
            raise ValueError("original_url must use http or https")
        return value


class CrawlerPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    title: str = Field(alias="bt", min_length=1)
    url: str
    publish_date: str | None = Field(default=None, alias="pubDate")
    document_no: str | None = Field(default=None, alias="wh")
    content: str
    source: str
    base_id: str = Field(alias="baseId", min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("title", "source", "base_id")
    @classmethod
    def required_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required KMS field cannot be blank")
        return value

    @field_validator("url")
    @classmethod
    def kms_url_is_http(cls, value: str) -> str:
        value = value.strip()
        if not HTTP_URL.match(value):
            raise ValueError("KMS url must use http or https")
        return value

    @field_validator("publish_date")
    @classmethod
    def validate_publish_date(cls, value: str | None) -> str | None:
        if value:
            date.fromisoformat(value)
        return value

    @field_validator("content")
    @classmethod
    def meaningful_html(cls, value: str) -> str:
        text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
        if not text:
            raise ValueError("KMS content cannot be empty HTML")
        return value

    def to_kms_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class KmsResult(BaseModel):
    success: bool
    code: str
    message: str
    attempts: int = 1
    raw: Any = None


class StartRunRequest(BaseModel):
    task_codes: list[str] = Field(default_factory=list)
    dry_run: bool = True
    confirm_write: bool = False
    phase: Literal["all", "crawl", "push"] = "all"
    auto_sync: bool = False
    refresh_existing: bool = False


class PushArticlesRequest(BaseModel):
    article_ids: list[str] = Field(min_length=1)
    dry_run: bool = False
    confirm_write: bool = False


class UpdateArticlesRequest(PushArticlesRequest):
    """覆盖更新 KMS 正文的已抓取文章请求。"""


class KmsAuthConfigRequest(BaseModel):
    """仅用于本机服务运行期的 KMS 鉴权配置，不落库、不回显。"""

    authorization_token: str = Field(default="", max_length=8192)
    cookie: str = Field(default="", max_length=16384)


class RunRecord(BaseModel):
    run_id: str
    trigger_type: str
    status: str
    task_codes: list[str]
    dry_run: bool
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    current_item: str | None = None
    message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
