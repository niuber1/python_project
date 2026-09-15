from __future__ import annotations

from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup


class ClassificationError(RuntimeError):
    """Policy 智能体分类不可用或返回不符合接口约定。"""


class ClassificationSkipped(RuntimeError):
    """智能体正常返回，但政策不属于允许入库范围。"""

    def __init__(self, applicable_type: str, policy_type: str):
        self.applicable_type = applicable_type
        self.policy_type = policy_type
        super().__init__(f"分类结果不入库：{applicable_type or '未识别'} / {policy_type or '未识别'}")


@dataclass(frozen=True)
class ClassificationResult:
    applicable_type: str
    policy_type: str
    base_id: str


class PolicyClassifier:
    def __init__(self, settings, client: httpx.Client):
        self.settings = settings
        self.client = client

    def classify(self, content_html: str, declare_base_id: str, non_declare_base_id: str) -> ClassificationResult:
        text = BeautifulSoup(content_html, "lxml").get_text("\n", strip=True)
        if not text:
            raise ClassificationError("智能体分类正文为空")
        try:
            response = self.client.post(self.settings.policy_classify_url, json={"content": text})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ClassificationError(f"智能体分类调用失败：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), dict):
            raise ClassificationError("智能体分类返回格式无效")
        data = payload["data"]
        applicable_type = str(data.get("applicable_type") or "").strip()
        policy_type = str(data.get("policy_type") or "").strip()
        if applicable_type != "惠企" or policy_type not in {"申报通知类", "申报类", "非申报通知类", "非申报类"}:
            raise ClassificationSkipped(applicable_type, policy_type)
        base_id = declare_base_id if policy_type in {"申报通知类", "申报类"} else non_declare_base_id
        return ClassificationResult(applicable_type=applicable_type, policy_type=policy_type, base_id=base_id)

    def classify_base_id(self, content_html: str, declare_base_id: str, non_declare_base_id: str) -> str:
        """兼容已有调用方：仅返回入库知识库 ID。"""
        return self.classify(content_html, declare_base_id, non_declare_base_id).base_id
