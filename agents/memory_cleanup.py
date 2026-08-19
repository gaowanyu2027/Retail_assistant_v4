"""
Agent 存储清理策略 — 检查点 / 长期记忆 / 日志

背景：长会话会产生大量 LangGraph 检查点（agent 每步写一行），
工具日志/语音日志/热度汇报持续累积，长期记忆也不断增长。
LangGraph 恢复只依赖每个 thread 的最新检查点，旧的可安全清理。

策略（全部可配置，见 config/settings.py）：
1. 每个会话保留最新 N 个检查点（默认 20），连同其 pending writes 一并清理
2. 清理无主 checkpoint_writes（孤儿写入）
3. 长期记忆保留最新 K 条（默认 200）
4. agent_tool_log / voice_command_log 保留 N 天（默认 30 天）
5. heat_report 保留最新 M 条（默认 500）

存储约定与项目一致：MySQL 优先（Retail_assistant），不可用时回退 SQLite。
由 api/main.py 的缓存清理线程周期性调用，幂等、失败不影响主流程。
"""
from datetime import datetime, timedelta

from config.settings import (
    AGENT_CHECKPOINT_KEEP_PER_THREAD,
    AGENT_LONG_TERM_KEEP_RECORDS,
    AGENT_LOG_KEEP_DAYS,
    AGENT_HEAT_REPORT_KEEP_RECORDS,
    DATA_DIR,
)


# ==================== MySQL 后端 ====================

def _mysql_cleanup() -> dict:
    import mysql_db

    stats: dict = {"checkpoints": 0, "orphan_writes": 0, "long_term": 0,
                   "tool_log": 0, "voice_log": 0, "heat_report": 0}
    conn = mysql_db.get_connection()
    try:
        with conn.cursor() as cur:
            # 1) 每个 thread 保留最新 N 个检查点（uuid6 时间有序，DESC 即最新在前）
            cur.execute(
                "DELETE c FROM agent_checkpoints c "
                "LEFT JOIN ("
                " SELECT thread_id, checkpoint_ns, checkpoint_id,"
                "        ROW_NUMBER() OVER (PARTITION BY thread_id, checkpoint_ns"
                "        ORDER BY checkpoint_id DESC) rn"
                " FROM agent_checkpoints"
                ") k ON c.thread_id=k.thread_id AND c.checkpoint_ns=k.checkpoint_ns"
                "     AND c.checkpoint_id=k.checkpoint_id"
                " WHERE k.rn > %s",
                (AGENT_CHECKPOINT_KEEP_PER_THREAD,),
            )
            stats["checkpoints"] = cur.rowcount

            # 2) 清理无主 writes（被删检查点的 pending writes）
            cur.execute(
                "DELETE w FROM agent_checkpoint_writes w "
                "LEFT JOIN agent_checkpoints c"
                " ON w.thread_id=c.thread_id AND w.checkpoint_ns=c.checkpoint_ns"
                "    AND w.checkpoint_id=c.checkpoint_id"
                " WHERE c.checkpoint_id IS NULL"
            )
            stats["orphan_writes"] = cur.rowcount

            # 3) 长期记忆保留最新 K 条
            cur.execute(
                "DELETE FROM agent_long_term_memory WHERE id NOT IN ("
                " SELECT id FROM ("
                "  SELECT id FROM agent_long_term_memory"
                "  ORDER BY updated_at DESC, id DESC LIMIT %s"
                " ) t)",
                (AGENT_LONG_TERM_KEEP_RECORDS,),
            )
            stats["long_term"] = cur.rowcount

            # 4) 工具/语音日志保留 N 天
            cutoff = (datetime.now() - timedelta(days=AGENT_LOG_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("DELETE FROM agent_tool_log WHERE created_at < %s", (cutoff,))
            stats["tool_log"] = cur.rowcount
            cur.execute("DELETE FROM voice_command_log WHERE created_at < %s", (cutoff,))
            stats["voice_log"] = cur.rowcount

            # 5) 热度汇报保留最新 M 条
            cur.execute(
                "DELETE FROM heat_report WHERE id NOT IN ("
                " SELECT id FROM ("
                "  SELECT id FROM heat_report ORDER BY id DESC LIMIT %s"
                " ) t)",
                (AGENT_HEAT_REPORT_KEEP_RECORDS,),
            )
            stats["heat_report"] = cur.rowcount
    finally:
        conn.close()
    return stats


# ==================== SQLite 回退后端 ====================

def _sqlite_cleanup() -> dict:
    import sqlite3

    stats: dict = {"checkpoints": 0, "orphan_writes": 0, "long_term": 0,
                   "tool_log": 0, "voice_log": 0, "heat_report": 0}

    cp_path = DATA_DIR / "agent_checkpoints.db"
    if cp_path.exists():
        conn = sqlite3.connect(str(cp_path), timeout=10.0, autocommit=True)
        try:
            # 每个 thread 保留最新 N 个
            cur = conn.execute(
                "DELETE FROM checkpoints WHERE rowid IN ("
                " SELECT c.rowid FROM checkpoints c"
                " LEFT JOIN ("
                "  SELECT thread_id, checkpoint_ns, checkpoint_id,"
                "         ROW_NUMBER() OVER (PARTITION BY thread_id, checkpoint_ns"
                "         ORDER BY checkpoint_id DESC) rn"
                "  FROM checkpoints"
                " ) k ON c.thread_id=k.thread_id AND c.checkpoint_ns=k.checkpoint_ns"
                "     AND c.checkpoint_id=k.checkpoint_id"
                " WHERE k.rn > ?)",
                (AGENT_CHECKPOINT_KEEP_PER_THREAD,),
            )
            stats["checkpoints"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM checkpoint_writes WHERE rowid IN ("
                " SELECT w.rowid FROM checkpoint_writes w"
                " LEFT JOIN checkpoints c"
                "  ON w.thread_id=c.thread_id AND w.checkpoint_ns=c.checkpoint_ns"
                "     AND w.checkpoint_id=c.checkpoint_id"
                " WHERE c.checkpoint_id IS NULL)"
            )
            stats["orphan_writes"] = cur.rowcount
        finally:
            conn.close()

    ltm_path = DATA_DIR / "long_term_memory.db"
    if ltm_path.exists():
        conn = sqlite3.connect(str(ltm_path), timeout=10.0, autocommit=True)
        try:
            cur = conn.execute(
                "DELETE FROM agent_long_term_memory WHERE id NOT IN ("
                " SELECT id FROM agent_long_term_memory"
                " ORDER BY updated_at DESC, id DESC LIMIT ?)",
                (AGENT_LONG_TERM_KEEP_RECORDS,),
            )
            stats["long_term"] = cur.rowcount
        finally:
            conn.close()
    return stats


# ==================== 统一入口 ====================

def cleanup_agent_storage() -> dict:
    """执行 Agent 存储清理（MySQL 优先，SQLite 回退），返回各表删除行数。

    幂等；任何失败只打印日志，不影响主流程。
    """
    try:
        import mysql_db
        if mysql_db.mysql_available():
            stats = _mysql_cleanup()
            deleted = sum(stats.values())
            if deleted:
                print(
                    f"[MemoryCleanup] MySQL 清理完成: 检查点 {stats['checkpoints']}、"
                    f"孤儿写入 {stats['orphan_writes']}、长期记忆 {stats['long_term']}、"
                    f"工具日志 {stats['tool_log']}、语音日志 {stats['voice_log']}、"
                    f"热度汇报 {stats['heat_report']}"
                )
            return stats
    except Exception as e:
        print(f"[MemoryCleanup][WARN] MySQL 清理失败，尝试 SQLite: {e}")

    try:
        stats = _sqlite_cleanup()
        deleted = sum(stats.values())
        if deleted:
            print(f"[MemoryCleanup] SQLite 清理完成: {stats}")
        return stats
    except Exception as e:
        print(f"[MemoryCleanup][WARN] SQLite 清理失败: {e}")
        return {}


if __name__ == "__main__":
    import json
    print(json.dumps(cleanup_agent_storage(), ensure_ascii=False, indent=2))
