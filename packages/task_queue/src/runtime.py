"""进程内单例：Store / Worker。

WebUI 与 CLI 都从这里取同一个实例，避免各自 new 一份连接导致状态互相打架。

后端选择
--------
* 默认 **SQLite**（`cache/tasks.db`），离线可用；
* 设 ``MONTAGE_TASK_BACKEND=redis`` + ``MONTAGE_REDIS_URL=redis://host:6379/0``
  即切到 Redis（多进程/多机共享任务表），业务代码无需改动。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from .store import TaskStore
from .worker import TaskWorker

DEFAULT_DB = "cache/tasks.db"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

_lock = threading.RLock()
_store = None
_worker: Optional[TaskWorker] = None


def default_db_path() -> str:
    """SQLite 任务库位置，可用环境变量 ``MONTAGE_TASK_DB`` 覆盖。"""
    return os.environ.get("MONTAGE_TASK_DB") or DEFAULT_DB


def default_backend() -> str:
    """任务表后端：``sqlite``（默认）或 ``redis``。"""
    return (os.environ.get("MONTAGE_TASK_BACKEND") or "sqlite").strip().lower()


def default_redis_url() -> str:
    """Redis 连接串，可用 ``MONTAGE_REDIS_URL`` 覆盖。"""
    return os.environ.get("MONTAGE_REDIS_URL") or DEFAULT_REDIS_URL


def _is_redis_url(target) -> bool:
    return bool(target) and str(target).startswith(("redis://", "rediss://", "unix://"))


def _build_store(target: Optional[str] = None, backend: Optional[str] = None):
    backend = (backend or default_backend()).strip().lower()
    if backend == "redis" or _is_redis_url(target):
        from .redis_store import RedisTaskStore

        url = target if _is_redis_url(target) else default_redis_url()
        return RedisTaskStore(url)
    path = target or default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return TaskStore(path)


def get_store(target: Optional[str] = None, backend: Optional[str] = None):
    """取得任务表单例。``target`` 对 sqlite 是路径、对 redis 是 URL。"""
    global _store
    with _lock:
        if _is_redis_url(target) or (backend or default_backend()).lower() == "redis":
            want = f"redis:{target if _is_redis_url(target) else default_redis_url()}"
        else:
            want = str(target or default_db_path())
        if _store is None or _store.db_path != want:
            if _store is not None:
                _store.close()
            _store = _build_store(target, backend)
        return _store


def get_worker(target: Optional[str] = None, backend: Optional[str] = None) -> TaskWorker:
    """取得 worker 单例；若 store 目标变了则连同 worker 一起重建。"""
    global _worker
    with _lock:
        store = get_store(target, backend)
        if _worker is None or _worker.store is not store:
            if _worker is not None:
                _worker.stop()
            _worker = TaskWorker(store)
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
