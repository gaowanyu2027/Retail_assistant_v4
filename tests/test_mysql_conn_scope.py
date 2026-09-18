"""`mysql_db.get_connection()` 的**局部导入作用域**回归测试（潜伏缺陷）。

缺陷（实测复现）：
```python
def get_connection(database: str = MYSQL_DB):
    if database == MYSQL_DB:
        try:
            import db_engine       # ← 局部导入，使 db_engine 成为整个函数的局部名
        except Exception:
            db_engine = None
    ...
    if db_engine is not None and db_engine.breaker_open():   # ← 传别的库名时从未赋值
```
于是 `get_connection("<别的库>")` 抛的不是连接错误，而是
`UnboundLocalError: cannot access local variable 'db_engine'`。

发现经过：为验证"CI 里 MySQL 是空库"这条修复，本地新建了一个全新库做对照，
`get_connection("eval_fresh_probe")` 一调就炸（见 改进记录 模块 C 续 第 8 条）。

本测试**不连真库**：把 `pymysql.connect` 换成桩，只验证会走到裸连接分支且参数正确；
另有一条把 `db_engine` 换成假模块，验证文档里明确要求的
"**DBPoolBusy 必须原样抛出**，不能降级成裸连接"这条契约。
（断言信息里会屏蔽口令，避免测试失败时把凭据打进日志。）
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import mysql_db  # noqa: E402

_SENSITIVE = ("password", "passwd", "api_key", "token")


def _mask(kwargs: dict) -> dict:
    return {k: ("***" if any(s in k.lower() for s in _SENSITIVE) else v)
            for k, v in (kwargs or {}).items()}


class _StubConn:
    """假的 pymysql 连接对象（只记录，不用真连）。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_other_database_uses_raw_connection_without_unbound_error():
    """修复前：传别的库名 → UnboundLocalError（而不是去连库）。"""
    calls: list = []
    orig_connect = mysql_db.pymysql.connect
    mysql_db.pymysql.connect = lambda **kw: (calls.append(kw), _StubConn(**kw))[1]
    try:
        conn = mysql_db.get_connection("eval_fresh_probe")
    finally:
        mysql_db.pymysql.connect = orig_connect
    assert isinstance(conn, _StubConn), conn
    assert calls, "没走到裸连接分支"
    assert calls[0]["database"] == "eval_fresh_probe", _mask(calls[0])
    assert calls[0]["connect_timeout"] == 3, "降级路径不该用长超时（实测 8s 会拖垮请求）"


def test_pool_busy_is_raised_not_swallowed():
    """文档契约：池满(DBPoolBusy) 必须**原样抛出**。

    为什么重要（见 get_connection 的 docstring）：吞掉它再新建一条不受限的裸连接，
    等于在 db_engine 的兜底之上又叠一层，并发一高就会撞 MySQL 的 max_connections。
    """
    import types

    fake = types.ModuleType("db_engine")

    class DBPoolBusy(Exception):
        pass

    class DBUnavailable(Exception):
        pass

    def pooled_connection():
        raise DBPoolBusy("池满")

    fake.DBPoolBusy, fake.DBUnavailable = DBPoolBusy, DBUnavailable
    fake.pooled_connection = pooled_connection
    fake.breaker_open = lambda: False
    fake.breaker_record = lambda ok: None

    old = sys.modules.get("db_engine")
    sys.modules["db_engine"] = fake
    try:
        try:
            mysql_db.get_connection(mysql_db.MYSQL_DB)
        except DBPoolBusy:
            return                      # ✅ 原样抛出 = 符合契约
        except Exception as e:
            raise AssertionError(f"DBPoolBusy 被换成了别的异常: {type(e).__name__}: {e}")
        raise AssertionError("DBPoolBusy 没抛出 → 会叠加裸连接，并发高时撞 max_connections")
    finally:
        if old is not None:
            sys.modules["db_engine"] = old
        else:
            sys.modules.pop("db_engine", None)


def test_default_argument_is_the_project_database():
    """默认参数就是项目库名（CI 的 MYSQL_DATABASE 必须与它一致，否则又是一种静默错配）。"""
    import inspect

    sig = inspect.signature(mysql_db.get_connection)
    assert sig.parameters["database"].default == "retail_assistant", sig
    assert mysql_db.MYSQL_DB == "retail_assistant", mysql_db.MYSQL_DB
