"""任务工作线程：从 :class:`~packages.task_queue.src.store.TaskStore` 取任务并执行。

形态说明
--------
一个进程内的**后台线程**（daemon），轮询认领任务。相比 Celery 的独立 worker
进程，它足够支撑"WebUI 单进程 + 一个 worker"的部署，且零外部依赖；进程重启时
``start()`` 会先 ``resume_stale()`` 把上次被杀的任务捞回来续跑。

处理器契约::

    def handler(task_id: str, payload: dict) -> None:
        # 需要汇报进度就用 store.handle(task_id) 赋值
        # 失败就抛异常（TaskError(retryable=False) 表示不要重试）

处理器**抛出的异常**才会触发重试；自己把状态写成 ``error`` 而不抛异常不会被
重试（但 :meth:`TaskWorker.run_once` 有兜底，会补一次 mark_error）。
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Callable, Dict, Optional

from .errors import TaskError
from .store import TERMINAL, TaskStore

Handler = Callable[[str, dict], None]


class TaskWorker:
    def __init__(self, store: TaskStore, *, poll_interval: float = 0.5,
                 stale_seconds: float = 600.0, clock=time.time,
                 sleeper=time.sleep, logger=print, traceback_printer=None):
        self.store = store
        self.poll_interval = float(poll_interval)
        self.stale_seconds = float(stale_seconds)
        self._clock = clock
        self._sleep = sleeper
        self._log = logger
        self._tb = traceback_printer or traceback.print_exc
        self._handlers: Dict[str, Handler] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.processed = 0

    # ------------------------------------------------------------ 处理器注册
    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    @property
    def handlers(self) -> Dict[str, Handler]:
        return dict(self._handlers)

    # ---------------------------------------------------------------- 单次执行
    def run_once(self) -> bool:
        """认领并执行一个任务。返回是否真的处理了任务。"""
        task = self.store.claim_next(self._clock())
        if not task:
            return False
        tid, kind = task["id"], task.get("kind") or "montage"
        handler = self._handlers.get(kind)
        if handler is None:
            self.store.mark_error(tid, f"未注册的处理器: {kind}", retryable=False)
            self.processed += 1
            return True

        try:
            handler(tid, task.get("payload") or {})
        except TaskError as e:
            self.store.mark_error(tid, str(e), retryable=e.retryable,
                                  now=self._clock())
        except Exception as e:  # noqa: BLE001 - 队列必须兜住一切，否则任务会卡在 running
            self._log(f"[TaskWorker] 任务 {tid} 异常: {type(e).__name__}: {e}")
            self._tb()
            self.store.mark_error(tid, f"{type(e).__name__}: {e}",
                                  retryable=True, now=self._clock())
        else:
            row = self.store.get(tid) or {}
            if row.get("status") not in TERMINAL:
                self.store.mark_done(tid, output_path=row.get("output_path"))
        self.processed += 1
        return True

    # ---------------------------------------------------------------- 生命周期
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.run_once()
            except Exception as e:  # noqa: BLE001 - 轮询本身绝不能被异常打断
                self._log(f"[TaskWorker] 轮询异常（继续）: {type(e).__name__}: {e}")
            if not worked:
                self._stop.wait(self.poll_interval)

    def start(self) -> None:
        """启动后台线程；启动前先把上次进程残留的 running 任务续跑。"""
        if self.running:
            return
        try:
            info = self.store.resume_stale(self.stale_seconds, now=self._clock())
            if info.get("resumed") or info.get("exhausted"):
                self._log(f"[TaskWorker] 续跑: 重新入队 {info['resumed']} 个，"
                          f"重试耗尽转失败 {info['exhausted']} 个")
        except Exception as e:  # noqa: BLE001
            self._log(f"[TaskWorker] 续跑检查失败（忽略）: {e}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="task-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    def drain(self, max_tasks: int = 1000) -> int:
        """把当前队列跑空（阻塞式，测试/CLI 用）。返回处理的任务数。"""
        n = 0
        while n < max_tasks and self.run_once():
            n += 1
        return n
