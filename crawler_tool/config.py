from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
LOG_DIR = ROOT_DIR / "logs"
TARGET_BASE_ID = "395cbf7152564184ad7c701beaf80cc5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="CRAWLER_",
        case_sensitive=False,
        extra="ignore",
    )

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = ""
    db_password: str = ""
    db_name: str = "dsfa_policy"
    kms_db_name: str = "kms_kms"
    kms_base_url: str = "http://10.1.3.144:20002"
    kms_path: str = "/kms/api/etl/dg/crawlerToBase"
    kms_update_path: str = "/kms/openapi/knowledge/document/update"
    kms_auth_check_path: str = "/kms/openapi/knowledge/stat/overview"
    kms_token_path: str = "/platform/api/v2/auth/getToken"
    kms_application_key: str = ""
    kms_application_secret: str = ""
    # @Verification 使用独立的 OpenAPI 集成凭据，不是浏览器登录会话。
    kms_access_token: str = ""
    kms_authorization: str = ""
    kms_cookie: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    admin_user: str = ""
    admin_password: str = ""
    request_timeout_seconds: float = Field(default=60, gt=0, le=600)
    kms_timeout_seconds: float = Field(default=300, gt=0, le=900)
    item_delay_seconds: float = Field(default=0.2, ge=0, le=30)
    max_items_per_task: int = Field(default=0, ge=0)
    schedule_hour: int = Field(default=1, ge=0, le=23)
    schedule_minute: int = Field(default=0, ge=0, le=59)
    log_level: str = "INFO"

    @property
    def kms_url(self) -> str:
        return f"{self.kms_base_url.rstrip('/')}/{self.kms_path.lstrip('/')}"

    @property
    def kms_update_url(self) -> str:
        return f"{self.kms_base_url.rstrip('/')}/{self.kms_update_path.lstrip('/')}"

    @property
    def kms_auth_check_url(self) -> str:
        return f"{self.kms_base_url.rstrip('/')}/{self.kms_auth_check_path.lstrip('/')}"

    @property
    def kms_token_url(self) -> str:
        return f"{self.kms_base_url.rstrip('/')}/{self.kms_token_path.lstrip('/')}"

    def ensure_directories(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


TASKS = {
    "suishenban_declare": {
        "code": "suishenban_declare",
        "name": "随申办—申报类政策抓取",
        "source_code": "suishenban",
        "source_name": "上海一网通办",
        "base_id": TARGET_BASE_ID,
        "rule": "申报类：申报期限为进行中或即将开始，且免申为否",
    },
    "qifuyun_declare": {
        "code": "qifuyun_declare",
        "name": "企服云—申报类政策抓取",
        "source_code": "qifuyun",
        "source_name": "上海市企业服务云",
        "base_id": TARGET_BASE_ID,
        "rule": "上海市、申报中；政策原文为空时跳过",
    },
}
