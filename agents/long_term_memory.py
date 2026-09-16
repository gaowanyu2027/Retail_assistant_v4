"""
长期记忆存储 — 跨会话主题摘要（记忆分层中的"长期"层）

- 短期记忆：LangGraph 检查点按 thread_id 保存会话内消息（见 checkpointer）
- 长期记忆：本模块按 session_id 保存"被压缩掉的早期对话摘要"，
  供新会话开始时注入 system 上下文，实现跨会话连续性。

存储约定与项目一致：MySQL 优先（Retail_assistant.agent_long_term_memory），
MySQL 不可用时自动回退 SQLite（data/long_term_memory.db）并打印日志。
"""
import sqlite3
import threading
from datetime import datetime

from config.settings import DATA_DIR

# ==================== 建表 DDL（MySQL / SQLite 同构） ====================

MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS agent_long_term_memory (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    summary TEXT NOT NULL,
    keywords VARCHAR(512) NOT NULL DEFAULT '',
    message_count INT UNSIGNED NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    owner VARCHAR(64) NOT NULL DEFAULT '',
    UNIQUE KEY uq_ltm_session (session_id),
    KEY idx_ltm_owner (owner)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS agent_long_term_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT ''
)
"""

# ⚠ owner 一律加在**最后一列**：新装走 DDL、老库走 ALTER，两者列序一致，
# `_row_to_dict` 才能继续用固定下标取值（否则新老库下标会错位）。
MYSQL_ALTER_OWNER = (
    "ALTER TABLE agent_long_term_memory "
    "ADD COLUMN owner VARCHAR(64) NOT NULL DEFAULT '', "
    "ADD INDEX idx_ltm_owner (owner)"
)
SQLITE_ALTER_OWNER = (
    "ALTER TABLE agent_long_term_memory ADD COLUMN owner TEXT NOT NULL DEFAULT ''"
)


class LongTermMemory:
    """长期记忆统一接口：upsert / get_recent / get_by_session / search / count。

    子类（MySQL/SQLite 后端）实现 _execute 等差异逻辑。

    `owner`（B3）：摘要来自用户对话，**必须带归属** —— 否则新会话会把
    「其他人历史会话的摘要」注入到自己的上下文里（LLM 会直接读出来）。
    `owner=None` 表示不限定（审计视角）。
    """

    def upsert(self, session_id: str, summary: str, keywords: str = "", message_count: int = 0,
               owner: str = ""):
        raise NotImplementedError

    def get_recent(self, limit: int = 3, owner: str | None = None) -> list[dict]:
        raise NotImplementedError

    def get_by_session(self, session_id: str) -> dict | None:
        raise NotImplementedError

    def search(self, keyword: str, limit: int = 5, owner: str | None = None) -> list[dict]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError


class MysqlLongTermMemory(LongTermMemory):
    """MySQL 后端（pymysql，autocommit）。"""

    def __init__(self):
        import pymysql
        from mysql_db import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB
        self._params = dict(
            host=MYSQL_HOST, port=int(MYSQL_PORT), user=MYSQL_USER,
            password=MYSQL_PASSWORD, database=MYSQL_DB,
            charset="utf8mb4", autocommit=True, connect_timeout=10,
        )
        self._lock = threading.RLock()
        self._conn: pymysql.connections.Connection | None = None
        self._ensure_ready()

    def _conn_get(self):
        import pymysql
        if self._conn is None or not self._conn.open:
            self._conn = pymysql.connect(**self._params)
        return self._conn

    def _ensure_ready(self):
        with self._lock:
            cur = self._conn_get().cursor()
            cur.execute(MYSQL_DDL)
            # B3 迁移：老库补 owner 列（新装已由 DDL 带上）
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=DATABASE() "
                "AND table_name='agent_long_term_memory' AND column_name='owner'"
            )
            if int(cur.fetchone()[0]) == 0:
                cur.execute(MYSQL_ALTER_OWNER)
            cur.close()

    def _run(self, sql: str, params=(), fetch: bool = False):
        """执行 SQL；连接失效（MySQL 重启/wait_timeout）时自动重连并重试一次。"""
        import pymysql
        with self._lock:
            cur = self._conn_get().cursor()
            try:
                try:
                    cur.execute(sql, params)
                    rows = cur.fetchall() if fetch else None
                    return rows
                except pymysql.OperationalError:
                    # 连接已死：关闭并重建后重试一次（对齐 mysql_checkpointer._run）
                    self.close()
                    cur = self._conn_get().cursor()
                    cur.execute(sql, params)
                    rows = cur.fetchall() if fetch else None
                    return rows
            finally:
                cur.close()

    def _row_to_dict(self, row) -> dict:
        return {
            "id": row[0], "session_id": row[1], "summary": row[2],
            "keywords": row[3], "message_count": row[4],
            "created_at": str(row[5]) if row[5] else "",
            "updated_at": str(row[6]) if row[6] else "",
            "owner": (row[7] or "") if len(row) > 7 else "",
        }

    def upsert(self, session_id, summary, keywords="", message_count=0, owner=""):
        self._run(
            "INSERT INTO agent_long_term_memory(session_id, summary, keywords, message_count, owner)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE"
            " summary=VALUES(summary), keywords=VALUES(keywords),"
            " message_count=VALUES(message_count), owner=VALUES(owner),"
            " updated_at=CURRENT_TIMESTAMP",
            (session_id, summary[:4000], (keywords or "")[:500], int(message_count or 0),
             owner or ""),
        )

    def get_recent(self, limit: int = 3, owner: str | None = None):
        rows = self._run(
            "SELECT * FROM agent_long_term_memory"
            " WHERE (%s IS NULL OR owner=%s)"
            " ORDER BY updated_at DESC, id DESC LIMIT %s",
            (owner, owner, int(limit)), fetch=True,
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    def get_by_session(self, session_id: str):
        rows = self._run(
            "SELECT * FROM agent_long_term_memory WHERE session_id=%s",
            (session_id,), fetch=True,
        )
        return self._row_to_dict(rows[0]) if rows else None

    def search(self, keyword: str, limit: int = 5, owner: str | None = None):
        pattern = f"%{keyword.strip()}%"
        rows = self._run(
            "SELECT * FROM agent_long_term_memory"
            " WHERE (summary LIKE %s OR keywords LIKE %s)"
            " AND (%s IS NULL OR owner=%s)"
            " ORDER BY updated_at DESC LIMIT %s",
            (pattern, pattern, owner, owner, int(limit)), fetch=True,
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    def count(self):
        rows = self._run("SELECT COUNT(*) FROM agent_long_term_memory", fetch=True)
        return int(rows[0][0]) if rows else 0

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class SqliteLongTermMemory(LongTermMemory):
    """SQLite 回退后端（标准库 sqlite3，autocommit=True）。"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or (DATA_DIR / "long_term_memory.db"))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, autocommit=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(SQLITE_DDL)
        # B3 迁移：老库补 owner 列（新装已由 DDL 带上）
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(agent_long_term_memory)")]
        if "owner" not in cols:
            try:
                self._conn.execute(SQLITE_ALTER_OWNER)
            except sqlite3.OperationalError as e:
                print(f"[LongTermMemory][WARN] SQLite 补 owner 列失败（可能并发迁移）: {e}")

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _row_to_dict(self, row) -> dict:
        return {
            "id": row[0], "session_id": row[1], "summary": row[2],
            "keywords": row[3], "message_count": row[4],
            "created_at": row[5] or "", "updated_at": row[6] or "",
            "owner": (row[7] or "") if len(row) > 7 else "",
        }

    def upsert(self, session_id, summary, keywords="", message_count=0, owner=""):
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_long_term_memory(session_id, summary, keywords, message_count, created_at, updated_at, owner)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " summary=excluded.summary, keywords=excluded.keywords,"
                " message_count=excluded.message_count, owner=excluded.owner,"
                " updated_at=excluded.updated_at",
                (session_id, summary[:4000], (keywords or "")[:500], int(message_count or 0),
                 now, now, owner or ""),
            )

    def get_recent(self, limit: int = 3, owner: str | None = None):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_long_term_memory"
                " WHERE (? IS NULL OR owner=?)"
                " ORDER BY updated_at DESC, id DESC LIMIT ?",
                (owner, owner, int(limit)),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_by_session(self, session_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_long_term_memory WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def search(self, keyword: str, limit: int = 5, owner: str | None = None):
        pattern = f"%{keyword.strip()}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_long_term_memory"
                " WHERE (summary LIKE ? OR keywords LIKE ?)"
                " AND (? IS NULL OR owner=?)"
                " ORDER BY updated_at DESC LIMIT ?",
                (pattern, pattern, owner, owner, int(limit)),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM agent_long_term_memory").fetchone()
        return int(row[0]) if row else 0

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ==================== 单例工厂（MySQL 优先，SQLite 回退） ====================

_memory_singleton: LongTermMemory | None = None
_memory_choice: str | None = None
_factory_lock = threading.Lock()


def get_long_term_memory() -> LongTermMemory:
    """返回进程级共享的长期记忆实例（线程安全单例）。

    与 create_memory 相同的约定：优先 MySQL，不可用时回退 SQLite。
    """
    global _memory_singleton, _memory_choice
    if _memory_singleton is not None:
        return _memory_singleton

    with _factory_lock:
        if _memory_singleton is not None:
            return _memory_singleton

        # 1) 优先 MySQL
        try:
            import mysql_db
            if mysql_db.mysql_available():
                _memory_singleton = MysqlLongTermMemory()
                _memory_choice = "mysql"
                print("[LongTermMemory] 长期记忆持久化: MySQL (Retail_assistant.agent_long_term_memory)")
                return _memory_singleton
        except Exception as e:
            print(f"[LongTermMemory][WARN] MySQL 初始化失败，回退 SQLite: {e}")

        # 2) 回退 SQLite
        _memory_singleton = SqliteLongTermMemory()
        _memory_choice = "sqlite"
        print(f"[LongTermMemory] 长期记忆持久化: SQLite ({_memory_singleton.db_path})")
        return _memory_singleton


if __name__ == "__main__":
    ltm = get_long_term_memory()
    print("backend:", _memory_choice)
    ltm.upsert("test-session", "这是测试摘要", "测试,摘要", 3)
    print("recent:", ltm.get_recent(3))
    print("search:", ltm.search("测试"))
    print("count:", ltm.count())
