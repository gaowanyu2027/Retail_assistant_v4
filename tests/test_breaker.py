"""向量层熔断 + 舱壁的状态机测试（纯函数/线程，不需要 Qdrant）。"""
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import vector_memory as vm  # noqa: E402
from config.settings import (  # noqa: E402
    VECTOR_BREAKER_COOLDOWN_SECONDS,
    VECTOR_BREAKER_FAILS,
    VECTOR_BULKHEAD_MAX_CONCURRENT,
    VECTOR_BULKHEAD_WAIT_SECONDS,
)


def test_breaker_opens_after_threshold_and_blocks_fast():
    vm.breaker_reset()
    assert vm.breaker_open() is False
    for _ in range(VECTOR_BREAKER_FAILS - 1):
        vm.breaker_record(False)
    assert vm.breaker_open() is False, "未达阈值不该熔断"
    vm.breaker_record(False)
    assert vm.breaker_open() is True, "达到阈值应熔断"
    assert vm.breaker_remaining() > 0
    # 冷却期内调用必须**快速失败**，且不发任何网络请求
    t0 = time.time()
    try:
        vm._qdrant("test", lambda: (_ for _ in ()).throw(AssertionError("不该被执行")))
        raise AssertionError("冷却期内应当抛出 VectorUnavailable")
    except vm.VectorUnavailable:
        pass
    assert (time.time() - t0) < 0.2, "冷却期内应当**立即**失败，而不是等超时"
    vm.breaker_reset()


def test_breaker_resets_on_success():
    vm.breaker_reset()
    for _ in range(VECTOR_BREAKER_FAILS):
        vm.breaker_record(False)
    assert vm.breaker_open() is True
    vm.breaker_record(True)
    assert vm.breaker_open() is False, "一次成功应复位"
    assert vm.breaker_remaining() == 0


def test_breaker_cooldown_expires():
    """冷却时长可配：这里只验证"到期后不再视为熔断"（不真的等 15 秒）。"""
    vm.breaker_reset()
    import vector_memory
    with vector_memory._breaker_lock:                 # 直接改内部状态，避免真等
        vector_memory._breaker_until = time.time() - 1
    assert vm.breaker_open() is False
    vm.breaker_reset()


def test_bulkhead_caps_concurrency():
    """舱壁的**核心不变量**：同时在执行的调用数不超过许可数。

    注意别把"排队的会在等待上限内拿到许可并执行"误判成 bug ——
    舱壁是"限流"不是"丢弃"：等得到就执行，等不到（超过 wait 上限）才降级。
    """
    vm.breaker_reset()
    cur = {"n": 0, "max": 0}
    lock = threading.Lock()

    def work():
        with lock:
            cur["n"] += 1
            cur["max"] = max(cur["max"], cur["n"])
        time.sleep(0.3)
        with lock:
            cur["n"] -= 1
        return "ok"

    def worker():
        try:
            vm._qdrant("slow", work)
        except Exception:                             # noqa: BLE001
            pass

    threads = [threading.Thread(target=worker) for _ in range(VECTOR_BULKHEAD_MAX_CONCURRENT * 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cur["max"] <= VECTOR_BULKHEAD_MAX_CONCURRENT, \
        f"并发超过许可数: {cur['max']} > {VECTOR_BULKHEAD_MAX_CONCURRENT}"


def test_bulkhead_fast_fails_when_wait_exhausted():
    """工作耗时 > 等待上限时，超出的调用必须**降级**（不无限排队）。"""
    vm.breaker_reset()
    results = []
    lock = threading.Lock()

    def worker():
        try:
            vm._qdrant("very-slow", lambda: time.sleep(VECTOR_BULKHEAD_WAIT_SECONDS + 1.5) or "ok")
            r = "ok"
        except vm.VectorUnavailable:
            r = "fast-fail"
        except Exception as e:                        # noqa: BLE001
            r = type(e).__name__
        with lock:
            results.append(r)

    n = VECTOR_BULKHEAD_MAX_CONCURRENT + 2
    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == VECTOR_BULKHEAD_MAX_CONCURRENT, results
    assert results.count("fast-fail") == 2, results


def test_qdrant_call_returns_value_and_counts_success():
    vm.breaker_reset()
    assert vm._qdrant("echo", lambda x: x * 2, 21) == 42
    assert vm.breaker_open() is False


def test_qdrant_call_records_failure():
    vm.breaker_reset()
    try:
        vm._qdrant("boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    except RuntimeError:
        pass
    # 失败计数已累加：再来 threshold-1 次即熔断
    for _ in range(VECTOR_BREAKER_FAILS - 1):
        try:
            vm._qdrant("boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        except Exception:                             # noqa: BLE001
            pass
    assert vm.breaker_open() is True
    vm.breaker_reset()
