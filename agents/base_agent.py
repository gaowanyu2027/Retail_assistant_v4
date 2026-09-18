"""
Agent 基础设施 — LangChain 方案
使用 ChatOpenAI + create_agent，参考 travel_agent 架构
"""
import re
import threading

from langchain_openai import ChatOpenAI
from config.settings import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, LLM_TEMPERATURE

# ==================== 对话记忆持久化（MySQL 优先，SQLite 回退） ====================

_memory_singleton = None
_memory_choice = None  # "mysql" | "sqlite"
_memory_lock = threading.Lock()

# ==================== Langfuse 可观测（@observe 打点，环境变量驱动） ====================

_langfuse_enabled = None


def langfuse_available() -> bool:
    """Langfuse 是否可用（环境变量齐全且包可导入）。首次探测后缓存结果。"""
    global _langfuse_enabled
    if _langfuse_enabled is not None:
        return _langfuse_enabled
    try:
        import os
        from langfuse.decorators import observe  # noqa: F401
        ok = bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))
        _langfuse_enabled = ok
        if ok:
            print("[Langfuse] 可观测已启用（@observe 打点：问答/工具调用将上报 Trace）")
        return ok
    except Exception as e:
        print(f"[Langfuse] 未启用，跳过追踪: {e}")
        _langfuse_enabled = False
        return False


def observe_langfuse():
    """返回 langfuse @observe 装饰器；不可用时返回空装饰器，不影响主流程。"""
    if not langfuse_available():
        def _noop(fn):
            return fn
        return _noop
    from langfuse.decorators import observe
    return observe()


def create_memory():
    """创建进程级共享的持久化记忆 checkpointer（线程安全单例）。

    与项目数据层约定一致：优先写入 MySQL（Retail_assistant.agent_checkpoints），
    MySQL 不可用时自动回退 SQLite（data/agent_checkpoints.db）并在终端打印日志。

    Returns:
        LangGraph checkpointer 实例 — 按 thread_id(=session_id) 隔离多轮对话，
        服务重启 / Agent 重建后记忆仍可恢复。
    """
    global _memory_singleton, _memory_choice
    if _memory_singleton is not None:
        return _memory_singleton

    with _memory_lock:
        # 双重检查：避免并发首次调用创建多个实例/连接
        if _memory_singleton is not None:
            return _memory_singleton

        # 1) 优先 MySQL
        try:
            import mysql_db

            if mysql_db.mysql_available():
                from agents.mysql_checkpointer import MysqlCheckpointer

                cp = MysqlCheckpointer()
                cp._ensure_ready()  # 建表探测，失败即回退
                _memory_singleton = cp
                _memory_choice = "mysql"
                print("[Agent] 对话记忆持久化: MySQL (Retail_assistant.agent_checkpoints)")
                return _memory_singleton
        except Exception as e:
            print(f"[Agent][WARN] MySQL 检查点初始化失败，回退 SQLite: {e}")

        # 2) 回退 SQLite
        from config.settings import DATA_DIR
        from agents.sqlite_checkpointer import SqliteCheckpointer

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = DATA_DIR / "agent_checkpoints.db"
        _memory_singleton = SqliteCheckpointer(db_path)
        _memory_choice = "sqlite"
        print(f"[Agent] 对话记忆持久化: SQLite ({db_path})")
        return _memory_singleton


def create_llm(temperature: float = LLM_TEMPERATURE, tag: str = "unknown") -> ChatOpenAI:
    """创建 LLM 实例 — 通过 ChatOpenAI 对接 DeepSeek

    带请求超时与自动重试（DeepSeek 偶发 5xx 时框架自动重试，配合 Agent 层模板降级）。

    `tag`：这次调用的**用途标签**（answer / intent / report / title …），
    用于 LLM 用量与成本归因 —— 全项目 9 处调用都从这里创建实例，
    所以指标只需要挂在这一处（见 `agents/llm_metrics.py`）。

    Args:
        temperature: 生成温度（0=确定，1=随机）
        tag: 调用用途（用于指标归因，不影响行为）

    Returns:
        ChatOpenAI 实例
    """
    from agents.llm_metrics import make_metrics_handler

    handler = make_metrics_handler(tag)
    return ChatOpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
        temperature=temperature,
        timeout=60,
        max_retries=2,
        callbacks=[handler] if handler else None,
    )


def generate_session_title(question: str, max_chars: int = 16) -> str:
    """用 LLM 生成简短会话标题；失败或空输入时回退为问题前 N 字。

    Args:
        question: 用户首个问题
        max_chars: 回退标题最大长度

    Returns:
        会话标题字符串
    """
    question = (question or "").strip()
    if not question:
        return "新会话"
    try:
        llm = create_llm(temperature=0, tag="title")
        prompt = (
            "请为下面这句用户的提问生成一个不超过12个字的中文会话标题。"
            "只输出标题本身，不要引号、不要解释、不要多余标点。\n提问：" + question[:200]
        )
        resp = llm.invoke(prompt)
        content = resp.content if isinstance(resp.content, str) else str(resp.content or "")
        title = content.strip().strip('"“”\'')
        title = re.sub(r"\s+", " ", title)
        if 1 <= len(title) <= max_chars + 8:
            return title
    except Exception as e:
        print(f"[Agent] 会话标题生成失败，使用默认标题: {e}")
    return question[:max_chars]
