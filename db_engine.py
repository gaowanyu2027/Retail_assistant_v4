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
            pool_size=5,          # 常驻连接数
            max_overflow=10,      # 峰值额外连接数
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

    close() 归还连接池。池化不可用时降级为裸连接（原行为）。
    """
    engine = get_engine()
    if engine is not None:
        try:
            return engine.raw_connection()
        except Exception as e:
            print(f"[DBEngine] 取池化连接失败，降级裸连接: {e}")
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
