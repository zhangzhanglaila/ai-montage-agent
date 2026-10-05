"""任务队列冒烟（F3.1）——纯逻辑，不依赖 ffmpeg / 外部素材 / 网络。

覆盖四件"队列必须做对"的事，每条断言都可证伪：

1. **持久化**：任务落盘，另开一个连接仍然读得到（模拟进程重启）；
2. **原子认领**：6 个线程并发抢 20 个任务，不能有一个被领两次；
3. **失败重试 + 指数退避**：handler 前 2 次抛异常、第 3 次成功
   -> 终态 done、attempts==3、handler 恰好被调用 3 次；
   并单独核对退避时长严格递增且被 cap 封顶；
4. **非可重试错误**：`TaskError(retryable=False)` 一次就落 error，不浪费重试；
5. **断点续跑**：伪造"进程被杀"留下的超时 running，worker 启动后应自动续跑成功；
6. **生命周期**：start/stop 干净，stop 后线程确实退出。

用法：
    python scripts/smoke_task_queue.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.task_queue import (  # noqa: E402
    TERMINAL,
    TaskError,
    TaskStore,
    TaskWorker,
)

OUT_DIR = ROOT / "output" / "smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _fresh_db(name: str) -> str:
    p = OUT_DIR / name
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    return str(p)


def _quiet_worker(store: TaskStore, **kwargs) -> TaskWorker:
    """不打印日志/堆栈的 worker —— 冒烟里会**故意**让 handler 失败。"""
    kwargs.setdefault("logger", lambda *a, **k: None)
    kwargs.setdefault("traceback_printer", lambda *a, **k: None)
    return TaskWorker(store, **kwargs)


def _wait_terminal(store: TaskStore, tid: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store.get(tid) or {}
        if row.get("status") in TERMINAL:
            return row
        time.sleep(0.02)
    return store.get(tid) or {}


def test_persistence() -> None:
    db = _fresh_db("_tq_persist.db")
    s1 = TaskStore(db)
    s1.create("demo", {"hello": "世界"}, task_id="p1", message="等待")
    s1.enqueue("p1")
    s1.handle("p1")["progress"] = 37
    s1.close()                                   # 模拟进程退出

    s2 = TaskStore(db)                           # 重新打开
    row = s2.get("p1")
    assert row is not None, "重开连接后任务丢失"
    assert row["status"] == "queued", f"状态未持久化: {row['status']}"
    assert row["progress"] == 37, f"进度未持久化: {row['progress']}"
    assert row["payload"]["hello"] == "世界", "payload 未持久化/中文被破坏"
    s2.close()
    print(f"  [1/6] 持久化跨连接 OK  (status={row['status']}, progress={row['progress']})")


def test_atomic_claim() -> None:
    db = _fresh_db("_tq_atomic.db")
    store = TaskStore(db)
    n = 20
    for i in range(n):
        store.create("demo", {}, task_id=f"a{i:02d}")
        store.enqueue(f"a{i:02d}")

    claimed, lock = [], threading.Lock()

    def worker():
        while True:
            row = store.claim_next()
            if row is None:
                return
            with lock:
                claimed.append(row["id"])

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == n, f"认领总数 {len(claimed)} != {n}"
    assert len(set(claimed)) == n, f"有任务被重复认领: {len(claimed)} vs {len(set(claimed))}"
    store.close()
    print(f"  [2/6] 原子认领 OK  (6 线程抢 {n} 个任务，无重复)")


def test_retry_backoff() -> None:
    db = _fresh_db("_tq_retry.db")
    store = TaskStore(db, backoff_base=0.05, backoff_cap=0.2)
    calls = []

    def flaky(tid, payload):
        calls.append(tid)
        if len(calls) < 3:
            raise RuntimeError(f"第 {len(calls)} 次失败")

    worker = _quiet_worker(store, poll_interval=0.01)
    worker.register("demo", flaky)
    store.create("demo", {}, task_id="r1", max_attempts=3, status="queued")

    deadline = time.time() + 10
    saw_retry = False
    while time.time() < deadline:
        worker.run_once()
        row = store.get("r1")
        if row["status"] == "queued" and row.get("error"):
            saw_retry = True                     # 观察到了"失败后重新入队"
        if row["status"] in TERMINAL:
            break
        time.sleep(0.02)

    row = store.get("r1")
    assert row["status"] == "done", f"重试后未成功: {row['status']} / {row.get('error')}"
    assert row["attempts"] == 3, f"attempts 应为 3，实际 {row['attempts']}"
    assert len(calls) == 3, f"handler 应被调用 3 次，实际 {len(calls)}"
    assert saw_retry, "过程中未观察到'失败->重新入队'的重试态"

    # 退避公式：delay = min(base * 2^(n-1), cap)，严格递增且封顶
    store.backoff_base, store.backoff_cap = 1.0, 8.0
    delays = [store._backoff(n) for n in (1, 2, 3, 4, 5, 9)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0], f"退避曲线不对: {delays}"

    # 退避时刻要真的推迟认领
    store.create("demo", {}, task_id="bk", max_attempts=3, status="queued")
    store.claim_next(now=0.0)                    # attempts -> 1
    store.mark_error("bk", "x", retryable=True, now=100.0)
    nb = store.get("bk")
    assert abs(nb["next_retry_at"] - 101.0) < 1e-6, f"next_retry_at 应为 101.0，实际 {nb['next_retry_at']}"
    assert store.claim_next(now=100.5) is None, "未到退避时刻就被认领了"
    assert store.claim_next(now=101.5) is not None, "到达退避时刻仍未能认领"
    store.close()
    print(f"  [3/6] 失败重试+退避 OK  (calls=3, attempts=3, delays={delays})")


def test_non_retryable() -> None:
    db = _fresh_db("_tq_noretry.db")
    store = TaskStore(db, backoff_base=0.01)
    calls = []

    def fatal(tid, payload):
        calls.append(tid)
        raise TaskError("参数非法，重试无意义", retryable=False)

    worker = _quiet_worker(store, poll_interval=0.01)
    worker.register("demo", fatal)
    store.create("demo", {}, task_id="nr1", max_attempts=5, status="queued")
    worker.run_once()
    time.sleep(0.05)                             # 留窗口给"万一被重试"的情形暴露出来
    row = store.get("nr1")
    assert row["status"] == "error", f"应直接落 error，实际 {row['status']}"
    assert len(calls) == 1, f"不可重试错误被重试了 {len(calls)} 次"
    assert row["attempts"] == 1, f"attempts 应为 1，实际 {row['attempts']}"
    store.close()
    print("  [4/6] 非可重试错误 OK  (一次即终止，未消耗重试次数)")


def test_resume_stale() -> None:
    db = _fresh_db("_tq_resume.db")
    store = TaskStore(db, backoff_base=0.01)
    done = []

    def handler(tid, payload):
        done.append(tid)

    worker = TaskWorker(store, poll_interval=0.01, stale_seconds=60.0)
    worker.register("demo", handler)

    # 伪造"上次进程被杀"留下的 running（updated_at 远早于现在）
    store.create("demo", {}, task_id="s1", max_attempts=3)
    store.update("s1", status="running", attempts=1, updated_at=1.0)
    assert store.get("s1")["status"] == "running"

    worker.start()                               # start 内部先 resume_stale
    row = _wait_terminal(store, "s1", timeout=10)
    worker.stop()

    assert row["status"] == "done", f"续跑未成功: {row['status']}"
    assert done == ["s1"], f"续跑的 handler 调用异常: {done}"
    print("  [5/6] 断点续跑 OK  (running -> 自动重新入队 -> 执行成功)")


def test_lifecycle() -> None:
    db = _fresh_db("_tq_life.db")
    store = TaskStore(db)
    worker = TaskWorker(store, poll_interval=0.01)
    worker.register("demo", lambda tid, p: None)
    assert not worker.running, "初始不应处于运行态"
    worker.start()
    assert worker.running, "start() 后应为运行态"
    worker.start()                               # 重复 start 不应起两个线程
    worker.stop()
    assert not worker.running, "stop() 后线程未退出"
    store.close()
    print("  [6/6] worker 生命周期 OK  (start/stop 干净，重复 start 安全)")


def main() -> int:
    print("=" * 58)
    print("任务队列冒烟（F3.1：持久化 / 原子认领 / 重试续跑）")
    print("=" * 58)
    test_persistence()
    test_atomic_claim()
    test_retry_backoff()
    test_non_retryable()
    test_resume_stale()
    test_lifecycle()
    print("-" * 58)
    print("冒烟通过 ✓ （持久化 / 原子认领 / 退避重试 / 不可重试 / 断点续跑 / 生命周期）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
