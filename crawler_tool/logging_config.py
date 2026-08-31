from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, Settings


def configure_logging(settings: Settings) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    if not any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        handler = RotatingFileHandler(
            LOG_DIR / "crawler.log", maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
