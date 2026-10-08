"""进程内任务进度跟踪（供 /tasks 与前端进度条使用）。

每个任务一个具名 ``Progress`` 实例，跨线程安全。字段：
    running / phase / total / done / percent / message / error / started_at / finished_at
"""
import threading
import time


class Progress:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self._d = {
            "name": name,
            "running": False,
            "phase": "",
            "total": 0,
            "done": 0,
            "message": "",
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    def start(self, phase: str = "", total: int = 0) -> None:
        with self._lock:
            self._d.update(
                running=True, phase=phase, total=total, done=0,
                message="", error=None, started_at=int(time.time()), finished_at=None,
            )

    def update(self, phase=None, total=None, done=None, inc=0, message=None) -> None:
        with self._lock:
            if phase is not None:
                self._d["phase"] = phase
            if total is not None:
                self._d["total"] = total
            if done is not None:
                self._d["done"] = done
            elif inc:
                self._d["done"] += inc
            if message is not None:
                self._d["message"] = message

    def finish(self, message: str = "", error: str = None) -> None:
        with self._lock:
            self._d.update(
                running=False, finished_at=int(time.time()),
                message=message or self._d.get("message", ""), error=error,
            )

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._d)
        total = d.get("total") or 0
        if d.get("running"):
            d["percent"] = int(d.get("done", 0) / total * 100) if total else 0
        else:
            d["percent"] = 100 if d.get("finished_at") else 0
        return d


_registry: dict[str, Progress] = {}
_reg_lock = threading.Lock()


def get(name: str) -> Progress:
    with _reg_lock:
        if name not in _registry:
            _registry[name] = Progress(name)
        return _registry[name]


def snapshot_all() -> dict:
    with _reg_lock:
        items = list(_registry.items())
    return {name: p.snapshot() for name, p in items}
