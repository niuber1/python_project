from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from ..models import PolicyArticle, PolicyCandidate


class AdapterError(RuntimeError):
    pass


class SourceEmptyError(AdapterError):
    pass


class CrawlerAdapter(ABC):
    source_code: str

    def __init__(self, client: httpx.Client):
        self.client = client

    @abstractmethod
    def discover(self) -> list[PolicyCandidate]: ...

    @abstractmethod
    def fetch(self, candidate: PolicyCandidate) -> PolicyArticle: ...

    def health_url(self) -> str:
        raise NotImplementedError
