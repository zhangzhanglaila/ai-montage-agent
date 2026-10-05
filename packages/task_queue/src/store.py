"""SQLite 持久化任务存储（纯标准库，不依赖 Redis/Celery）。

为什么不用 Celery/RQ
--------------------
本项目跑在离线环境，没装 Redis，也不该为了"任务能重试"就强绑一个中间件。
这里用 ``sqlite3`` 实现**等价的最小语义**，接口刻意做成可替换的：

* 持久化：任务表落盘，进程重启不丢；
* 原子认领：``claim_next()`` 用 ``BEGIN IMMEDIATE`` 事务，多线程/多进程下
  同一任务不会被重复领取；
* 失败重试：``mark_error()`` 按指数退避把任务重新入队，超过 ``max_attempts``
  才落 ``error``；
* 断点续跑：``resume_stale()`` 把"进程被杀导致卡在 running"的任务捞回队列。

将来若真接 Celery，只要实现同名方法即可，调用方（WebUI / CLI）不用改。

线程安全
--------
单连接 + ``check_same_thread=False`` + ``RLock``，WAL 模式。适合
"一个 WebUI 进程 + 一个 worker 线程"的部署形态；跨进程并发请自行加锁或换后端。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# 任务状态机：pending -> queued -> running -> done / error / canceled
STATUSES = ("pending", "queued", "running", "done", "error", "canceled")
TERMINAL = ("done", "error", "canceled")

# 真实列（其余 kwarg 一律塞进 extra JSON）
_COLUMNS = (
    "id", "kind", "status", "progress", "message", "payload", "result",
    "output_path", "error", "attempts", "max_attempts", "priority", "seq",
    "next_retry_at", "created_at", "updated_at", "started_at", "finished_at",
)
_INT_FIELDS = {"progress", "attempts", "max_attempts", "priority", "seq"}
_JSON_FIELDS = {"payload", "result", "extra"}
_REAL_FIELDS = {"result", "output_path", "error", "extra"}
_SCALAR_FIELDS = {"status", "progress", "message", "priority", "max_attempts"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    kind          TEXT    NOT NULL DEFAULT 'montage',
    status        TEXT    NOT NULL DEFAULT 'pending',
    progress      INTEGER NOT NULL DEFAULT 0,
    message       TEXT    NOT NULL DEFAULT '',
    payload       TEXT    NOT NULL DEFAULT '{}',
    result        TEXT,
    output_path   TEXT,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    priority      INTEGER NOT NULL DEFAULT 0,
    seq           INTEGER,
    next_retry_at REAL,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    extra         TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_pick ON tasks(status, priority DESC, seq ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""


def _dumps(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class TaskStore:
    """SQLite 任务表。"""

    def __init__(self, db_path: str | Path = "cache/tasks.db",
                 backoff_base: float = 2.0, backoff_cap: float = 60.0,
                 clock=time.time):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self._clock = clock
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ 基础读写
    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = {k: row[k] for k in row.keys() if k != "extra"}
        data["payload"] = _loads(row["payload"], {})
        data["result"] = _loads(row["result"], None)
        extra = _loads(row["extra"], {})
        if isinstance(extra, dict):
            for k, v in extra.items():
                data.setdefault(k, v)
        return data

    def create(self, kind: str = "montage", payload: Optional[dict] = None, *,
               task_id: Optional[str] = None, max_attempts: int = 3,
               priority: int = 0, status: str = "pending",
               message: str = "任务已创建，等待执行") -> Dict[str, Any]:
        """新建任务。``task_id`` 可显式指定（幂等/测试用）。"""
        tid = task_id or uuid.uuid4().hex
        now = self._clock()
        with self._lock:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM tasks").fetchone()["s"]
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (id, kind, status, progress, message, payload, attempts,"
                "  max_attempts, priority, seq, created_at, updated_at, extra)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, kind, status, 0, message, _dumps(payload or {}),
                 0, int(max_attempts), int(priority), seq, now, now, "{}"),
            )
            self._conn.commit()
        return self.get(tid)  # type: ignore[return-value]

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, status: Optional[str] = None, kind: Optional[str] = None,
             limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update(self, task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """更新任务字段。非真实列的键写入 ``extra`` JSON。"""
        if not fields:
            return self.get(task_id)
        now = self._clock()
        cols: Dict[str, Any] = {"updated_at": now}
        extra_patch: Dict[str, Any] = {}
        for key, value in fields.items():
            if key in _COLUMNS:
                if key in _INT_FIELDS:
                    value = int(value or 0)
                elif key in _JSON_FIELDS:
                    value = _dumps(value)
                cols[key] = value
            elif key == "updated_at":
                continue
            else:
                extra_patch[key] = value

        with self._lock:
            if extra_patch:
                row = self._conn.execute(
                    "SELECT extra FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row is None:
                    return None
                extra = _loads(row["extra"], {})
                if not isinstance(extra, dict):
                    extra = {}
                extra.update(extra_patch)
                cols["extra"] = _dumps(extra)
            assignments = ", ".join(f"{k} = ?" for k in cols)
            self._conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                list(cols.values()) + [task_id],
            )
            self._conn.commit()
        return self.get(task_id)

    # -------------------------------------------------------------- 队列语义
    def enqueue(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.update(task_id, status="queued", next_retry_at=None)

    def _backoff(self, attempts: int) -> float:
        return min(self.backoff_base * (2 ** max(0, attempts - 1)), self.backoff_cap)

    def claim_next(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """原子地认领下一个可执行任务并置为 running（attempts+1）。

        ``BEGIN IMMEDIATE`` 保证并发下同一行只会被一个调用者拿到。
        """
        now = self._clock() if now is None else now
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT * FROM tasks"
                    " WHERE status = 'queued'"
                    "   AND (next_retry_at IS NULL OR next_retry_at <= ?)"
                    " ORDER BY priority DESC, seq ASC LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    cur.execute("COMMIT")
                    return None
                cur.execute(
                    "UPDATE tasks SET status='running', attempts=attempts+1,"
                    " started_at=COALESCE(started_at, ?), updated_at=?,"
                    " next_retry_at=NULL WHERE id=?",
                    (now, now, row["id"]),
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return self.get(row["id"])

    def mark_done(self, task_id: str, output_path: Optional[str] = None,
                  result: Any = None) -> Optional[Dict[str, Any]]:
        now = self._clock()
        fields: Dict[str, Any] = {
            "status": "done", "progress": 100, "message": "任务完成",
            "finished_at": now, "next_retry_at": None, "error": None,
        }
        if output_path is not None:
            fields["output_path"] = output_path
        if result is not None:
            fields["result"] = result
        return self.update(task_id, **fields)

    def mark_error(self, task_id: str, error: str, *, retryable: bool = True,
                   now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """标记失败：可重试且未超次数则退避后重新入队，否则落 error。"""
        now = self._clock() if now is None else now
        row = self.get(task_id)
        if row is None:
            return None
        attempts = int(row.get("attempts") or 0)
        max_attempts = int(row.get("max_attempts") or 1)
        if retryable and attempts < max_attempts:
            delay = self._backoff(attempts)
            return self.update(
                task_id, status="queued", error=error,
                next_retry_at=now + delay, message=f"失败，{delay:.1f}s 后重试"
                                                   f"（第 {attempts}/{max_attempts} 次）",
            )
        return self.update(
            task_id, status="error", error=error,
            message=f"任务失败：{error}", finished_at=now, next_retry_at=None,
        )

    def retry(self, task_id: str) -> Optional[Dict[str, Any]]:
        """手动重试：清零尝试次数并重新入队。"""
        return self.update(task_id, status="queued", attempts=0, error=None,
                           next_retry_at=None, finished_at=None,
                           message="手动重新入队")

    def resume_stale(self, stale_seconds: float = 600.0,
                     now: Optional[float] = None) -> Dict[str, int]:
        """把卡在 running 且超时的任务捞回队列（进程被杀后的断点续跑）。

        返回 ``{"resumed": n, "exhausted": m}``：
        * ``resumed`` —— 重试次数未用尽，重新入队；
        * ``exhausted`` —— 重试次数已用尽，直接落 error（避免无限崩溃循环）。
        """
        now = self._clock() if now is None else now
        cutoff = now - float(stale_seconds)
        resumed = exhausted = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            rows = cur.execute(
                "SELECT id, attempts, max_attempts FROM tasks"
                " WHERE status='running' AND updated_at < ?", (cutoff,),
            ).fetchall()
            for r in rows:
                if int(r["attempts"] or 0) < int(r["max_attempts"] or 0):
                    cur.execute(
                        "UPDATE tasks SET status='queued', updated_at=?,"
                        " message='进程中断，已重新入队' WHERE id=?",
                        (now, r["id"]),
                    )
                    resumed += 1
                else:
                    cur.execute(
                        "UPDATE tasks SET status='error', updated_at=?,"
                        " finished_at=?, error=?, message=? WHERE id=?",
                        (now, now, "进程中断且重试次数已用尽",
                         "进程中断且重试次数已用尽", r["id"]),
                    )
                    exhausted += 1
            cur.execute("COMMIT")
        return {"resumed": resumed, "exhausted": exhausted}

    def cancel(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.update(task_id, status="canceled", finished_at=self._clock(),
                           next_retry_at=None, message="已取消")

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        out = {s: 0 for s in STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        return out

    def purge(self, statuses=TERMINAL) -> int:
        if not statuses:
            return 0
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM tasks WHERE status IN ({marks})", tuple(statuses))
            self._conn.commit()
            return cur.rowcount

    # ----------------------------------------------------------- dict 适配层
    def handle(self, task_id: str) -> "TaskHandle":
        """返回 dict 风格句柄：``handle['progress'] = 40`` 即写库。"""
        return TaskHandle(self, task_id)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class TaskHandle:
    """把任务表包装成"赋值即持久化"的 dict 风格对象。

    这样老的流水线代码（大量 ``task["progress"] = ...``）可以**一行不改**地
    跑在持久化存储上：只要把 ``task = _tasks[id]`` 换成 ``task = store.handle(id)``。
    """

    def __init__(self, store: TaskStore, task_id: str):
        self._store = store
        self._id = task_id

    @property
    def id(self) -> str:
        return self._id

    def __setitem__(self, key: str, value: Any) -> None:
        self._store.update(self._id, **{key: value})

    def __getitem__(self, key: str) -> Any:
        row = self._store.get(self._id) or {}
        if key not in row:
            raise KeyError(key)
        return row[key]

    def get(self, key: str, default: Any = None) -> Any:
        return (self._store.get(self._id) or {}).get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in (self._store.get(self._id) or {})

    def to_dict(self) -> Dict[str, Any]:
        return self._store.get(self._id) or {}
