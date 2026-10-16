"""进程内共享的 Langfuse 客户端（手动打点辅助）。

**为什么需要它（实测出来的线程泄漏）**：本项目用的 langfuse 2.60.10 里，
`Langfuse()` **不是单例** —— 每构造一次就常驻 3 个消费者线程
（ingestion / media_upload / prompt_cache），且不随实例被回收而退出：

    Langfuse() ×1 → 进程线程 +3（实测连续构造：1 → 4 → 7 → 10 → 13）

模板问答路径 `MasterAgent._lf_quick_finish` 每个请求构造两次（打点 + flush），
恰好 6 线程/请求。压测实证：1000 请求后后端进程线程数 62 → 6145，
另一长期运行的进程曾达 24405 线程、CPU 增量 ≈18.5 核——线程堆积把延迟
逐轮拖垮（同参数 P95 3.3s → 72.9s），让所有吞吐测量失真。

修法：统一走库自带的 `LangfuseSingleton().get()`（`@observe` 装饰器用的就是它，
见 `langfuse/decorators/langfuse_decorator.py`）——进程内只构造一次，
手动打点与装饰器打点共享同一个客户端。

**另一个实测（为什么请求路径禁止 flush）**：单个事件 `flush()` ≈560ms ——
它同步等 ingestion POST 完成（本机 langfuse 在 WSL 里，经 wslrelay 往返）；
共享客户端下高并发请求会在同一条上报队列上排队，压测 c100 整轮被拖到 <3 QPS。
修复前的 flush 打在新构造的**空**客户端上（队列为空、0ms 返回、事件静默丢弃），
所以这个代价此前从未暴露。事件入队后交给客户端后台线程按 `flush_interval`
批量上报即可（延迟 ~1s），请求路径**不要**调 flush——防回退守卫见
`tests/test_langfuse_client.py::test_master_agent_never_flushes_on_request_path`。

单独成模块的原因与 `agents/memory_append` 相同：单测注入假的 langfuse 模块
即可离线验证"只构造一次"，不必 import master_agent 的重链
（langchain + langgraph + langchain_openai，CI 最小依赖跑不动）。
"""
import threading

_lock = threading.Lock()
_client = None
_failed = False


def get_langfuse():
    """返回进程内共享的 Langfuse 客户端；不可用或构造失败返回 None。

    构造失败**只降级一次、不逐请求重试**：打点是可观测性旁路，而每次重试
    都会再新建 3 个消费者线程——正是本模块要消掉的泄漏。失败后打印一行，
    本进程内后续调用直接返回 None（调用方按 no-op 处理）。
    """
    global _client, _failed
    if _client is not None:
        return _client
    if _failed:
        return None
    with _lock:
        if _client is not None:
            return _client
        if _failed:
            return None
        try:
            from langfuse.utils.langfuse_singleton import LangfuseSingleton
            _client = LangfuseSingleton().get()
        except Exception as e:
            _failed = True
            print(f"[Langfuse] 客户端构造失败，本进程不再重试（打点降级）: {e}")
            return None
    return _client


def reset_for_tests() -> None:
    """测试用：清掉缓存与降级状态（自研运行器无 fixture，用例自己复位）。"""
    global _client, _failed
    with _lock:
        _client = None
        _failed = False
