"""长期记忆的 owner 隔离与 SQLite 回退后端。

长期记忆的摘要是**用户对话内容**，新会话会把它注入 system 上下文。
若读取不带 owner，就会把别人的会话摘要喂给当前用户的 LLM（历史泄露路径）。
这里用临时 SQLite 文件钉住：

- `get_recent/search` 按 owner 过滤；`owner=None` 才不限定（审计视角）
- 同一 session_id upsert 覆盖而不新增
- summary 截断 4000 / keywords 截断 500
- 老库迁移：缺 `owner` 列时自动 ALTER，且新列在**最后一列**（老行默认 `""`）

不依赖 MySQL，CI 最小依赖即可运行。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.long_term_memory import SqliteLongTermMemory  # noqa: E402


def test_owner_isolation_on_read():
    with tempfile.TemporaryDirectory() as d:
        mem = SqliteLongTermMemory(Path(d) / "ltm.db")
        mem.upsert("s-alice", "爱丽丝的会话摘要", "货架", 2, owner="alice")
        mem.upsert("s-bob", "鲍勃的会话摘要", "销量", 3, owner="bob")
        mem.upsert("s-anon", "无归属的摘要", "客流", 1)

        assert {r["session_id"] for r in mem.get_recent(10, owner="alice")} == {"s-alice"}
        assert {r["session_id"] for r in mem.get_recent(10, owner="bob")} == {"s-bob"}
        assert {r["session_id"] for r in mem.get_recent(10, owner="")} == {"s-anon"}
        assert {r["session_id"] for r in mem.get_recent(10, owner=None)} == {
            "s-alice", "s-bob", "s-anon"}
        assert {r["session_id"] for r in mem.search("摘要", owner="bob")} == {"s-bob"}
        mem.close()


def test_upsert_updates_existing_session_without_duplicate():
    with tempfile.TemporaryDirectory() as d:
        mem = SqliteLongTermMemory(Path(d) / "ltm.db")
        mem.upsert("s1", "旧摘要", "旧关键词", 2, owner="alice")
        mem.upsert("s1", "新摘要", "新关键词", 5, owner="bob")

        assert mem.count() == 1
        row = mem.get_by_session("s1")
        assert row["summary"] == "新摘要"
        assert row["keywords"] == "新关键词"
        assert row["message_count"] == 5
        assert row["owner"] == "bob", "upsert 会连同归属一起更新"
        mem.close()


def test_summary_and_keywords_are_truncated():
    with tempfile.TemporaryDirectory() as d:
        mem = SqliteLongTermMemory(Path(d) / "ltm.db")
        mem.upsert("s1", "x" * 5000, "k" * 600, 1, owner="alice")
        row = mem.get_by_session("s1")
        assert len(row["summary"]) == 4000
        assert len(row["keywords"]) == 500
        mem.close()


def test_get_recent_limit_and_order():
    with tempfile.TemporaryDirectory() as d:
        mem = SqliteLongTermMemory(Path(d) / "ltm.db")
        for i in range(1, 4):
            mem.upsert(f"s{i}", f"摘要{i}", owner="alice")
        recent = mem.get_recent(2, owner="alice")
        assert [r["session_id"] for r in recent] == ["s3", "s2"], "最新的在前"
        mem.close()


def test_old_database_gets_owner_column_migration():
    """老库（无 owner 列）打开时自动补列；旧行 owner 为空串，且仍可被更新。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE agent_long_term_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO agent_long_term_memory"
            "(session_id, summary, keywords, message_count, created_at, updated_at)"
            " VALUES ('old', '旧库摘要', '', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        mem = SqliteLongTermMemory(path)
        row = mem.get_by_session("old")
        assert row["summary"] == "旧库摘要"
        assert row["owner"] == "", "迁移补列的默认值为空归属"

        mem.upsert("old", "迁移后摘要", owner="alice")
        assert mem.get_by_session("old")["owner"] == "alice"
        assert [r["session_id"] for r in mem.get_recent(5, owner="alice")] == ["old"]
        mem.close()
