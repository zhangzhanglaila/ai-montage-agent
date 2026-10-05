"""进程内单例：Store / Worker。

WebUI 与 CLI 都从这里取同一个实例，避免各自 new 一份连接导致 WAL 文件互相打架。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from .store import TaskStore
from .worker import TaskWorker

DEFAULT_DB = "cache/tasks.db"

_lock = threading.RLock()
_store: Optional[TaskStore] = None
_worker: Optional[TaskWorker] = None


def default_db_path() -> str:
    """任务库位置，可用环境变量 ``MONTAGE_TASK_DB`` 覆盖。"""
    return os.environ.get("MONTAGE_TASK_DB") or DEFAULT_DB


def get_store(db_path: Optional[str] = None) -> TaskStore:
    global _store
    with _lock:
        path = db_path or default_db_path()
        if _store is None or (db_path and _store.db_path != str(path)):
            if _store is not None:
                _store.close()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            _store = TaskStore(path)
        return _store


def get_worker(db_path: Optional[str] = None) -> TaskWorker:
    global _worker
    with _lock:
        if _worker is None:
            _worker = TaskWorker(get_store(db_path))
        elif db_path and _worker.store.db_path != str(db_path):
            _worker = TaskWorker(get_store(db_path))
        return _worker


def reset_runtime() -> None:
    """释放单例（测试用）。"""
    global _store, _worker
    with _lock:
        if _worker is not None:
            _worker.stop()
        if _store is not None:
            _store.close()
        _worker = None
        _store = None
