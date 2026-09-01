from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from .config import Settings
from .models import CrawlerPayload, KmsResult


KMS_MESSAGES = {
    "0": "知识库不存在", "1": "入库成功", "2": "创建文档失败", "3": "正文解析失败",
    "4": "参数校验失败", "5": "知识库处理中", "6": "文档已撤销", "7": "文档已存在",
    "8": "知识库类型不支持", "9": "文件处理失败", "11": "文档写入失败", "99": "未知错误",
}

class KmsClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.kms_timeout_seconds)
        self._owns_client = client is None
        self.sleeper = sleeper

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _business_code(response: httpx.Response) -> tuple[str, Any]:
        try:
            raw = response.json()
        except ValueError:
            raw = response.text.strip()
        value = raw
        for _ in range(3):
            if isinstance(value, dict) and "data" in value:
                value = value["data"]
            else:
                break
        if isinstance(value, str):
            return value.strip().strip('"'), raw
        if isinstance(value, bool):
            return str(value).lower(), raw
        if isinstance(value, (int, float)):
            return str(value), raw
        return "UNPARSEABLE", raw

    @staticmethod
    def _http_error_message(response: httpx.Response) -> str:
        """保留 KMS 网关返回的简短错误正文，便于定位 4xx/5xx。"""
        detail = response.text.replace("\r", " ").replace("\n", " ").strip()
        return f"KMS HTTP {response.status_code}" + (f": {detail[:500]}" if detail else "")

    @staticmethod
    def auth_headers(authorization_token: str, cookie: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        token = authorization_token.strip()
        if token:
            # KMS 网页请求使用 Authorization-Token；OpenAPI 网关兼容读取
            # access_token/Authorization，因此三者都传递同一令牌。
            headers["Authorization-Token"] = token
            headers["access_token"] = token
            headers["Authorization"] = token
        if cookie.strip():
            headers["Cookie"] = cookie.strip()
        return headers

    def _add_auth_headers(self, headers: dict[str, str]) -> None:
        headers.update(self.auth_headers(self.settings.kms_authorization_token, self.settings.kms_cookie))
        # 兼容旧配置；新页面不再提供此字段。
        if self.settings.kms_authorization:
            headers["Authorization"] = self.settings.kms_authorization

    def test_auth(self, authorization_token: str, cookie: str) -> KmsResult:
        """调用 KMS 只读接口验证鉴权，不写入任何知识库数据。"""
        try:
            response = self.client.post(self.settings.kms_auth_check_url, headers=self.auth_headers(authorization_token, cookie))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return KmsResult(success=False, code="NETWORK_ERROR", message=f"KMS 网络异常: {type(exc).__name__}")
        if response.status_code >= 400:
            return KmsResult(success=False, code=f"HTTP_{response.status_code}", message=self._http_error_message(response))
        try:
            raw = response.json()
        except ValueError:
            raw = response.text.strip()
        if isinstance(raw, dict) and raw.get("success") is False:
            return KmsResult(success=False, code=str(raw.get("state") or "AUTH_FAILED"), message=str(raw.get("message") or "KMS 鉴权失败"), raw=raw)
        return KmsResult(success=True, code="AUTH_OK", message="KMS 鉴权有效", raw=raw)

    def push(self, payload: CrawlerPayload) -> KmsResult:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        self._add_auth_headers(headers)
        last_message = ""
        for attempt in range(1, 4):
            try:
                response = self.client.post(self.settings.kms_url, json=payload.to_kms_json(), headers=headers)
                if response.status_code >= 500:
                    last_message = self._http_error_message(response)
                    if attempt < 3:
                        self.sleeper(2 ** (attempt - 1))
                        continue
                    return KmsResult(success=False, code=f"HTTP_{response.status_code}", message=last_message, attempts=attempt)
                if response.status_code >= 400:
                    return KmsResult(success=False, code=f"HTTP_{response.status_code}", message=self._http_error_message(response), attempts=attempt)
                code, raw = self._business_code(response)
                return KmsResult(
                    # 1=入库成功 5=知识库处理中(异步拆解/解析中，视为已入库) 7=文档已存在
                    success=code in {"1", "5", "7"}, code=code,
                    message=KMS_MESSAGES.get(code, f"KMS 未知业务码 {code}"), attempts=attempt, raw=raw,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_message = f"KMS 网络异常: {type(exc).__name__}"
                if attempt < 3:
                    self.sleeper(2 ** (attempt - 1))
                    continue
        return KmsResult(success=False, code="NETWORK_ERROR", message=last_message, attempts=3)

    def update_content(self, document_id: str, content: str, publish_date: str | None = None) -> KmsResult:
        """通过 KMS 公开文档更新接口覆盖正文，并触发重新分段与元数据处理。"""
        headers = {"Content-Type": "application/json; charset=utf-8"}
        self._add_auth_headers(headers)
        # KMS 的 openapi 在此字段中使用的是“租户 ID”而不是知识库 ID。
        # 传入 base_id 会切换至错误的数据上下文；标准调用仅按 documentId 更新。
        # cwrq 必须回传，否则 KMS 会把成文日期覆盖成当前日期。
        body: dict[str, Any] = {"documentId": document_id, "content": content, "permission": []}
        if publish_date:
            body["cwrq"] = publish_date
        last_message = ""
        for attempt in range(1, 4):
            try:
                response = self.client.post(self.settings.kms_update_url, json=body, headers=headers)
                if response.status_code >= 500:
                    last_message = self._http_error_message(response)
                    if attempt < 3:
                        self.sleeper(2 ** (attempt - 1))
                        continue
                    return KmsResult(success=False, code=f"HTTP_{response.status_code}", message=last_message, attempts=attempt)
                if response.status_code >= 400:
                    return KmsResult(success=False, code=f"HTTP_{response.status_code}", message=self._http_error_message(response), attempts=attempt)
                code, raw = self._business_code(response)
                success = code.lower() in {"true", "1", "5"}
                return KmsResult(success=success, code="UPDATE_OK" if success else "UPDATE_FAILED", message="正文覆盖更新成功" if success else "KMS 正文覆盖更新失败", attempts=attempt, raw=raw)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_message = f"KMS 网络异常: {type(exc).__name__}"
                if attempt < 3:
                    self.sleeper(2 ** (attempt - 1))
                    continue
        return KmsResult(success=False, code="NETWORK_ERROR", message=last_message, attempts=3)
