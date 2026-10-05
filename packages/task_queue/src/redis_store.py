"""Redis 版任务存储 —— 与 :class:`~packages.task_queue.src.store.TaskStore` **同接口**。

用途
----
F3.1 默认用 SQLite（离线可用）。当你有 Redis、想让**多个进程/多台机器**共享同一个
任务表时，把后端切到 Redis 即可：WebUI、CLI、worker 代码一行不用改。
切换方式：``MONTAGE_TASK_BACKEND=redis`` + ``MONTAGE_REDIS_URL=redis://127.0.0.1:6379/0``。

数据结构
--------
* ``{prefix}:task:<id>``  HASH，任务全部字段（JSON 字段存字符串）。
* ``{prefix}:queued``     ZSET，仅含 status=queued 的任务。score 编码排队次序：
  ``-priority * 1e12 + seq`` → 升序弹出即「优先级高者先、同级按入队先后」。
* ``{prefix}:running``    ZSET，score=updated_at，供 ``resume_stale`` 高效按时间捞取。
* ``{prefix}:ids``        ZSET，score=created_at，供列表/统计遍历。
* ``{prefix}:seq``        STRING 自增计数器，保证 FIFO 次序。

原子性
------
``claim_next`` 用 **Lua 脚本**（``EVAL``）在服务端原子完成「找→校验退避→改状态 +
attempts+1 + 移出 queued + 记入 running」，因此多进程并发也不会重复认领。

兼容性（踩坑记录）
------------------
* **redis-py ≥ 8 默认走 RESP3**，握手会发 ``HELLO``，而老 Redis（如 3.0.504）不认识
  → ``ResponseError: unknown command 'HELLO'``。必须显式 ``protocol=2``。
* **多字段 HSET 是 Redis 4.0 才有的**（``HSET key f1 v1 f2 v2``）。在 3.x 上只认
  ``HSET key field value`` 单字段形式，而 ``redis-py`` 的 ``hset(mapping=...)`` 恰好生成
  多字段形式 → 报 ``wrong number of arguments for 'hset'``。因此本模块统一走
  :func:`_pipe_hset` **逐字段 HSET**（Lua 脚本内同理，拆成多条单字段 HSET）。
* ``resume_stale`` 里用到的字段/命令（HGETALL/HSET/ZADD/ZRANGEBYSCORE/EVAL）在
  Redis 2.6+ 即有，故 3.0 可用。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .store import (
    STATUSES,
    TERMINAL,
    TaskHandle,
    _COLUMNS,
    _INT_FIELDS,
    _JSON_FIELDS,
)

try:  # redis 是可选依赖
    import redis as _redis
except ImportError:  # pragma: no cover
    _redis = None

# 可空字段：空串在 Redis 里代表 None（Hash 不能存 None）
_NULLABLE_STR = {"result", "output_path", "error", "started_at", "finished_at"}
_NULLABLE_FLOAT = {"next_retry_at"}
_FLOAT_FIELDS = {"created_at", "updated_at"}


def _pipe_hset(pipe, key: str, mapping: Dict[str, Any]) -> None:
    """跨版本安全的批量写 Hash：逐字段 HSET，兼容 Redis < 4.0。

    ``redis-py`` 的 ``hset(key, mapping=...)`` 会发多字段 ``HSET``，Redis 3.x 不支持，
    故一律拆成单字段 ``HSET key field value``（管道内多条，依然是 MULTI/EXEC 原子）。
    """
    for field, value in (mapping or {}).items():
        pipe.hset(key, field, value)


# 原子认领：ZSET 升序扫描，跳过未到退避时刻的，认领第一个合格的
_CLAIM_LUA = """
local ids = redis.call('ZRANGE', KEYS[1], 0, tonumber(ARGV[2]))
local now = tonumber(ARGV[1])
for i = 1, #ids do
  local id = ids[i]
  local key = KEYS[2] .. id
  local nra = redis.call('HGET', key, 'next_retry_at')
  local ready = true
  if nra and nra ~= '' then
    ready = (tonumber(nra) <= now)
  end
  if ready then
    redis.call('ZREM', KEYS[1], id)
    redis.call('HSET', key, 'status', 'running')
    redis.call('HSET', key, 'updated_at', ARGV[1])
    redis.call('HSET', key, 'next_retry_at', '')
    redis.call('HINCRBY', key, 'attempts', 1)
    local st = redis.call('HGET', key, 'started_at')
    if (not st) or st == '' then
      redis.call('HSET', key, 'started_at', ARGV[1])
    end
    redis.call('ZADD', KEYS[3], ARGV[1], id)
    return id
  end
end
return false
"""


def redis_available(url: Optional[str] = None) -> bool:
    """redis-py 是否可用；传了 ``url`` 则顺带探测该实例是否连得上。

    不传参只判断依赖是否安装（纯本地判断，永不抛错）。传参时任何异常都当作
    "不可用"，方便测试里做 ``@pytest.mark.skipif(not redis_available(URL))`` 门禁。
    """
    if _redis is None:
        return False
    if not url:
        return True
    try:
        client = _redis.Redis.from_url(
            url, protocol=2, decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        return False


class RedisTaskStore:
    """Redis 任务表（TaskStore 的 drop-in 替代）。"""

    def __init__(self, url: str = "redis://127.0.0.1:6379/0", *,
                 prefix: str = "tq", backoff_base: float = 2.0,
                 backoff_cap: float = 60.0, clock=time.time, **redis_kwargs):
        if _redis is None:  # pragma: no cover
            raise RuntimeError("未安装 redis-py，无法使用 Redis 后端（pip install redis）")
        self.db_path = f"redis:{url}"          # 让 runtime 的"路径变了就重建"逻辑可用
        self.url = url
        self.prefix = prefix.rstrip(":")
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self._clock = clock
        # protocol=2 是关键：redis-py>=8 默认 RESP3 会发 HELLO，老 Redis 不支持
        redis_kwargs.setdefault("protocol", 2)
        redis_kwargs.setdefault("decode_responses", True)
        redis_kwargs.setdefault("socket_connect_timeout", 5)
        redis_kwargs.setdefault("socket_timeout", 30)
        self._r = _redis.Redis.from_url(url, **redis_kwargs)

    # ------------------------------------------------------------------ 基础
    def _k(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    def _task_key(self, task_id: str) -> str:
        return self._k("task", task_id)

    def ping(self) -> bool:
        return bool(self._r.ping())

    def _score(self, row: Dict[str, Any]) -> float:
        """排队次序编码：优先级高者 score 更小；同级按 seq 升序。"""
        try:
            seq = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        try:
            pri = int(row.get("priority") or 0)
        except (TypeError, ValueError):
            pri = 0
        return float(seq) - float(pri) * 1e12

    def _encode(self, key: str, value: Any) -> str:
        if value is None:
            return ""
        if key in _JSON_FIELDS:
            return json.dumps(value, ensure_ascii=False)
        if key in _INT_FIELDS:
            return str(int(value or 0))
        if key in _FLOAT_FIELDS:
            return repr(float(value))
        return str(value)

    def _decode(self, raw: Dict[str, str]) -> Optional[Dict[str, Any]]:
        if not raw:
            return None
        data: Dict[str, Any] = {}
        for key, text in raw.items():
            if key in _JSON_FIELDS:
                try:
                    data[key] = json.loads(text) if text else ({} if key != "result" else None)
                except (TypeError, ValueError):
                    data[key] = {} if key != "result" else None
            elif key in _INT_FIELDS:
                try:
                    data[key] = int(text or 0)
                except (TypeError, ValueError):
                    data[key] = 0
            elif key in _NULLABLE_FLOAT:
                data[key] = float(text) if text else None
            elif key in _FLOAT_FIELDS:
                data[key] = float(text) if text else 0.0
            elif key in _NULLABLE_STR:
                data[key] = text or None
            else:
                data[key] = text
        # 保证列齐全
        data.setdefault("payload", {})
        data.setdefault("result", None)
        data.setdefault("extra", {})
        if not isinstance(data["payload"], dict):
            data["payload"] = {}
        extra = str(raw.get("extra") or "")
        if extra:
            try:
                patch = json.loads(extra)
                if isinstance(patch, dict):
                    for k, v in patch.items():
                        data.setdefault(k, v)
            except (TypeError, ValueError):
                pass
        return data

    def _sync_index(self, task_id: str, row: Dict[str, Any]) -> None:
        """让 queued/running 两个 ZSET 与任务状态保持一致。"""
        status = row.get("status")
        pipe = self._r.pipeline()
        if status == "queued":
            pipe.zadd(self._k("queued"), {task_id: self._score(row)})
        else:
            pipe.zrem(self._k("queued"), task_id)
        if status == "running":
            pipe.zadd(self._k("running"), {task_id: float(row.get("updated_at") or 0)})
        else:
            pipe.zrem(self._k("running"), task_id)
        pipe.execute()

    # ------------------------------------------------------------ 基础读写
    def create(self, kind: str = "montage", payload: Optional[dict] = None, *,
               task_id: Optional[str] = None, max_attempts: int = 3,
               priority: int = 0, status: str = "pending",
               message: str = "任务已创建，等待执行") -> Dict[str, Any]:
        tid = task_id or uuid.uuid4().hex
        now = self._clock()
        seq = int(self._r.incr(self._k("seq")))
        fields = {
            "id": tid, "kind": kind, "status": status, "progress": 0,
            "message": message, "payload": payload or {}, "result": None,
            "output_path": None, "error": None, "attempts": 0,
            "max_attempts": int(max_attempts), "priority": int(priority),
            "seq": seq, "next_retry_at": None, "extra": {},
            "created_at": now, "updated_at": now, "started_at": None,
            "finished_at": None,
        }
        mapping = {k: self._encode(k, v) for k, v in fields.items()}
        pipe = self._r.pipeline()
        _pipe_hset(pipe, self._task_key(tid), mapping)
        pipe.zadd(self._k("ids"), {tid: now})
        pipe.execute()
        row = self.get(tid)
        self._sync_index(tid, row)
        return self.get(tid)  # type: ignore[return-value]

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        raw = self._r.hgetall(self._task_key(task_id))
        return self._decode(raw) if raw else None

    def list(self, status: Optional[str] = None, kind: Optional[str] = None,
             limit: int = 200) -> List[Dict[str, Any]]:
        ids = self._r.zrevrange(self._k("ids"), 0, -1)
        out: List[Dict[str, Any]] = []
        for tid in ids:
            row = self.get(tid)
            if row is None:
                continue
            if status and row.get("status") != status:
                continue
            if kind and row.get("kind") != kind:
                continue
            out.append(row)
            if len(out) >= int(limit):
                break
        return out

    def update(self, task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get(task_id)
        if self._r.exists(self._task_key(task_id)) == 0:
            return None
        now = self._clock()
        mapping: Dict[str, str] = {}
        extra_patch: Dict[str, Any] = {}
        for key, value in fields.items():
            if key in _COLUMNS:
                mapping[key] = self._encode(key, value)
            else:
                extra_patch[key] = value
        # 与 SQLite 版保持一致：调用方显式传 updated_at 时以它为准（供测试/断点续跑
        # 复用），未传才回落当前时钟。``updated_at`` 本身在 _COLUMNS 内，故上面已写入。
        mapping.setdefault("updated_at", self._encode("updated_at", now))
        if extra_patch:
            row = self.get(task_id) or {}
            extra = dict(row.get("extra") or {})
            extra.update(extra_patch)
            mapping["extra"] = self._encode("extra", extra)
        pipe = self._r.pipeline()
        _pipe_hset(pipe, self._task_key(task_id), mapping)
        pipe.execute()
        row = self.get(task_id)
        self._sync_index(task_id, row)
        return row

    # -------------------------------------------------------------- 队列语义
    def enqueue(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.update(task_id, status="queued", next_retry_at=None)

    def _backoff(self, attempts: int) -> float:
        return min(self.backoff_base * (2 ** max(0, attempts - 1)), self.backoff_cap)

    def claim_next(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        now = self._clock() if now is None else now
        tid = self._r.eval(
            _CLAIM_LUA,
            3,
            self._k("queued"),
            self._k("task") + ":",
            self._k("running"),
            repr(float(now)),
            200,
        )
        if not tid:
            return None
        return self.get(str(tid))

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
        now = self._clock() if now is None else now
        row = self.get(task_id)
        if row is None:
            return None
        attempts = int(row.get("attempts") or 0)
        max_attempts = int(row.get("max_attempts") or 1)
        if retryable and attempts < max_attempts:
            delay = self._backoff(attempts)
            return self.update(
                task_id, status="queued", error=error, next_retry_at=now + delay,
                message=f"失败，{delay:.1f}s 后重试（第 {attempts}/{max_attempts} 次）",
            )
        return self.update(
            task_id, status="error", error=error,
            message=f"任务失败：{error}", finished_at=now, next_retry_at=None,
        )

    def retry(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.update(task_id, status="queued", attempts=0, error=None,
                           next_retry_at=None, finished_at=None,
                           message="手动重新入队")

    def resume_stale(self, stale_seconds: float = 600.0,
                     now: Optional[float] = None) -> Dict[str, int]:
        now = self._clock() if now is None else now
        cutoff = now - float(stale_seconds)
        ids = self._r.zrangebyscore(self._k("running"), "-inf", cutoff)
        resumed = exhausted = 0
        for tid in ids:
            row = self.get(str(tid))
            if row is None or row.get("status") != "running":
                continue
            if int(row.get("attempts") or 0) < int(row.get("max_attempts") or 0):
                self.update(str(tid), status="queued", updated_at=now,
                            message="进程中断，已重新入队")
                resumed += 1
            else:
                self.update(str(tid), status="error", updated_at=now,
                            finished_at=now, error="进程中断且重试次数已用尽",
                            message="进程中断且重试次数已用尽")
                exhausted += 1
        return {"resumed": resumed, "exhausted": exhausted}

    def cancel(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.update(task_id, status="canceled", finished_at=self._clock(),
                           next_retry_at=None, message="已取消")

    def stats(self) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for tid in self._r.zrange(self._k("ids"), 0, -1):
            status = self._r.hget(self._task_key(str(tid)), "status")
            if status:
                out[str(status)] = out.get(str(status), 0) + 1
        return out

    def purge(self, statuses=TERMINAL) -> int:
        if not statuses:
            return 0
        removed = 0
        for tid in list(self._r.zrange(self._k("ids"), 0, -1)):
            status = self._r.hget(self._task_key(str(tid)), "status")
            if status in statuses:
                pipe = self._r.pipeline()
                pipe.delete(self._task_key(str(tid)))
                pipe.zrem(self._k("ids"), tid)
                pipe.zrem(self._k("queued"), tid)
                pipe.zrem(self._k("running"), tid)
                pipe.execute()
                removed += 1
        return removed

    def clear_all(self) -> int:
        """清空本前缀下所有任务（测试用）。"""
        ids = list(self._r.zrange(self._k("ids"), 0, -1))
        keys = [self._task_key(str(t)) for t in ids] + [
            self._k("ids"), self._k("queued"), self._k("running"), self._k("seq")]
        if keys:
            self._r.delete(*keys)
        return len(ids)

    def handle(self, task_id: str) -> TaskHandle:
        return TaskHandle(self, task_id)

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:  # noqa: BLE001
            pass
