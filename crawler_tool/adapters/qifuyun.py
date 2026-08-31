from __future__ import annotations

from urllib.parse import urljoin

from .base import AdapterError, CrawlerAdapter, SourceEmptyError
from ..html_utils import is_shanghai_district, parse_date
from ..models import Attachment, PolicyArticle, PolicyCandidate


class QifuyunAdapter(CrawlerAdapter):
    source_code = "qifuyun"
    api_root = "https://shpolicy.ssme.sh.gov.cn/governmentCloudApi/"
    web_root = "https://shpolicy.ssme.sh.gov.cn/knowledge/"

    def health_url(self) -> str:
        return urljoin(self.api_root, "chatSNet/policy")

    def discover(self) -> list[PolicyCandidate]:
        page, result = 1, []
        while True:
            body = {
                "pageNum": page, "pageSize": 100, "area": "上海市", "industryType": None,
                "projectType": None, "informationSource": None, "applicationStatus": "申报中", "name": "",
            }
            response = self.client.post(urljoin(self.api_root, "chatSNet/policy"), json=body)
            response.raise_for_status()
            payload = response.json().get("data", {}).get("respData", {})
            rows = payload.get("dataList") or []
            for row in rows:
                source_id = str(row.get("id") or row.get("policyId") or "").strip()
                if not source_id:
                    continue
                result.append(PolicyCandidate(
                    source_code=self.source_code,
                    source_item_id=source_id,
                    project_name=str(row.get("name") or row.get("projectName") or "未命名项目"),
                    detail_ref=source_id,
                    original_url=row.get("originalUrl"),
                    raw=row,
                ))
            total = int(payload.get("total") or len(result))
            if not rows or len(result) >= total:
                return result
            page += 1
            if page > 1000:
                raise AdapterError("企服云分页超过安全上限")

    def fetch(self, candidate: PolicyCandidate) -> PolicyArticle:
        response = self.client.get(
            urljoin(self.api_root, "chatSNet/policyInfo"),
            params={"policyId": candidate.detail_ref, "type": "申报通知"},
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", {}).get("respData", {}).get("dataList") or []
        if not rows:
            raise SourceEmptyError("企服云政策原文 dataList 为空")
        detail = rows[0]
        content = str(detail.get("content") or "").strip()
        if not content:
            raise SourceEmptyError("企服云政策原文正文为空")
        raw_attachments = detail.get("attachments") or []
        attachments = []
        for item in raw_attachments:
            url = item.get("filePath") or item.get("url")
            if url:
                attachments.append(Attachment(name=item.get("fileName") or "附件", url=urljoin(self.web_root, url)))
        row = candidate.raw
        original_url = detail.get("originalURL") or detail.get("originalUrl") or candidate.original_url
        if not original_url:
            original_url = f"{self.web_root}#/policyDetail?policyId={candidate.source_item_id}"
        # 发布单位只取真实部门名；区级政策的『部门』只有区名（如 闵行区），视为无效置空
        publish_dept = detail.get("department") or row.get("department") or row.get("publishDepartment")
        if is_shanghai_district(publish_dept):
            publish_dept = None
        return PolicyArticle(
            source_code=self.source_code,
            source_item_id=candidate.source_item_id,
            source_name="上海市企业服务云",
            title=str(detail.get("name") or row.get("name") or candidate.project_name),
            project_name=candidate.project_name,
            policy_level=row.get("policyLevel") or row.get("level") or ("市级" if (detail.get("area") or row.get("area")) == "上海市" else None),
            publish_dept=publish_dept,
            document_no=detail.get("documentNo") or row.get("documentNo"),
            publish_date=parse_date(detail.get("publishTime") or detail.get("date") or detail.get("publishDate") or row.get("publishDate")),
            apply_start=parse_date(row.get("startDate") or row.get("applicationStartTime") or row.get("applyStart")),
            apply_end=parse_date(row.get("endDate") or row.get("applicationEndTime") or row.get("applyEnd")),
            original_url=urljoin(self.web_root, original_url),
            raw_content_html=content,
            attachments=attachments,
            raw={"list": row, "detail": detail},
        )
