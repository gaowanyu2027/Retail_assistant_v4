"""
基于 MySQL 的 LangGraph 检查点持久化（pymysql 实现，零新增依赖）

- 表：Retail_assistant.agent_checkpoints / agent_checkpoint_writes（自动建表）
- 语义与 langgraph 1.2.x 的 InMemorySaver 保持一致
- 连接懒加载 + 断线自动重连（MySQL 重启 / wait_timeout 后无需重建实例）
"""
import asyncio
import threading
from collections.abc import Iterator, Sequence
from typing import Any, Optional

import pymysql

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

from mysql_db import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB

__all__ = ["MysqlCheckpointer"]


def _pack(serde, obj) -> bytes:
    """serde.dumps_typed 返回 (type, bytes)，统一编码为 type + NUL + payload 以便存入 LONGBLOB。"""
    typ, payload = serde.dumps_typed(obj)
    return typ.encode("ascii") + b"\x00" + payload


def _unpack(serde, data: bytes):
    if not data:
        return None
    typ, _, payload = data.partition(b"\x00")
    return serde.loads_typed((typ.decode("ascii"), payload))


class MysqlCheckpointer(BaseCheckpointSaver):
    """线程安全的 MySQL 检查点存储。

    用法：实例化后先调用 _ensure_ready() 建表（工厂探测用），
    之后 put/get_tuple/list/put_writes 在首次使用时自动建连接。
    """

    DDL = [
        """
        CREATE TABLE IF NOT EXISTS agent_checkpoints (
            thread_id VARCHAR(128) NOT NULL,
            checkpoint_ns VARCHAR(64) NOT NULL DEFAULT '',
            checkpoint_id VARCHAR(64) NOT NULL,
            parent_checkpoint_id VARCHAR(64) NULL,
            checkpoint LONGBLOB NOT NULL,
            metadata LONGBLOB NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_checkpoint_writes (
            thread_id VARCHAR(128) NOT NULL,
            checkpoint_ns VARCHAR(64) NOT NULL DEFAULT '',
            checkpoint_id VARCHAR(64) NOT NULL,
            task_id VARCHAR(64) NOT NULL,
            idx INT NOT NULL,
            channel VARCHAR(128) NOT NULL,
            task_path TEXT NULL,
            value LONGBLOB NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ]

    def __init__(
        self,
        *,
        serde: Any = None,
        host: str = MYSQL_HOST,
        port: int = MYSQL_PORT,
        user: str = MYSQL_USER,
        password: str = MYSQL_PASSWORD,
        database: str = MYSQL_DB,
    ):
        super().__init__(serde=serde)
        self._lock = threading.RLock()
        self._conn: pymysql.connections.Connection | None = None
        self._params = dict(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
        )

    # ==================== 连接管理 ====================

    def _get_conn(self):
        if self._conn is None or not self._conn.open:
            self._conn = pymysql.connect(**self._params)
        return self._conn

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def close(self):
        with self._lock:
            self._close()

    def _ensure_ready(self):
        """建表（幂等）。供工厂探测 MySQL 是否可用。"""
        with self._lock:
            conn = self._get_conn()
            with conn.cursor() as cur:
                for ddl in self.DDL:
                    cur.execute(ddl)
        return self

    def _run(self, sql: str, params: Sequence[Any] = (), fetch: bool = False):
        """执行 SQL；连接失效时自动重连并重试一次。返回 fetchall 结果或 None。"""
        with self._lock:
            try:
                conn = self._get_conn()
            except pymysql.MySQLError:
                raise
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                rows = cur.fetchall() if fetch else None
                cur.close()
                return rows
            except pymysql.OperationalError:
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
                cur = self._get_conn().cursor()
                cur.execute(sql, params)
                rows = cur.fetchall() if fetch else None
                cur.close()
                return rows

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
        import random

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

        self._run(
            "INSERT INTO agent_checkpoints"
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint, metadata)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE"
            " parent_checkpoint_id=VALUES(parent_checkpoint_id),"
            " checkpoint=VALUES(checkpoint), metadata=VALUES(metadata)",
            (
                thread_id,
                checkpoint_ns,
                cid,
                parent,
                _pack(self.serde, checkpoint),
                _pack(self.serde, get_checkpoint_metadata(config, metadata)),
            ),
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
                    exists = self._run(
                        "SELECT 1 FROM agent_checkpoint_writes"
                        " WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s"
                        " AND task_id=%s AND idx=%s",
                        (thread_id, checkpoint_ns, cid, task_id, w_idx),
                        fetch=True,
                    )
                    if exists:
                        continue
                self._run(
                    "INSERT INTO agent_checkpoint_writes"
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, task_path, value)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE channel=VALUES(channel),"
                    " task_path=VALUES(task_path), value=VALUES(value)",
                    (
                        thread_id, checkpoint_ns, cid, task_id, w_idx,
                        channel, task_path, _pack(self.serde, value),
                    ),
                )

    # ==================== 读取 ====================

    def _row_to_tuple(self, row: tuple, config: Optional[RunnableConfig] = None) -> CheckpointTuple:
        thread_id, checkpoint_ns, cid, parent, cp_blob, md_blob = row
        checkpoint: Checkpoint = _unpack(self.serde, cp_blob)
        metadata: CheckpointMetadata = _unpack(self.serde, md_blob)

        write_rows = self._run(
            "SELECT task_id, channel, value FROM agent_checkpoint_writes"
            " WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s"
            " ORDER BY task_id, idx",
            (thread_id, checkpoint_ns, cid),
            fetch=True,
        )
        pending_writes = [
            (t, ch, _unpack(self.serde, v)) for t, ch, v in write_rows
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

        if cid:
            rows = self._run(
                "SELECT * FROM agent_checkpoints"
                " WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s",
                (thread_id, checkpoint_ns, cid),
                fetch=True,
            )
            if not rows:
                return None
            return self._row_to_tuple(rows[0], config=config)

        rows = self._run(
            "SELECT * FROM agent_checkpoints"
            " WHERE thread_id=%s AND checkpoint_ns=%s"
            " ORDER BY checkpoint_id DESC LIMIT 1",
            (thread_id, checkpoint_ns),
            fetch=True,
        )
        if not rows:
            return None
        return self._row_to_tuple(rows[0])

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
            where.append("thread_id = %s")
            params.append(config["configurable"]["thread_id"])
            ns = config["configurable"].get("checkpoint_ns")
            if ns is not None:
                where.append("checkpoint_ns = %s")
                params.append(ns)
            cid = get_checkpoint_id(config)
            if cid:
                where.append("checkpoint_id = %s")
                params.append(cid)
        if before is not None:
            bc = get_checkpoint_id(before)
            if bc:
                where.append("checkpoint_id < %s")
                params.append(bc)

        sql = "SELECT * FROM agent_checkpoints"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC"

        rows = self._run(sql, params, fetch=True) or []
        count = 0
        for row in rows:
            if filter is not None:
                md: CheckpointMetadata = _unpack(self.serde, row[5])
                if not all(qv == md.get(qk) for qk, qv in filter.items()):
                    continue
            if limit is not None and count >= limit:
                break
            count += 1
            yield self._row_to_tuple(row)
