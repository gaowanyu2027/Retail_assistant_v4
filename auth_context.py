"""请求级「当前账号」上下文（B3：会话与问答记录归属）

## 为什么需要它

会话数据（`chat_session` / `query_history` / 向量库里的历史问答）的读写入口有三类：

1. HTTP 路由 —— 有 `request`，可以 `Depends(get_current_user)`；
2. WebSocket 路由 —— 有 `scope["state"]["user"]`；
3. **Agent 工具与向量记忆** —— `agents/master_agent.py` 的 `search_chat_history`
   工具、`vector_memory.search_messages_hybrid()`。

第 3 类拿不到 request，却恰恰是**最危险的泄露路径**：它把检索到的历史问答
**直接喂给 LLM 生成回答**，用户不需要主动"翻别人的会话"，只要问一句
"之前问过什么"或触发记忆检索就能拿到。

给这些工具逐个加参数要改动 Agent 调用链上几十个签名（`@tool` 装饰的
LangChain 工具签名还会影响 LLM 的工具调用协议）。而 `AuthMiddleware`
已经在**每个请求入口**解析出了账号，把它写进 ContextVar 后即可全局读取 ——
`asyncio.to_thread()` 与 `asyncio.create_task()` 都会**复制当前 context**
（CPython 实现使用 `contextvars.copy_context()`），所以线程池里的同步工具函数
同样读得到，无需传参。

## 约定（很重要，安全默认偏严）

- `write_owner()`：写入时的归属，**永不返回 None**。无请求上下文时返回 `""`
  （无归属；只有审计视角可见）——例如评估脚本、后台任务写入的数据。
- `owner_filter()`：读取时的过滤值。
  - `None` = **不限定**（审计视角）：只有 `system:manage` 持有者（root）或
    **无请求上下文的后台内部调用**（如向量库重建）才会得到 None；
  - 字符串 = 只能看到归属等于该值的行；
  - **无请求上下文时返回 `""`** —— 安全默认：看不到任何有归属的数据。
    这也是"忘了设置上下文"时最不容易出事的取值。
"""
from contextvars import ContextVar

# 当前请求的账号；None 表示"没有请求上下文"或"匿名"
_actor: ContextVar[str | None] = ContextVar("dsh_actor", default=None)
# 是否为审计视角（持有 system:manage，即 root）：可查看全部账号的会话
_audit: ContextVar[bool] = ContextVar("dsh_audit", default=False)


def set_actor(username: str | None, audit: bool = False) -> None:
    """由 `AuthMiddleware` 在每个请求入口调用。

    注意：ContextVar 是按**任务/上下文**隔离的，`AuthMiddleware` 是纯 ASGI
    中间件（不是 `BaseHTTPMiddleware`），与端点在同一 task 内，
    因此这里设置的值端点读得到；不同请求之间不会串。
    """
    _actor.set(username or None)
    _audit.set(bool(audit and username))


def clear_actor() -> None:
    """显式清空（后台任务、单元测试用；避免继承到不相干的上下文）。"""
    _actor.set(None)
    _audit.set(False)


def get_actor() -> str | None:
    """当前账号名；无请求上下文时返回 None。"""
    return _actor.get()


def is_audit() -> bool:
    """当前是否为审计视角（root / system:manage）。"""
    return bool(_audit.get())


def write_owner() -> str:
    """写入归属：永不返回 None（无上下文 = `""`）。"""
    return _actor.get() or ""


def owner_filter() -> str | None:
    """读取过滤值：None = 不限定（审计或后台内部）；否则只允许该归属。"""
    if _audit.get():
        return None
    return _actor.get() or ""
