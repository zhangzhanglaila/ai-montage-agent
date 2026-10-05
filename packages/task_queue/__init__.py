"""持久化任务队列（F3.1）。

纯标准库实现：SQLite 落盘 + 进程内 worker 线程，提供
**持久化 / 原子认领 / 失败退避重试 / 进程重启断点续跑** 四件事，
接口可平替 Celery/RQ。

快速用法::

    from packages.task_queue import get_store, get_worker

    store = get_store()                       # cache/tasks.db
    task = store.create("montage", {"a": 1})  # 落盘
    store.enqueue(task["id"])

    def handler(task_id, payload):
        store.handle(task_id)["progress"] = 50   # 赋值即持久化

    get_worker().register("montage", handler)
    get_worker().start()
"""

from .src.errors import TaskError
from .src.runtime import default_db_path, get_store, get_worker, reset_runtime
from .src.store import STATUSES, TERMINAL, TaskHandle, TaskStore
from .src.worker import TaskWorker

__all__ = [
    "TaskStore", "TaskHandle", "TaskWorker", "TaskError",
    "STATUSES", "TERMINAL",
    "get_store", "get_worker", "reset_runtime", "default_db_path",
]
