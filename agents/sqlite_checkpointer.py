"""
基于 SQLite 的 LangGraph 检查点持久化

替代进程内 InMemorySaver：
- Agent 多轮对话记忆按 thread_id(=session_id) 隔离
- 存储在 data/agent_checkpoints.db，服务重启 / Agent 重建后记忆仍可恢复
- 语义与 langgraph 1.2.x 的 InMemorySaver 保持一致（版本号格式、pending writes 结构等）
- 仅依赖标准库 sqlite3，无新增第三方包
"""
import asyncio
import atexit
import os
import random
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from typing import Any, Optional

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.memory import WRITES_IDX_MAP
from langchain_core.runnables import RunnableConfig

__all__ = ["SqliteCheckpointer"]


class SqliteCheckpointer(BaseCheckpointSaver):
    """线程安全的 SQLite 检查点存储。

    与 InMemorySaver 的差异：checkpoint（含 channel_values）整包序列化存入
    checkpoints 表（不做增量 blob 拆分）；pending writes 存 checkpoint_writes 表。
    对 LangGraph 运行时而言两种存储等价。
    """

    def __init__(self, db_path: str | os.PathLike, *, serde: Any = None):
        super().__init__(serde=serde)
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        # autocommit=True：每条语句立即提交，避免隐式事务在连接关闭时回滚
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=15.0, autocommit=True
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._init_schema()
        atexit.register(self.close)

    def _init_schema(self):
        with self._lock:
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                checkpoint BLOB NOT NULL,
                metadata BLOB NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """)
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoint_writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                task_path TEXT NOT NULL DEFAULT '',
                value BLOB NOT NULL,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cp_latest "
                "ON checkpoints(thread_id, checkpoint_ns, checkpoint_id)"
            )

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ==================== 序列化辅助 ====================

    @staticmethod
    def _pack(serde, obj) -> bytes:
        """serde.dumps_typed 返回 (type, bytes)，统一编码为 type + NUL + payload 以便存入 BLOB。"""
        typ, payload = serde.dumps_typed(obj)
        return typ.encode("ascii") + b"\x00" + payload

    @staticmethod
    def _unpack(serde, data: bytes):
        if not data:
            return None
        typ, _, payload = data.partition(b"\x00")
        return serde.loads_typed((typ.decode("ascii"), payload))

    # ==================== 异步包装（langgraph 1.2.x Base 默认 raise NotImplementedError） ====================

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config=None, *, filter=None, before=None, limit=None):
        return await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )

    # ==================== 版本号（与 InMemorySaver 一致） ====================

    def get_next_version(self, current: Any, channel: Any = None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    # ==================== 写入 ====================

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Any,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        cid = checkpoint["id"]
        parent = config["configurable"].get("checkpoint_id")

        cp_blob = self._pack(self.serde, checkpoint)
        md_blob = self._pack(self.serde, get_checkpoint_metadata(config, metadata))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints"
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint, metadata)"
                " VALUES (?,?,?,?,?,?)",
                (thread_id, checkpoint_ns, cid, parent, cp_blob, md_blob),
            )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": cid,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        cid = config["configurable"]["checkpoint_id"]

        with self._lock:
            for idx, (channel, value) in enumerate(writes):
                w_idx = WRITES_IDX_MAP.get(channel, idx)
                if w_idx >= 0:
                    exists = self._conn.execute(
                        "SELECT 1 FROM checkpoint_writes"
                        " WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=? AND task_id=? AND idx=?",
                        (thread_id, checkpoint_ns, cid, task_id, w_idx),
                    ).fetchone()
                    if exists:
                        continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO checkpoint_writes"
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, task_path, value)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        thread_id, checkpoint_ns, cid, task_id, w_idx,
                        channel, task_path, self._pack(self.serde, value),
                    ),
                )

    # ==================== 读取 ====================

    def _row_to_tuple(self, row: tuple, config: Optional[RunnableConfig] = None) -> CheckpointTuple:
        thread_id, checkpoint_ns, cid, parent, cp_blob, md_blob = row
        checkpoint: Checkpoint = self._unpack(self.serde, cp_blob)
        metadata: CheckpointMetadata = self._unpack(self.serde, md_blob)

        write_rows = self._conn.execute(
            "SELECT task_id, channel, value FROM checkpoint_writes"
            " WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?"
            " ORDER BY task_id, idx",
            (thread_id, checkpoint_ns, cid),
        ).fetchall()
        pending_writes = [
            (t, ch, self._unpack(self.serde, v)) for t, ch, v in write_rows
        ]

        cfg = config or {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": cid,
            }
        }
        return CheckpointTuple(
            config=cfg,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent,
                    }
                }
                if parent
                else None
            ),
            pending_writes=pending_writes,
        )

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        cid = get_checkpoint_id(config)

        with self._lock:
            if cid:
                row = self._conn.execute(
                    "SELECT * FROM checkpoints"
                    " WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                    (thread_id, checkpoint_ns, cid),
                ).fetchone()
                if row is None:
                    return None
                return self._row_to_tuple(row, config=config)

            row = self._conn.execute(
                "SELECT * FROM checkpoints"
                " WHERE thread_id=? AND checkpoint_ns=?"
                " ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id, checkpoint_ns),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_tuple(row)

    def list(
        self,
        config: Optional[RunnableConfig] = None,
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        where: list[str] = []
        params: list[Any] = []
        if config is not None:
            where.append("thread_id = ?")
            params.append(config["configurable"]["thread_id"])
            ns = config["configurable"].get("checkpoint_ns")
            if ns is not None:
                where.append("checkpoint_ns = ?")
                params.append(ns)
            cid = get_checkpoint_id(config)
            if cid:
                where.append("checkpoint_id = ?")
                params.append(cid)
        if before is not None:
            bc = get_checkpoint_id(before)
            if bc:
                where.append("checkpoint_id < ?")
                params.append(bc)

        sql = "SELECT * FROM checkpoints"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            count = 0
            for row in rows:
                if filter is not None:
                    md: CheckpointMetadata = self._unpack(self.serde, row[5])
                    if not all(qv == md.get(qk) for qk, qv in filter.items()):
                        continue
                if limit is not None and count >= limit:
                    break
                count += 1
                yield self._row_to_tuple(row)
