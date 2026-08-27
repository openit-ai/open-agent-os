"""P2-2 §16D performance / concurrency / quality metrics tests.

Covers:
- LongTask decomposition, checkpoint, resume, timeout, progress events
- ConcurrencyController: semaphore per tenant/user, rate limit, queue/backpressure
- MetricsCollector: Prometheus exposition, success/policy_deny rates
"""

import time
import threading
import pytest

# ── imports ────────────────────────────────────────────────────────
from runtime_adapter.long_tasks import LongTaskManager, LongTaskStatus, decompose_task
from control_plane.concurrency import (
    ConcurrencyController,
    ConcurrencyConfig,
    ConcurrencyLimitExceeded,
    BoundedQueue,
    RateLimiter,
)
from execution_gateway.metrics import MetricsCollector


# ── LongTask tests ─────────────────────────────────────────────────

def test_decompose_explicit_steps():
    subs = decompose_task("Research", steps=["a", "b", "c"])
    assert len(subs) == 3
    assert subs[0].title == "a"
    assert subs[0].order == 0


def test_long_task_create_and_progress():
    mgr = LongTaskManager()
    task = mgr.create_task("Big job", steps=["step1", "step2", "step3"])
    assert task.total_steps == 3
    assert task.progress_pct == 0.0
    mgr.start(task.task_id)
    assert mgr.get_task(task.task_id).status == LongTaskStatus.RUNNING
    # complete first subtask
    sub0 = task.sub_tasks[0]
    mgr.complete_subtask(task.task_id, sub0.id, result={"ok": True})
    p = mgr.get_progress(task.task_id)
    assert p["completed_steps"] == 1
    assert p["progress_pct"] == pytest.approx(33.3, abs=0.1)
    # checkpoints & events
    assert len(mgr.get_checkpoints(task.task_id)) == 1
    events = mgr.progress_events(task.task_id)
    assert any(e["event"] == "checkpoint" for e in events)
    assert any(e["event"] == "subtask_done" for e in events)


def test_long_task_checkpoint_resume():
    mgr = LongTaskManager()
    task = mgr.create_task("Resumable", steps=["A", "B"])
    mgr.start(task.task_id)
    mgr.complete_subtask(task.task_id, task.sub_tasks[0].id)
    # pause increments checkpoint
    mgr.pause(task.task_id)
    assert mgr.get_task(task.task_id).status.value == "paused"
    assert len(mgr.get_checkpoints(task.task_id)) == 2
    # resume
    mgr.resume(task.task_id)
    assert mgr.get_task(task.task_id).status == LongTaskStatus.RUNNING
    assert mgr.get_task(task.task_id).resume_count == 1
    # finish
    mgr.complete_subtask(task.task_id, task.sub_tasks[1].id)
    assert mgr.get_task(task.task_id).status == LongTaskStatus.COMPLETED


def test_long_task_timeout():
    mgr = LongTaskManager()
    task = mgr.create_task("Quick timeout", steps=["only"], timeout_seconds=0.05)
    mgr.start(task.task_id)
    time.sleep(0.08)
    mgr.check_timeout(task.task_id)
    assert mgr.get_task(task.task_id).status == LongTaskStatus.TIMEOUT


def test_long_task_all_steps_complete_auto():
    mgr = LongTaskManager()
    task = mgr.create_task("All done", steps=["x", "y"])
    mgr.start(task.task_id)
    for sub in list(task.sub_tasks):
        mgr.complete_subtask(task.task_id, sub.id)
    assert mgr.get_task(task.task_id).status == LongTaskStatus.COMPLETED
    assert mgr.get_progress(task.task_id)["progress_pct"] == 100.0


# ── Concurrency tests ──────────────────────────────────────────────

def test_concurrency_tenant_limit_and_queue():
    cfg = ConcurrencyConfig(tenant_concurrency=1, user_concurrency=5, queue_capacity=1)
    ctrl = ConcurrencyController(cfg)
    ctrl.acquire("t1", "employee:kim")
    # second acquire on same tenant should enqueue or raise
    with pytest.raises(ConcurrencyLimitExceeded) as exc:
        ctrl.acquire("t1", "employee:lee")
    assert "tenant" in str(exc.value).lower()
    # queue depth should be 1
    assert ctrl.queue.depth == 1
    # release frees slot
    ctrl.release("t1", "employee:kim")
    # now lee can acquire
    ctrl.acquire("t1", "employee:lee")
    ctrl.release("t1", "employee:lee")


def test_concurrency_user_limit():
    cfg = ConcurrencyConfig(tenant_concurrency=10, user_concurrency=1, queue_capacity=10)
    ctrl = ConcurrencyController(cfg)
    ctrl.acquire("t1", "employee:kim")
    with pytest.raises(ConcurrencyLimitExceeded) as exc:
        ctrl.acquire("t1", "employee:kim")
    assert "user" in str(exc.value).lower()
    ctrl.release("t1", "employee:kim")


def test_concurrency_rate_limit():
    rl = RateLimiter(rate_per_sec=1, burst=1)
    assert rl.allow("user1") is True
    assert rl.allow("user1") is False
    # retry_after > 0
    assert rl.retry_after("user1") > 0


def test_bounded_queue_backpressure():
    q = BoundedQueue(capacity=2)
    q.push("a")
    q.push("b")
    with pytest.raises(ConcurrencyLimitExceeded):
        q.push("c")
    assert q.is_full()
    assert q.pop() == "a"
    assert q.depth == 1


def test_concurrency_stats():
    ctrl = ConcurrencyController(ConcurrencyConfig(tenant_concurrency=5, user_concurrency=5))
    ctrl.acquire("t1", "employee:a")
    s = ctrl.stats()
    assert s["active_sessions"] == 1
    assert s["queue_depth"] == 0
    ctrl.release("t1", "employee:a")


def test_concurrency_concurrent_threads():
    """Verify thread-safety: 10 threads competing for tenant limit 3 should get some 429s."""
    cfg = ConcurrencyConfig(tenant_concurrency=3, user_concurrency=10, queue_capacity=50)
    ctrl = ConcurrencyController(cfg)
    successes: list[int] = []
    failures: list[int] = []

    def worker(idx: int):
        try:
            ctrl.acquire("t-shared", f"employee:user{idx}")
            successes.append(idx)
            time.sleep(0.05)
            ctrl.release("t-shared", f"employee:user{idx}")
        except ConcurrencyLimitExceeded:
            failures.append(idx)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # At tenant limit 3, at least some should have failed (backpressure)
    assert len(successes) <= 3 or len(failures) >= 1  # at least one queued
    # eventually queue may hold remainder; successes + failures == 10
    assert len(successes) + len(failures) == 10


# ── Metrics tests ──────────────────────────────────────────────────

def test_metrics_success_and_deny_rates():
    m = MetricsCollector()
    m.record_success(latency_ms=50, tool="gmail_search")
    m.record_success(latency_ms=80)
    m.record_failure(latency_ms=120, tool="gmail_search")
    m.record_policy_deny()
    snap = m.snapshot()
    assert snap["request_count"] == 4
    assert snap["success_count"] == 2
    assert snap["failure_count"] == 2  # 1 failure + 1 deny
    assert snap["policy_deny_count"] == 1
    assert snap["success_rate"] == pytest.approx(0.5)
    assert snap["policy_deny_rate"] == pytest.approx(0.25)
    assert snap["latency_avg_ms"] is not None
    assert snap["latency_p50_ms"] is not None


def test_metrics_prometheus_format():
    m = MetricsCollector()
    m.record_success(latency_ms=10, tool="calendar_list")
    m.record_audit_event("tool_call")
    prom = m.to_prometheus()
    assert "oaos_requests_total" in prom
    assert "oaos_success_rate" in prom
    assert "oaos_policy_deny_rate" in prom
    assert "oaos_audit_events_total" in prom
    assert 'oaos_tool_calls_total{tool="calendar_list"}' in prom
    # each metric has HELP/TYPE
    assert prom.count("# HELP") >= 5


def test_metrics_latency_percentiles():
    m = MetricsCollector()
    for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        m.observe_latency(v)
    assert m.latency_percentile(50) is not None
    assert m.latency_percentile(95) is not None
    assert m.latency_avg() == pytest.approx(55.0)
