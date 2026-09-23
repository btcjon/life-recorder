from __future__ import annotations

import threading
import time
from collections import deque


class Limiter:
    """30 authorized read requests per client id per rolling 60 seconds."""

    def __init__(self, limit: int = 30, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, client_id: str, now: float | None = None) -> tuple[bool, int]:
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(client_id, deque())
            while hits and moment - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = max(1, int(self.window - (moment - hits[0])) + 1)
                return False, retry
            hits.append(moment)
            return True, 0
