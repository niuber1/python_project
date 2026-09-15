from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import AdapterError, CrawlerAdapter, SourceEmptyError
from ..html_utils import parse_date
from ..models import PolicyArticle, PolicyCandidate


class ShanghaiPolicyPlatformAdapter(CrawlerAdapter):
    source_code = "shanghai_policy_platform"
    api_root = "https://www.shanghai.gov.cn"
    PAGE_RETRY_DELAYS = (1.0, 2.0)

    # 政策平台前端当前公开的站点集合；按三种页面口径请求。
    CITY_SITE_IDS = ["0001"]
    DISTRICT_SITE_IDS = [f"00{value}" for value in range(70, 86)]
    DEPARTMENT_SITE_IDS = [
        "0025", "0020", "0023", "0015", "0113", "0016", "0007", "0033", "0034", "0008", "0017", "0035", "0032", "0024", "0011", "0064", "0010", "0009", "0004", "0022", "0012", "0097", "0036", "0037", "0029", "0019", "0018", "0100", "0054", "0027", "0014", "0038", "0039", "0040", "0026", "0041", "0005", "0108", "0021", "0042", "0043", "0013", "0003", "0114", "0107", "0102", "0103", "0104", "0105", "0055", "0031", "0106", "0058", "0059", "0061", "0062", "0099", "0063", "0110", "0111", "5b61c6917fac40fe9d080d621ca8be64",
    ]
    GROUPS = (("市级", CITY_SITE_IDS), ("市级部门", DEPARTMENT_SITE_IDS), ("各区", DISTRICT_SITE_IDS))

    def health_url(self) -> str:
        return f"{self.api_root}/gwk/policy/page"

    @staticmethod
    def _document_no(row: dict[str, Any]) -> str | None:
        doc_type, year, number = (str(row.get(key) or "").strip() for key in ("docType", "docYear", "docNo"))
        if doc_type and year and number:
            return f"{doc_type}〔{year}〕{number}号"
        return str(row.get("docNum") or "").strip() or None

    @classmethod
    def _candidate(cls, row: dict[str, Any], level: str) -> PolicyCandidate | None:
        site_id, business_id = str(row.get("siteId") or "").strip(), str(row.get("businessId") or "").strip()
        title = str(row.get("title") or "").strip()
        if not site_id or not business_id or not title:
            return None
        query = urlencode({"siteId": site_id, "businessId": business_id})
        return PolicyCandidate(
            source_code=cls.source_code, source_item_id=f"{site_id}:{business_id}", project_name=title,
            detail_ref=business_id, original_url=f"{cls.api_root}/zhengce/detail?{query}",
            raw={"list_item": row, "site_id": site_id, "business_id": business_id, "policy_level": level},
        )

    def _fetch_page(
        self,
        level: str,
        site_ids: list[str],
        page: int,
        on_progress: Callable[[str], None] | None,
    ) -> tuple[dict[str, Any] | None, Exception | None]:
        attempts = len(self.PAGE_RETRY_DELAYS) + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.client.post(
                    f"{self.api_root}/gwk/policy/page",
                    json={"siteIdList": site_ids, "pageNo": page, "pageSize": 100},
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get("records") or [], list):
                    raise AdapterError("上海市统一政策发布平台列表返回格式无效")
                return data, None
            except (httpx.HTTPError, ValueError, AdapterError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = self.PAGE_RETRY_DELAYS[attempt - 1]
                if on_progress:
                    on_progress(
                        f"发现警告：{level}第 {page} 页请求失败（第 {attempt}/{attempts} 次，"
                        f"{type(exc).__name__}），{delay:g} 秒后重试"
                    )
                if delay:
                    time.sleep(delay)
        return None, last_error

    def discover(self, on_progress: Callable[[str], None] | None = None) -> list[PolicyCandidate]:
        result: list[PolicyCandidate] = []
        failed_pages: list[str] = []
        for level, site_ids in self.GROUPS:
            page = 1
            total_page: int | None = None
            while True:
                if on_progress:
                    on_progress(f"正在发现：{level}，请求第 {page} 页（当前累计 {len(result)} 条）")
                data, error = self._fetch_page(level, site_ids, page, on_progress)
                if data is None:
                    failed_pages.append(f"{level}第{page}页")
                    error_name = type(error).__name__ if error else "未知错误"
                    if total_page is None:
                        if on_progress:
                            on_progress(
                                f"发现警告：{level}第 {page} 页连续重试失败（{error_name}），"
                                "无法确定总页数，跳过该层级"
                            )
                        break
                    if on_progress:
                        on_progress(
                            f"发现警告：{level}第 {page}/{total_page} 页连续重试失败"
                            f"（{error_name}），跳过该页并继续"
                        )
                    if page >= total_page:
                        break
                    page += 1
                    continue
                rows = data.get("records") or []
                for row in rows:
                    if isinstance(row, dict):
                        candidate = self._candidate(row, level)
                        if candidate:
                            result.append(candidate)
                total_page = int(data.get("totalPage") or total_page or page)
                if on_progress:
                    on_progress(f"发现进度：{level}，第 {page}/{total_page} 页，本页 {len(rows)} 条，累计 {len(result)} 条")
                if not rows or page >= total_page:
                    break
                page += 1
                if page > 10000:
                    raise AdapterError("上海市统一政策发布平台分页超过安全上限")
        if failed_pages and on_progress:
            on_progress(
                f"发现完成：共跳过 {len(failed_pages)} 个连续重试失败的页面："
                + "、".join(failed_pages[:20])
                + ("等" if len(failed_pages) > 20 else "")
            )
        return result

    def fetch(self, candidate: PolicyCandidate) -> PolicyArticle:
        raw = candidate.raw or {}
        site_id, business_id = str(raw.get("site_id") or "").strip(), str(raw.get("business_id") or "").strip()
        if not site_id or not business_id:
            raise SourceEmptyError("上海市统一政策发布平台详情缺少 siteId 或 businessId")
        response = self.client.post(f"{self.api_root}/gwk/policy/detail", json={"siteId": site_id, "businessId": business_id})
        response.raise_for_status()
        detail = (response.json() or {}).get("data") or {}
        content = str(detail.get("txt") or "").strip()
        if not content:
            raise SourceEmptyError("上海市统一政策发布平台政策正文为空")
        row = raw.get("list_item") if isinstance(raw.get("list_item"), dict) else detail
        attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
        title = str(row.get("title") or detail.get("title") or candidate.project_name).strip()
        if not title:
            raise SourceEmptyError("上海市统一政策发布平台政策标题为空")
        return PolicyArticle(
            source_code=self.source_code, source_item_id=candidate.source_item_id, source_name="上海市统一政策发布平台",
            title=title, project_name=title, policy_level=str(raw.get("policy_level") or "") or None,
            publish_dept=str(attrs.get("agency") or "").strip() or None,
            document_no=self._document_no(row), publish_date=parse_date(row.get("displayDate")),
            original_url=candidate.original_url or f"{self.api_root}/zhengce/detail?{urlencode({'siteId': site_id, 'businessId': business_id})}",
            raw_content_html=content, raw={"list_item": raw.get("list_item"), "detail": detail, "policy_level": raw.get("policy_level")},
        )
