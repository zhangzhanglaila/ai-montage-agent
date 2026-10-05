"""Redis 后端任务队列冒烟（F3.1 扩展）—— 自包含，直连真实 Redis 验证真语义。

与 ``scripts/smoke_task_queue.py``（SQLite 版）的关系
----------------------------------------------------
两者断言**同一套队列语义**，用来证明两个后端「同接口」：

    退避曲线 / 重试门禁 / 原子抢单 / 断点续跑(resume_stale) / worker 端到端

Redis 用**真实例**跑（本脚本不 mock），因为这里要验的正是「Lua 原子性 + 老版本
Redis 命令兼容性」这类只有真服务端才暴露的问题——事实上这两个坑就是靠它抓出来的：
  * redis-py ≥ 8 默认 RESP3 发 ``HELLO``，Redis 3.0 不认 → 必须 ``protocol=2``；
  * 多字段 ``HSET``（Redis 4.0+）在 3.x 上报 wrong number of arguments → 逐字段 HSET。

行为
----
* 默认打 ``redis://127.0.0.1:6379/0``，可用 ``MONTAGE_TEST_REDIS_URL`` 覆盖。
* **连不上就打印 SKIP 并退出码 0**（不装 Redis 的机器上不会拖垮 CI）。
* 每个用例用独立 prefix，结束统一清空，不污染真实业务数据。

直接跑::

    D:/programs/Python/Python310/python.exe scripts/smoke_task_queue_redis.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.task_queue import (  # noqa: E402
    RedisTaskStore,
    TaskError,
    TaskWorker,
    redis_available,
)

REDIS_URL = os.environ.get("MONTAGE_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")

_results: list = []


def check(name: str, cond: bool, extra: object = "") -> None:
    _results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra != "" else ""))


def _store(**kw) -> RedisTaskStore:
    return RedisTaskStore(REDIS_URL, prefix=f"smoke:{uuid.uuid4().hex[:8]}", **kw)


def main() -> int:
    print(f"Redis 后端任务队列冒烟 @ {REDIS_URL}")
    if not redis_available(REDIS_URL):
        print(f"  SKIP: 连不上 {REDIS_URL}，跳过 Redis 冒烟（退出码 0，不影响门禁）")
        return 0

    st = _store()
    try:
        _run_all()
    finally:
        st.close()

    ok = all(r[1] for r in _results)
    passed = sum(1 for r in _results if r[1])
    print(f"\n结果：{'全部通过' if ok else '存在失败'} ({passed}/{len(_results)})")
    return 0 if ok else 1


def _run_all() -> None:
    print("\n[1] 基础读写：create/enqueue/claim/handle/mark_done")
    st = _store()
    row = st.create("montage", {"clips": ["a.mp4"], "n": 1})
    check("create -> pending", row["status"] == "pending", row["status"])
    check("payload 往返", row["payload"] == {"clips": ["a.mp4"], "n": 1}, row["payload"])
    tid = row["id"]

    st.enqueue(tid)
    check("enqueue -> queued", st.get(tid)["status"] == "queued")

    got = st.claim_next()
    check("claim_next 认领到该任务", got is not None and got["id"] == tid)
    check("claim 后 running", st.get(tid)["status"] == "running")
    check("attempts -> 1", st.get(tid)["attempts"] == 1, st.get(tid)["attempts"])

    h = st.handle(tid)                       # 赋值即持久化
    h["stage"] = "render"
    h["progress"] = 50
    cur = st.get(tid)
    check("handle 回写(stage/progress)", (cur["stage"], cur["progress"]) == ("render", 50),
          f"{cur['stage']}/{cur['progress']}")

    st.mark_done(tid, output_path="out.mp4", result={"ok": True})
    done = st.get(tid)
    check("mark_done -> done/100%", done["status"] == "done" and done["progress"] == 100)
    check("result 往返", done["result"] == {"ok": True} and done["output_path"] == "out.mp4")
    check("list/stats 可见", [r["id"] for r in st.list()] == [tid] and st.stats()["done"] == 1)
    check("purge 清终态", st.purge() == 1 and st.get(tid) is None)
    st.clear_all()
    st.close()

    print("\n[2] 退避曲线 min(base*2^(n-1), cap) = [1,2,4,8,8]")
    stb = _store(backoff_base=1.0, backoff_cap=8.0)
    t2 = stb.create("montage", {}, max_attempts=8)["id"]
    stb.enqueue(t2)
    stb.claim_next()
    delays = []
    for _ in range(5):
        stb.mark_error(t2, "boom", retryable=True)
        delays.append(round(stb.get(t2)["next_retry_at"] - time.time()))
        stb.enqueue(t2)                      # 放回队列，模拟 worker 取走
        stb.claim_next()
    check("退避序列", delays == [1, 2, 4, 8, 8], delays)
    stb.clear_all()
    stb.close()

    print("\n[3] 非重试错误：直接落 error，不再入队")
    st3 = _store()
    t3 = st3.create("montage", {})["id"]
    st3.enqueue(t3)
    st3.claim_next()
    st3.mark_error(t3, "fatal", retryable=False)
    check("fatal -> error 终态", st3.get(t3)["status"] == "error")
    check("fatal 不再被认领", st3.claim_next() is None)
    st3.clear_all()
    st3.close()

    print("\n[4] 原子抢单：6 线程 × 20 任务，无重复无漏领")
    st4 = _store()
    for i in range(20):
        ti = st4.create("montage", {"i": i})["id"]
        st4.enqueue(ti)
    claimed, lock = [], threading.Lock()

    def grab():
        while True:
            r = st4.claim_next()
            if r is None:
                return
            with lock:
                claimed.append(r["id"])

    ts = [threading.Thread(target=grab) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("认领总数 == 20", len(claimed) == 20, len(claimed))
    check("无重复认领", len(set(claimed)) == 20, len(set(claimed)))
    st4.clear_all()
    st4.close()

    print("\n[5] 断点续跑：resume_stale 把卡住的 running 捞回队列")
    st5 = _store()
    fresh = st5.create("montage", {}, max_attempts=3)["id"]
    st5.update(fresh, status="running", attempts=1, updated_at=1.0)
    burnt = st5.create("montage", {}, max_attempts=3)["id"]
    st5.update(burnt, status="running", attempts=3, updated_at=1.0)
    info = st5.resume_stale(stale_seconds=60, now=1_000_000.0)
    check("resume/exhaust 计数", info == {"resumed": 1, "exhausted": 1}, info)
    check("未用尽 -> queued", st5.get(fresh)["status"] == "queued")
    check("已用尽 -> error", st5.get(burnt)["status"] == "error")
    st5.clear_all()
    st5.close()

    print("\n[6] worker 端到端：偶发失败 -> 重试 -> done")
    stw = _store(backoff_base=0.2, backoff_cap=0.4)
    calls = {"n": 0}
    worker = TaskWorker(stw, poll_interval=0.05, stale_seconds=600,
                        logger=lambda *a, **k: None,
                        traceback_printer=lambda *a, **k: None)

    def handler(tid, payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TaskError("transient", retryable=True)
        stw.handle(tid)["stage"] = "finished"

    worker.register("montage", handler)
    tw = stw.create("montage", {})["id"]
    stw.enqueue(tw)
    worker.start()
    deadline = time.time() + 20
    while time.time() < deadline and stw.get(tw)["status"] not in ("done", "error"):
        time.sleep(0.1)
    worker.stop()
    fin = stw.get(tw)
    check("最终 done", fin["status"] == "done", fin["status"])
    check("恰好执行 3 次", calls["n"] == 3, calls["n"])
    check("stage 回写", fin.get("stage") == "finished", fin.get("stage"))
    stw.clear_all()
    stw.close()

    print("\n[7] CLI 可用：pipeline.py --task-list 走 Redis 后端")
    st7 = _store()
    tid7 = st7.create("montage", {"cli": True})["id"]
    st7.enqueue(tid7)
    env = dict(os.environ, MONTAGE_TASK_BACKEND="redis",
               MONTAGE_REDIS_URL=REDIS_URL)
    # CLI 用的是默认前缀，这里只为验证「后端切换 + 命令可用」，故另建一条默认前缀任务
    from packages.task_queue import RedisTaskStore as _R
    cli_store = _R(REDIS_URL, prefix="tq")
    cli_store.clear_all()
    cli_id = cli_store.create("montage", {"cli": True})["id"]
    cli_store.enqueue(cli_id)
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "pipeline.py"), "--task-list"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        check("CLI 退出码 0", proc.returncode == 0, proc.returncode)
        check("CLI 显示的库是 redis", "redis:" in out, out.splitlines()[:1])
        check("CLI 列出任务", cli_id[:12] in out)
    finally:
        cli_store.clear_all()
        cli_store.close()
        st7.clear_all()
        st7.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(2)
