from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .models import PolicyCandidate


URL_SPLIT_RE = re.compile(r"[;\r\n]+")


def normalize_urls(values: list[str]) -> list[str]:
    """拆分多行/分号输入，保留首次出现的 HTTP(S) URL。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for piece in URL_SPLIT_RE.split(value or ""):
            url = piece.strip()
            parsed = urlparse(url)
            if not url:
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"URL 必须以 http:// 或 https:// 开头：{url[:120]}")
            if url not in seen:
                seen.add(url)
                result.append(url)
    if not result:
        raise ValueError("请至少输入一条 URL")
    return result


def _parameters(url: str) -> dict[str, list[str]]:
    parsed = urlparse(url)
    values = parse_qs(parsed.query)
    # 单页应用常将查询参数放在 #/route?policyId=... 中。
    if "?" in parsed.fragment:
        fragment_query = parsed.fragment.split("?", 1)[1]
        for key, items in parse_qs(fragment_query).items():
            values.setdefault(key, []).extend(items)
    return values


def _first(values: dict[str, list[str]], *keys: str) -> str | None:
    lowered = {key.lower(): items for key, items in values.items()}
    for key in keys:
        items = lowered.get(key.lower()) or []
        if items and items[0].strip():
            return items[0].strip()
    return None


def source_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "zwdt.sh.gov.cn" or host.endswith(".zwdt.sh.gov.cn"):
        return "suishenban"
    if host == "shpolicy.ssme.sh.gov.cn" or host.endswith(".shpolicy.ssme.sh.gov.cn"):
        return "qifuyun"
    raise ValueError("无法根据 URL 域名识别来源；仅支持随申办或企服云详情页")


def candidate_from_url(url: str) -> PolicyCandidate:
    """将两个受支持来源的详情页 URL 转为可复用的抓取候选项。"""
    source_code = source_from_url(url)
    values = _parameters(url)
    path = urlparse(url).path.lower()
    if source_code == "suishenban":
        project_id = _first(values, "policyProjectId", "projectId")
        policy_id = _first(values, "policyId")
        generic_id = _first(values, "id")
        if not project_id and not policy_id and generic_id:
            if "project" in path:
                project_id = generic_id
            else:
                policy_id = generic_id
        source_id = project_id or policy_id
        if not source_id:
            raise ValueError("未在随申办 URL 中识别到 policyProjectId、projectId 或 policyId")
        return PolicyCandidate(
            source_code=source_code,
            source_item_id=source_id,
            project_name=url,
            detail_ref=source_id,
            original_url=url,
            raw={"url_input": url, "url_reference": "project" if project_id else "policy"},
        )
    if source_code == "qifuyun":
        policy_id = _first(values, "policyId", "id")
        if not policy_id:
            raise ValueError("未在企服云 URL 中识别到 policyId")
        return PolicyCandidate(
            source_code=source_code,
            source_item_id=policy_id,
            project_name=url,
            detail_ref=policy_id,
            original_url=url,
            raw={"url_input": url, "url_reference": "policy"},
        )
    raise ValueError(f"不支持的 URL 来源：{source_code}")
