from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any


class EventStore:
    def __init__(self, max_events: int = 2000):
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=max_events))
        self._condition = threading.Condition()

    def emit(self, run_id: str, event_type: str, message: str, **data: Any) -> None:
        with self._condition:
            events = self._events[run_id]
            sequence = events[-1]["id"] + 1 if events else 1
            events.append({
                "id": sequence, "type": event_type, "message": message,
                "time": datetime.now().isoformat(timespec="seconds"), **data,
            })
            self._condition.notify_all()

    def wait_after(self, run_id: str, last_id: int, timeout: float = 15) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                items = [item for item in self._events[run_id] if item["id"] > last_id]
                if items or time.monotonic() >= deadline:
                    return items
                self._condition.wait(max(0, deadline - time.monotonic()))

    @staticmethod
    def sse(event: dict[str, Any]) -> str:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        return f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"
