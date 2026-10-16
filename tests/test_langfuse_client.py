"""Langfuse 客户端只构造一次（线程泄漏守卫）。

背景：langfuse 2.60.10 的 `Langfuse()` **不是单例**，每构造一次常驻 3 个
消费者线程（实测 1 → 4 → 7 → 10 → 13）。模板问答曾每请求构造两次
（打点 + flush）→ 6 线程/请求，1000 请求后进程线程 62 → 6145，压测延迟
逐轮劣化（详见 agents/langfuse_client.py 的完整测量记录）。

本文件用**假的 langfuse 模块**（注入 sys.modules 后还原）离线钉住两件事：
- get_langfuse() 重复调用只构造一次、返回同一实例；
- 构造失败只降级一次，不逐请求重试（每次重试都会再建 3 个线程）。
"""
import ast
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import agents.langfuse_client as LC  # noqa: E402

SINGLETON_PATH = "langfuse.utils.langfuse_singleton"


class _FakeModules:
    """临时注入假的 langfuse.utils.langfuse_singleton；退出时还原现场。"""

    def __init__(self, get_impl):
        self.get_impl = get_impl
        self._saved = {}

    def __enter__(self):
        for name in ("langfuse", "langfuse.utils", SINGLETON_PATH):
            self._saved[name] = sys.modules.get(name)

        fake_pkg = types.ModuleType("langfuse")
        fake_utils = types.ModuleType("langfuse.utils")
        fake_mod = types.ModuleType(SINGLETON_PATH)
        impl = self.get_impl        # 闭包捕获（FakeSingleton 的 self 是假单例实例）

        class FakeSingleton:
            def get(self, **kwargs):
                return impl()

        fake_mod.LangfuseSingleton = FakeSingleton
        fake_pkg.utils = fake_utils
        sys.modules["langfuse"] = fake_pkg
        sys.modules["langfuse.utils"] = fake_utils
        sys.modules[SINGLETON_PATH] = fake_mod
        return self

    def __exit__(self, *exc):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def test_client_constructed_once():
    """重复调用只构造一次、返回同一实例（这就是 6 线程/请求的修复点）。"""
    LC.reset_for_tests()
    calls = []

    def make():
        calls.append(1)
        return object()

    with _FakeModules(make):
        first = LC.get_langfuse()
        rest = [LC.get_langfuse() for _ in range(5)]

    assert first is not None
    assert all(r is first for r in rest), "重复调用返回了不同实例"
    assert len(calls) == 1, f"客户端被构造 {len(calls)} 次，应恰好 1 次"
    LC.reset_for_tests()


def test_construction_failure_degrades_once():
    """构造失败：返回 None 且不逐请求重试；复位后可重新尝试。"""
    LC.reset_for_tests()
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("模拟构造失败")

    with _FakeModules(boom):
        assert LC.get_langfuse() is None
        assert LC.get_langfuse() is None
        assert LC.get_langfuse() is None
    assert len(calls) == 1, f"失败后被重试了 {len(calls)} 次（每次都会再建 3 个线程）"

    # 复位后允许重试（fake 已还原，这里换成成功实现验证恢复路径）
    LC.reset_for_tests()
    ok_calls = []
    with _FakeModules(lambda: ok_calls.append(1) or object()):
        assert LC.get_langfuse() is not None
    assert len(ok_calls) == 1
    LC.reset_for_tests()


def test_master_agent_never_flushes_on_request_path():
    """master_agent.py 不得出现 flush() 调用（请求路径禁止同步 flush）。

    实测：单个事件的 flush ≈560ms（同步等 ingestion POST）；共享客户端下
    c100 并发全排在同一条上报队列上（整轮被拖到 <3 QPS）。事件的及时性交给
    客户端后台线程按 flush_interval 批量上报，延迟 ~1s、不阻塞请求。
    """
    src = (PROJECT_ROOT / "agents" / "master_agent.py").read_text(encoding="utf-8")
    hits = [
        node.lineno
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "flush"
    ]
    assert hits == [], f"master_agent.py 第 {hits} 行出现 flush() 调用（请求路径禁止）"
