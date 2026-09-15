from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any


logger = logging.getLogger(__name__)


class EventStore:
    def __init__(self, database: Any | None = None, max_events: int = 2000):
        self._database = database
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max_events))
        self._condition = threading.Condition()

    def emit(self, run_id: str, event_type: str, message: str, **data: Any) -> None:
        created_at = datetime.now()
        persisted_id: int | None = None
        if self._database is not None:
            try:
                persisted_id = self._database.append_run_event(
                    run_id, event_type, message, data, created_at
                )
            except Exception:
                # 日志持久化故障不能反过来中断抓取任务，内存日志仍继续可用。
                logger.exception("run_id=%s event persistence failed", run_id)
        with self._condition:
            events = self._events[run_id]
            sequence = persisted_id if persisted_id is not None else (events[-1]["id"] + 1 if events else 1)
            events.append({
                "id": sequence, "type": event_type, "message": message,
                "time": created_at.isoformat(timespec="seconds"), **data,
            })
            self._condition.notify_all()

    def recent(self, run_id: str, limit: int = 300) -> list[dict[str, Any]]:
        if self._database is not None:
            try:
                return self._database.recent_run_events(run_id, limit)
            except Exception:
                logger.exception("run_id=%s event history read failed", run_id)
        with self._condition:
            return list(self._events[run_id])[-limit:]

    def wait_after(self, run_id: str, last_id: int, timeout: float = 15) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._database is not None:
                    try:
                        items = self._database.list_run_events(run_id, last_id)
                        if items:
                            return items
                    except Exception:
                        logger.exception("run_id=%s event polling failed", run_id)
                items = [item for item in self._events[run_id] if item["id"] > last_id]
                if items or time.monotonic() >= deadline:
                    return items
                self._condition.wait(max(0, deadline - time.monotonic()))

    @staticmethod
    def sse(event: dict[str, Any]) -> str:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        return f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"
