"""
SQLAlchemy 连接池封装 — 为 mysql_db 提供复用的 MySQL 连接

为什么引入：
- 旧实现每次查询都 pymysql.connect() 新建 TCP 连接（约 5-20ms 开销）
- 池化后复用连接，pool_pre_ping 自动处理 MySQL wait_timeout 断线
- 与 SQLAlchemy 2.0 兼容（环境已装 sqlalchemy 2.0.51，零新增依赖）

设计：
- get_connection() 返回 DBAPI 层连接（engine.raw_connection()），
  对调用方完全透明：cursor()/close() 语义与旧 pymysql 连接一致，
  close() 归还连接池而非真正断开
- 连接参数与旧实现一致：autocommit=True、utf8mb4、connect_timeout=10
- 池化不可用（缺包/初始化失败）时自动降级为裸 pymysql 连接，业务零影响
"""
import os
import threading
import time

import pymysql

from mysql_db import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB

_engine = None
_engine_lock = threading.Lock()
# 池化失败后不再永久禁用：记录失败时间，30s 后自动重试建池，
# 避免 MySQL 瞬时不可用导致进程期内一直退化为裸连接
_pool_fail_ts: float = 0.0
_POOL_RETRY_INTERVAL = 30.0

# 池容量与等待上限（显式定义，便于在错误信息里说明，也避免依赖 SQLAlchemy 默认值）
POOL_SIZE = 5
MAX_OVERFLOW = 10
# ⚠ 原来是**隐式默认 30 秒**。对 Web 请求来说 30s 过长——池一紧张，
# 每个溢出调用者都先白等 30s 才降级，把请求线程一起拖住。
# 显式压到 5s：要么尽快拿到连接，要么尽快失败。
POOL_TIMEOUT = 5


class DBPoolBusy(RuntimeError):
    """连接池已满：应当**快速失败**，而不是无限新建裸连接。

    历史问题：池满时这里会 `pymysql.connect()` 新开一条**不受限**的裸连接，
    N 个并发调用者就新建 N 条。实测（占满 15 个池位后）：

        30 次调用 → 新建 30 条裸连接，且每个调用者先白等 pool_timeout

    并发再高就会撞 MySQL 的 `max_connections`（本项目容器里是 151），
    把整个库拖垮——从"部分请求变慢"升级为"全站不可用"。
    这种"降级反而放大故障"的路径，比直接失败更危险。

    现在池满即抛本异常，由上层决定限流/降级，而不是在这一层无限开连接。

    ⚠ 注意 `pooled_connection()` 里**保留**了另一处裸连接兜底，那是
    `engine is None`（池压根没建起来，例如启动时 MySQL 不可达 / 缺驱动）的
    **稳态**降级——与"池满"是两回事，不要一起删。
    """


def _build_engine():
    """创建 SQLAlchemy 引擎（带连接池）。失败时记录时间并返回 None（可重试）。"""
    global _pool_fail_ts
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL
        from sqlalchemy.pool import QueuePool

        url = URL.create(
            "mysql+pymysql",
            username=MYSQL_USER,
            password=MYSQL_PASSWORD,
            host=MYSQL_HOST,
            port=int(MYSQL_PORT),
            database=MYSQL_DB,
            query={"charset": "utf8mb4"},
        )
        engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=POOL_SIZE,          # 常驻连接数
            max_overflow=MAX_OVERFLOW,    # 峰值额外连接数
            pool_timeout=POOL_TIMEOUT,    # 等不到空闲连接的等待上限（见上方说明）
            pool_recycle=3600,    # 1 小时回收，避开 MySQL wait_timeout
            pool_pre_ping=True,   # checkout 前 SELECT 1，断线自动重建
            connect_args={"connect_timeout": 10, "autocommit": True},
        )
        # 探活：立即 checkout 一次，失败视为池化不可用
        with engine.connect():
            pass
        _pool_fail_ts = 0.0
        return engine
    except Exception as e:
        print(f"[DBEngine] 连接池初始化失败，30s 后重试（当前降级为裸 pymysql）: {e}")
        _pool_fail_ts = time.time()
        return None


def get_engine():
    """返回进程级共享引擎（线程安全单例；失败后按间隔自动重试建池）。"""
    import time
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                if _pool_fail_ts == 0.0 or time.time() - _pool_fail_ts >= _POOL_RETRY_INTERVAL:
                    _engine = _build_engine()
    return _engine


def pooled_connection():
    """从连接池取出一个 DBAPI 连接（pymysql 连接，语义与旧实现一致）。

    close() 归还连接池。

    两种"拿不到池化连接"的情况，处理方式**有意不同**：

    - `engine is None`（池没建起来）→ 降级裸连接。这是稳态降级，
      例如启动时 MySQL 不可达、或驱动缺失；每次请求一条连接但都由调用方
      close()，不会堆积。
    - `raw_connection()` 抛异常（**池满** / pre_ping 失败）→ 抛 `DBPoolBusy`。
      这里**刻意不再**新建裸连接：实测池满后 30 次调用会新建 30 条不受限的连接，
      并发再高就撞 `max_connections` 把全库拖垮。快速失败 >> 降级放大故障。
    """
    engine = get_engine()
    if engine is not None:
        try:
            return engine.raw_connection()
        except Exception as e:
            raise DBPoolBusy(
                f"连接池已满（容量 {POOL_SIZE}+{MAX_OVERFLOW}，等待 {POOL_TIMEOUT}s 仍无空闲连接）"
                f"，请稍后重试: {e}"
            ) from e
    return pymysql.connect(
        host=MYSQL_HOST,
        port=int(MYSQL_PORT),
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )


def dispose_engine():
    """释放全部池化连接（测试/关闭时用）。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
            _engine = None


if __name__ == "__main__":
    print("engine:", get_engine())
    conn = pooled_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s", (MYSQL_DB,))
        print("tables in db:", cur.fetchone()[0])
    conn.close()
    print("pool ok, connection returned to pool")
    dispose_engine()
