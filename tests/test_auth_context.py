"""账号上下文（owner 隔离）的安全默认值。

AGENTS.md §5：会话、问答历史、长期记忆、Qdrant payload 都按 owner 隔离；
真正危险的是"忘了设置上下文"的时刻，所以这里钉住 `auth_context` 的四个取值：

- 无请求上下文：`write_owner() == ""`、`owner_filter() == ""`（看不到任何有归属的数据）
- 匿名 + audit：audit 必须失效（匿名不能是审计视角）
- 有账号 + audit：`owner_filter() is None`（不限定，仅 root 审计用）
- ContextVar 按线程隔离：一个线程设置的账号不能泄漏到另一个线程

纯 stdlib，不碰数据库与请求对象。
"""
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import auth_context as ac  # noqa: E402


def _reset():
    ac.clear_actor()


def test_no_context_is_safe_by_default():
    """无上下文时写入归属为空串、读取过滤为空串（不是 None）。"""
    _reset()
    assert ac.get_actor() is None
    assert ac.write_owner() == "", "写入归属永不返回 None"
    assert ac.owner_filter() == "", "安全默认：无上下文时看不到任何有归属的数据"
    assert ac.is_audit() is False


def test_actor_sets_write_owner_and_filter():
    _reset()
    ac.set_actor("alice")
    try:
        assert ac.get_actor() == "alice"
        assert ac.write_owner() == "alice"
        assert ac.owner_filter() == "alice"
        assert ac.is_audit() is False
    finally:
        _reset()


def test_anonymous_cannot_be_audit():
    """audit 必须同时有账号：`set_actor(None, audit=True)` 不能变成全局可见。"""
    _reset()
    ac.set_actor(None, audit=True)
    try:
        assert ac.is_audit() is False
        assert ac.owner_filter() == ""
    finally:
        _reset()


def test_audit_user_gets_unrestricted_filter():
    _reset()
    ac.set_actor("root", audit=True)
    try:
        assert ac.is_audit() is True
        assert ac.owner_filter() is None, "None = 不限定（审计视角）"
        assert ac.write_owner() == "root"
    finally:
        _reset()


def test_clear_actor_restores_safe_defaults():
    _reset()
    ac.set_actor("alice", audit=True)
    ac.clear_actor()
    assert ac.get_actor() is None
    assert ac.owner_filter() == ""
    assert ac.write_owner() == ""


def test_context_does_not_leak_between_threads():
    """ContextVar 按线程隔离：主线程的账号不应被新线程继承。"""
    _reset()
    seen = {}

    def worker():
        seen["actor"] = ac.get_actor()
        seen["filter"] = ac.owner_filter()

    ac.set_actor("alice")
    try:
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert seen == {"actor": None, "filter": ""}, seen
    finally:
        _reset()
