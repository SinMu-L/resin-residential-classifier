"""近期已返回 IP 的短生命周期环形缓冲（PRD 5.5.3 / avoid_recent）。

- 仅内存、不持久化；超期自动移出。
- 用于 /nodes/random?avoid_recent=true，提升高频轮询时的分布均匀度。
"""
import threading
import time
from collections import deque


class RecentBuffer:
    def __init__(self, ttl: int = 60, maxlen: int = 1000):
        self.ttl = ttl
        self._dq = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, key: str) -> None:
        with self._lock:
            self._dq.append((key, time.time()))

    def _purge(self, now: float) -> None:
        while self._dq and now - self._dq[0][1] > self.ttl:
            self._dq.popleft()

    def contains(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._purge(now)
            return any(k == key for k, _ in self._dq)

    def keys(self) -> list:
        """返回当前未过期的全部 key（用于 avoid_recent 排除）。"""
        now = time.time()
        with self._lock:
            self._purge(now)
            return [k for k, _ in self._dq]

    def size(self) -> int:
        now = time.time()
        with self._lock:
            self._purge(now)
            return len(self._dq)


# 全局单例（进程级共享）
from .config import settings  # noqa: E402
recent_buffer = RecentBuffer(ttl=settings.recent_ttl, maxlen=settings.recent_maxlen)
