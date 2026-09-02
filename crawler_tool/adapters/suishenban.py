from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from .base import AdapterError, CrawlerAdapter, SourceEmptyError
from ..html_utils import decode_possible_base64_html, is_shanghai_district, parse_date, parse_shanghai_government_page
from ..models import Attachment, PolicyArticle, PolicyCandidate


class SuishenbanAdapter(CrawlerAdapter):
    source_code = "suishenban"
    api_root = "https://zwdt.sh.gov.cn/qykj/shspace/"
    web_root = "https://zwdt.sh.gov.cn/qykj/shell_oc_policy_zq/policy/"

    def health_url(self) -> str:
        return urljoin(self.api_root, "callinterface/policyproject")

    def discover(self) -> list[PolicyCandidate]:
        # 与网站「政策中心」页面同口径：申报期限=进行中+即将开始(applyState 1,2)、是否免申=否。
        # 筛选由服务端完成（与手动访问看到的结果一致），本地不再做日期推算。
        body = {
            "isNeedSort": False,
            "containProject": True,
            "objectType": 1,
            "sortStrategy": 0,
            "applyState": "1,2",  # 1=即将开始(未开始) 2=申报中(进行中)
            "policyType": ["BTLX", "RZLX", "JMLX", "RCLX", "RYLX", "JYLX", "QT"],
            "freeEnjoy": False,   # 是否免申：否
            "open": True,
            "clientType": "1",    # 企业端
        }
        result, page, fetched = [], 0, 0
        while True:
            payload_body = {**body, "page": page, "size": 100}
            response = self.client.post(
                urljoin(self.api_root, "policy_center/hqPolicy/projects"),
                params={"page": page, "size": 100, "isNeedSort": False},
                json=payload_body,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise AdapterError("随申办政策中心列表接口返回了非对象结构")
            data = payload["data"]
            total = int(data.get("total") or 0)
            rows = data.get("list") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # 防御性复查：仅保留非免申（正常由服务端 freeEnjoy=false 过滤）
                if str(row.get("freeEnjoy")).strip().lower() not in {"false", "0", "否"}:
                    continue
                project_id = str(row.get("id") or "").strip()
                if not project_id:
                    continue
                result.append(PolicyCandidate(
                    source_code=self.source_code,
                    source_item_id=project_id,
                    project_name=str(row.get("description") or row.get("name") or row.get("projectName") or "未命名项目"),
                    detail_ref=project_id,
                    raw=row,
                ))
            fetched += len(rows)
            if not rows or fetched >= total:
                return result
            page += 1
            if page > 500:
                raise AdapterError("随申办分页超过安全上限")

    @staticmethod
    def _policy_refs(row: dict[str, Any]) -> list[str]:
        original = row.get("policyOriginal")
        if isinstance(original, list):
            refs = []
            for item in original:
                if not isinstance(item, dict):
                    continue
                value = item.get("id") or item.get("policyId") or item.get("value")
                if value:
                    refs.append(str(value))
            return refs
        if isinstance(original, dict):
            for key in ("id", "policyId", "value"):
                if original.get(key):
                    return [str(original[key])]
        if isinstance(original, str) and original.strip():
            return [original.strip()]
        return []

    def fetch(self, candidate: PolicyCandidate) -> PolicyArticle:
        if candidate.raw.get("url_reference") == "shanghai_government":
            return self._fetch_shanghai_government_page(candidate)
        if candidate.raw.get("url_reference") == "policy":
            return self._fetch_policy(candidate, candidate.detail_ref, {}, {})
        project_response = self.client.get(
            urljoin(self.api_root, "policy_center/hqPolicy/questions"),
            params={"policyProjectId": candidate.detail_ref},
        )
        project_response.raise_for_status()
        project = project_response.json().get("policyProject") or {}
        source_policy = project.get("sourcePolicy") or {}
        policy_id = str(source_policy.get("id") or "").strip()
        if not policy_id:
            raise SourceEmptyError("随申办项目未关联政策原文")
        return self._fetch_policy(candidate, policy_id, project, candidate.raw)

    def _fetch_shanghai_government_page(self, candidate: PolicyCandidate) -> PolicyArticle:
        if not candidate.original_url:
            raise SourceEmptyError("上海一网通办公文 URL 为空")
        response = self.client.get(candidate.original_url)
        response.raise_for_status()
        parsed = parse_shanghai_government_page(response.text)
        if not parsed["title"]:
            raise SourceEmptyError("上海一网通办公文标题为空")
        if not parsed["content_html"].strip():
            raise SourceEmptyError("上海一网通办公文正文为空")
        return PolicyArticle(
            source_code=self.source_code,
            source_item_id=candidate.source_item_id,
            source_name="上海一网通办",
            title=parsed["title"],
            project_name=parsed["title"],
            publish_dept=parsed["publish_dept"] or None,
            document_no=parsed["document_no"] or None,
            publish_date=parsed["publish_date"],
            original_url=candidate.original_url,
            raw_content_html=parsed["content_html"],
            raw={"url_input": candidate.original_url, "page_type": "shanghai_government"},
        )

    def _fetch_policy(self, candidate: PolicyCandidate, policy_id: str | None, project: dict[str, Any], row: dict[str, Any]) -> PolicyArticle:
        if not policy_id:
            raise SourceEmptyError("随申办 URL 未识别政策 ID")
        response = self.client.get(
            urljoin(self.api_root, "policy_center/hqPolicy/policyDetail"),
            params={"policyId": policy_id},
        )
        response.raise_for_status()
        detail = response.json().get("policy") or {}
        if not detail:
            raise SourceEmptyError("随申办政策详情为空")
        content = decode_possible_base64_html(str(detail.get("content") or ""))
        if not content.strip():
            raise SourceEmptyError("随申办政策正文为空")
        attachments = []
        for item in detail.get("files") or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("fileUrl") or item.get("downloadUrl")
            if url:
                attachments.append(Attachment(name=item.get("name") or item.get("fileName") or "附件", url=urljoin(self.web_root, url)))
        for key, label in (("form", "申报表单"), ("downloadUrl", "下载附件")):
            value = detail.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                attachments.append(Attachment(name=label, url=value))
        policy_id = str(detail.get("id") or policy_id)
        original_url = detail.get("url") or f"{self.web_root}policy-detail?id={policy_id}"
        # 政策层级/发文单位只取展示名；接口的 level/publishDepartment 是编码（如 ZCJB0001005/SHHQGW），
        # 无展示名时保持为空，不落编码入库；区级政策的『部门』只有区名（如 闵行区），同样视为无效置空
        level = detail.get("levelName")
        department = detail.get("pubDeptName") or detail.get("publishDepartmentName")
        if is_shanghai_district(department):
            department = None
        if isinstance(department, list):
            department = "、".join(str(x.get("label") or x.get("name") or x) if isinstance(x, dict) else str(x) for x in department)
        time_tags = project.get("timeTags") or row.get("timeTags") or []
        main_tag = next((item for item in time_tags if isinstance(item, dict) and item.get("main")), None)
        if main_tag is None:
            main_tag = next((item for item in time_tags if isinstance(item, dict)), {})
        return PolicyArticle(
            source_code=self.source_code,
            source_item_id=candidate.source_item_id,
            source_name="上海一网通办",
            title=str(detail.get("name") or candidate.project_name),
            project_name=str(project.get("description") or project.get("name") or candidate.project_name),
            policy_level=str(level) if level else None,
            publish_dept=str(department) if department else None,
            document_no=detail.get("code") if str(detail.get("code") or "").strip() not in {"", "/", "-", "无"} else None,
            publish_date=parse_date(detail.get("releaseTime")),
            apply_start=parse_date(main_tag.get("start_time") or main_tag.get("startTime") or row.get("applyStart")),
            apply_end=parse_date(main_tag.get("end_time") or main_tag.get("endTime") or row.get("applyEnd")),
            original_url=urljoin(self.web_root, original_url),
            raw_content_html=content,
            attachments=attachments,
            raw={"project_list": row, "project_detail": project, "policy": detail},
        )
