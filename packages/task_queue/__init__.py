"""持久化任务队列（F3.1）。

提供 **持久化 / 原子认领 / 失败退避重试 / 进程重启断点续跑** 四件事，
接口可平替 Celery/RQ。两种后端，**同一个接口**：

* ``TaskStore``      —— SQLite（默认，纯标准库，离线可用）；
* ``RedisTaskStore`` —— Redis（多进程/多机共享任务表）。

切换后端（业务代码零改动）::

    MONTAGE_TASK_BACKEND=redis  MONTAGE_REDIS_URL=redis://127.0.0.1:6379/0

快速用法::

    from packages.task_queue import get_store, get_worker

    store = get_store()                       # 按环境变量选后端
    task = store.create("montage", {"a": 1})
    store.enqueue(task["id"])

    def handler(task_id, payload):
        store.handle(task_id)["progress"] = 50   # 赋值即持久化

    get_worker().register("montage", handler)
    get_worker().start()

注意：Redis 后端依赖 redis-py；**redis-py >= 8 默认 RESP3 会发 HELLO**，
老 Redis（如 3.0）不支持，故 ``RedisTaskStore`` 内部固定 ``protocol=2``。
"""

from .src.errors import TaskError
from .src.redis_store import RedisTaskStore, redis_available
from .src.runtime import (
    default_backend,
    default_db_path,
    default_redis_url,
    get_store,
    get_worker,
    reset_runtime,
)
from .src.store import STATUSES, TERMINAL, TaskHandle, TaskStore
from .src.worker import TaskWorker

__all__ = [
    "TaskStore", "RedisTaskStore", "TaskHandle", "TaskWorker", "TaskError",
    "redis_available",
    "STATUSES", "TERMINAL",
    "get_store", "get_worker", "reset_runtime",
    "default_db_path", "default_backend", "default_redis_url",
]
