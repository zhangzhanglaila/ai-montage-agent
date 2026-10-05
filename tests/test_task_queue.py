"""任务队列（F3.1）单元/集成测试。

与 ``scripts/smoke_task_queue.py`` 的区别：这里断得更细、跑得更快（无长睡眠），
并额外覆盖 **WebUI 集成**（提交 -> 落盘 -> worker 执行 -> 终态）。
"""

import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from packages.task_queue import (  # noqa: E402
    RedisTaskStore,
    TaskError,
    TaskStore,
    TaskWorker,
    redis_available,
)


@pytest.fixture()
def task_db(tmp_path, monkeypatch):
    """独立的临时任务库，并在用例前后清掉进程内单例。"""
    from packages.task_queue import reset_runtime

    db = str(tmp_path / "tasks.db")
    monkeypatch.setenv("MONTAGE_TASK_DB", db)
    reset_runtime()
    yield db
    reset_runtime()


def _quiet_worker(store, **kw):
    kw.setdefault("logger", lambda *a, **k: None)
    kw.setdefault("traceback_printer", lambda *a, **k: None)
    return TaskWorker(store, **kw)


def test_persistence_across_connections(tmp_path):
    db = str(tmp_path / "p.db")
    s1 = TaskStore(db)
    s1.create("demo", {"k": "值"}, task_id="p1")
    s1.enqueue("p1")
    s1.handle("p1")["progress"] = 55
    s1.close()

    s2 = TaskStore(db)
    row = s2.get("p1")
    assert row["status"] == "queued"
    assert row["progress"] == 55
    assert row["payload"] == {"k": "值"}
    s2.close()


def test_atomic_claim_no_duplicates(tmp_path):
    store = TaskStore(str(tmp_path / "a.db"))
    for i in range(12):
        store.create("demo", {}, task_id=f"a{i:02d}")
        store.enqueue(f"a{i:02d}")

    claimed, lock = [], threading.Lock()

    def run():
        while True:
            row = store.claim_next()
            if row is None:
                return
            with lock:
                claimed.append(row["id"])

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 12
    assert len(set(claimed)) == 12


def test_backoff_curve_and_gating(tmp_path):
    store = TaskStore(str(tmp_path / "b.db"), backoff_base=1.0, backoff_cap=8.0)
    assert [store._backoff(n) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 8.0]

    store.create("demo", {}, task_id="bk", max_attempts=3)
    store.enqueue("bk")
    store.claim_next(now=0.0)                     # attempts -> 1
    store.mark_error("bk", "boom", retryable=True, now=100.0)
    row = store.get("bk")
    assert row["status"] == "queued"
    assert row["next_retry_at"] == pytest.approx(101.0)
    assert store.claim_next(now=100.9) is None    # 未到点
    assert store.claim_next(now=101.1) is not None  # 到点


def test_retry_then_success(tmp_path):
    store = TaskStore(str(tmp_path / "r.db"), backoff_base=0.01, backoff_cap=0.05)
    calls = []

    def flaky(tid, payload):
        calls.append(tid)
        if len(calls) < 3:
            raise RuntimeError("boom")

    worker = _quiet_worker(store, poll_interval=0.01)
    worker.register("demo", flaky)
    store.create("demo", {}, task_id="r1", max_attempts=3)
    store.enqueue("r1")

    deadline = time.time() + 10
    while time.time() < deadline:
        worker.run_once()
        if store.get("r1")["status"] in ("done", "error"):
            break
        time.sleep(0.01)

    row = store.get("r1")
    assert row["status"] == "done"
    assert row["attempts"] == 3
    assert len(calls) == 3


def test_retry_exhausted_becomes_error(tmp_path):
    store = TaskStore(str(tmp_path / "e.db"), backoff_base=0.01, backoff_cap=0.02)

    def always_fail(tid, payload):
        raise RuntimeError("永远失败")

    worker = _quiet_worker(store, poll_interval=0.01)
    worker.register("demo", always_fail)
    store.create("demo", {}, task_id="e1", max_attempts=2)
    store.enqueue("e1")

    deadline = time.time() + 10
    while time.time() < deadline:
        worker.run_once()
        if store.get("e1")["status"] in ("done", "error"):
            break
        time.sleep(0.01)

    row = store.get("e1")
    assert row["status"] == "error"
    assert row["attempts"] == 2
    assert "永远失败" in row["error"]


def test_non_retryable_does_not_retry(tmp_path):
    store = TaskStore(str(tmp_path / "n.db"))
    calls = []

    def fatal(tid, payload):
        calls.append(tid)
        raise TaskError("参数非法", retryable=False)

    worker = _quiet_worker(store, poll_interval=0.01)
    worker.register("demo", fatal)
    store.create("demo", {}, task_id="n1", max_attempts=5)
    store.enqueue("n1")
    worker.run_once()

    row = store.get("n1")
    assert row["status"] == "error"
    assert row["attempts"] == 1
    assert calls == ["n1"]


def test_resume_stale_requeues_and_exhausts(tmp_path):
    store = TaskStore(str(tmp_path / "s.db"))
    store.create("demo", {}, task_id="fresh", max_attempts=3)
    store.update("fresh", status="running", attempts=1, updated_at=1.0)
    store.create("demo", {}, task_id="burnt", max_attempts=3)
    store.update("burnt", status="running", attempts=3, updated_at=1.0)

    info = store.resume_stale(stale_seconds=60, now=1_000_000.0)
    assert info == {"resumed": 1, "exhausted": 1}
    assert store.get("fresh")["status"] == "queued"
    assert store.get("burnt")["status"] == "error"


def test_worker_lifecycle(tmp_path):
    store = TaskStore(str(tmp_path / "l.db"))
    worker = _quiet_worker(store, poll_interval=0.01)
    worker.register("demo", lambda tid, p: None)
    assert not worker.running
    worker.start()
    assert worker.running
    worker.start()                                # 幂等
    worker.stop()
    assert not worker.running


def test_unknown_kind_fails_fast(tmp_path):
    store = TaskStore(str(tmp_path / "u.db"))
    worker = _quiet_worker(store)
    store.create("unknown-kind", {}, task_id="u1")
    store.enqueue("u1")
    worker.run_once()
    assert store.get("u1")["status"] == "error"


def test_webui_submit_persists_and_fails_fast(task_db):
    """WebUI 全链路：提交 -> 落盘 -> worker 执行 -> 终态（缺 BGM 属不可重试错误）。"""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient

    from packages.webui.src.app import app

    with TestClient(app) as client:
        resp = client.post("/api/montage", data={"video_paths": "[]"})
        tid = resp.json()["task_id"]
        assert tid

        deadline = time.time() + 15
        task = {}
        while time.time() < deadline:
            task = client.get(f"/api/task/{tid}").json()
            if task.get("status") in ("done", "error", "canceled"):
                break
            time.sleep(0.05)

        assert task["status"] == "error"
        assert task["attempts"] == 1              # 不可重试，不该白跑
        assert "BGM" in task["message"]

        # 列表接口能看到它
        listing = client.get("/api/tasks").json()
        assert any(t["id"] == tid for t in listing["tasks"])
        assert listing["stats"]["error"] >= 1

    # 已落盘：任务详情在最终产物之外仍可读
    store = TaskStore(task_db)
    assert store.get(tid)["status"] == "error"
    store.close()


# ======================================================================
# Redis 后端（F3.1 扩展）：与 SQLite 版**同接口**，多进程/多机共享任务表
# ======================================================================
# 本机无 Redis 时整块自动跳过，不影响其它环境的 `pytest` 全绿。
REDIS_URL = os.environ.get("MONTAGE_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")

requires_redis = pytest.mark.skipif(
    not redis_available(REDIS_URL),
    reason=f"未检测到可用 Redis：{REDIS_URL}（设 MONTAGE_TEST_REDIS_URL 可指定）",
)


def test_redis_available_probe_semantics():
    """``redis_available()`` 无参只判依赖；传 URL 判连通性且从不抛错。"""
    assert redis_available() is True                       # 已装 redis-py
    assert redis_available("redis://127.0.0.1:1") is False  # 连不上 -> False，不抛错


@pytest.fixture()
def redis_store():
    """每个用例一个独立 prefix，用例结束清空并关连接。"""
    made = []

    def make(**kw):
        st = RedisTaskStore(
            REDIS_URL, prefix=f"tqtest:{uuid.uuid4().hex[:8]}", **kw)
        made.append(st)
        return st

    yield make
    for st in made:
        try:
            st.clear_all()
        except Exception:  # noqa: BLE001
            pass
        st.close()


@requires_redis
def test_redis_interface_parity_with_sqlite(redis_store):
    """核心读写语义与 SQLite 版逐一对齐。"""
    st = redis_store()
    row = st.create("montage", {"clips": ["a.mp4"], "n": 1}, task_id="k1")
    assert row["status"] == "pending"
    assert row["seq"] == 1
    assert st.get("k1")["payload"] == {"clips": ["a.mp4"], "n": 1}

    st.enqueue("k1")
    assert st.get("k1")["status"] == "queued"

    claimed = st.claim_next()
    assert claimed["id"] == "k1"
    assert st.get("k1")["status"] == "running"
    assert st.get("k1")["attempts"] == 1

    h = st.handle("k1")                          # 赋值即持久化
    h["stage"] = "render"
    h["progress"] = 50
    got = st.get("k1")
    assert (got["stage"], got["progress"]) == ("render", 50)

    st.mark_done("k1", output_path="out.mp4", result={"ok": True})
    done = st.get("k1")
    assert done["status"] == "done" and done["progress"] == 100
    assert done["output_path"] == "out.mp4"
    assert done["result"] == {"ok": True}

    assert [r["id"] for r in st.list()] == ["k1"]
    assert st.stats()["done"] == 1
    assert st.purge() == 1
    assert st.get("k1") is None


@requires_redis
def test_redis_atomic_claim_no_duplicates(redis_store):
    """6 线程并发抢单，Lua 保证不重复认领。"""
    st = redis_store()
    for i in range(20):
        st.create("montage", {"i": i}, task_id=f"a{i:02d}")
        st.enqueue(f"a{i:02d}")

    claimed, lock = [], threading.Lock()

    def run():
        while True:
            row = st.claim_next()
            if row is None:
                return
            with lock:
                claimed.append(row["id"])

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 20
    assert len(set(claimed)) == 20


@requires_redis
def test_redis_backoff_curve_and_gating(redis_store):
    st = redis_store(backoff_base=1.0, backoff_cap=8.0)
    assert [st._backoff(n) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 8.0]

    st.create("montage", {}, task_id="bk", max_attempts=3)
    st.enqueue("bk")
    st.claim_next(now=0.0)                                   # attempts -> 1
    st.mark_error("bk", "boom", retryable=True, now=100.0)
    row = st.get("bk")
    assert row["status"] == "queued"
    assert row["next_retry_at"] == pytest.approx(101.0)
    assert st.claim_next(now=100.9) is None                  # 未到退避点
    assert st.claim_next(now=101.1) is not None              # 到点才认领


@requires_redis
def test_redis_resume_stale(redis_store):
    st = redis_store()
    st.create("montage", {}, task_id="fresh", max_attempts=3)
    st.update("fresh", status="running", attempts=1, updated_at=1.0)
    st.create("montage", {}, task_id="burnt", max_attempts=3)
    st.update("burnt", status="running", attempts=3, updated_at=1.0)

    info = st.resume_stale(stale_seconds=60, now=1_000_000.0)
    assert info == {"resumed": 1, "exhausted": 1}
    assert st.get("fresh")["status"] == "queued"
    assert st.get("burnt")["status"] == "error"


@requires_redis
def test_redis_worker_retry_then_success(redis_store):
    st = redis_store(backoff_base=0.01, backoff_cap=0.05)
    calls = []

    def flaky(tid, payload):
        calls.append(tid)
        if len(calls) < 3:
            raise RuntimeError("boom")

    worker = _quiet_worker(st, poll_interval=0.01)
    worker.register("montage", flaky)
    st.create("montage", {}, task_id="r1", max_attempts=3)
    st.enqueue("r1")

    deadline = time.time() + 10
    while time.time() < deadline:
        worker.run_once()
        if st.get("r1")["status"] in ("done", "error"):
            break
        time.sleep(0.01)

    row = st.get("r1")
    assert row["status"] == "done"
    assert row["attempts"] == 3
    assert len(calls) == 3


@requires_redis
def test_runtime_switches_to_redis_backend(monkeypatch):
    """``MONTAGE_TASK_BACKEND=redis`` 时 get_store 返回 RedisTaskStore。"""
    from packages.task_queue import get_store, reset_runtime

    monkeypatch.setenv("MONTAGE_TASK_BACKEND", "redis")
    monkeypatch.setenv("MONTAGE_REDIS_URL", REDIS_URL)
    reset_runtime()
    try:
        store = get_store()
        assert isinstance(store, RedisTaskStore)
        assert store.db_path == f"redis:{REDIS_URL}"
    finally:
        reset_runtime()
