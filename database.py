"""
SQLite 数据库操作模块 — 门店人脸表情记录持久化
"""
import sqlite3
import threading
import os
from datetime import datetime, timedelta
from collections import Counter

from config.settings import EMOTION_DB_PATH

# 线程锁：SQLite 在多线程写入时可能 database locked
_db_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None


def _mysql_enabled():
    """判断当前环境是否配置了 MySQL 密码。"""
    return bool(os.environ.get("mysql_root") or os.environ.get("MYSQL_PASSWORD"))


def _mysql_import():
    import mysql_db
    return mysql_db


def _connect():
    """获取并复用带多线程支持的 SQLite 连接"""
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(
            EMOTION_DB_PATH,
            check_same_thread=False,
            timeout=10.0,
        )
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        _db_conn.execute("PRAGMA busy_timeout=5000")
        _db_conn.execute("PRAGMA temp_store=MEMORY")
    return _db_conn


def init_db():
    """初始化数据库：**MySQL 与 SQLite 两条路径都要**把表建好。

    为什么必须都建（实测踩过）：本模块的设计是"平时写 MySQL，MySQL 挂了自动落
    SQLite"。但 SQLite 的表原先只在「MySQL 不可用」这个分支里创建，
    而 MySQL 正常启动时会 `return` 提前结束——于是 SQLite 里根本没有
    `emotion_record` 表。运行期 MySQL 一旦掉线，降级写入直接抛
    `no such table: emotion_record`，**这批表情数据被静默丢弃**
    （外层 except 只打一行日志，没有任何重试或告警）。

    实测复现：
        场景A（MySQL 可用）-> SQLite 文件甚至没被创建
        场景B（MySQL 掉线）-> OperationalError: no such table: emotion_record

    建表是幂等的（`IF NOT EXISTS`）且开销极低，两条路径都建好，
    降级路径才真正可用。
    """
    if _mysql_enabled():
        try:
            _mysql_import().init_schema()
        except Exception as e:
            print(f"[database] MySQL 初始化失败，回退 SQLite: {e}")
        # ⚠ 这里**不能 return**：SQLite 表必须始终建好，否则降级路径不可用

    with _db_lock:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS emotion_record (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT,
            capture_time TEXT,
            emotion TEXT,
            conf REAL
        )
        ''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_time_camera
        ON emotion_record(capture_time, camera_id)
        ''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_camera_time
        ON emotion_record(camera_id, capture_time)
        ''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_camera_id
        ON emotion_record(camera_id, id)
        ''')
        conn.commit()


def insert_record(camera_id, emotion, conf):
    """写入单条表情记录"""
    if _mysql_enabled():
        try:
            _mysql_import().insert_emotion_record(camera_id, emotion, conf)
            return
        except Exception as e:
            print(f"[database] MySQL 写入失败，回退 SQLite: {e}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO emotion_record(camera_id,capture_time,emotion,conf) VALUES (?,?,?,?)",
            (camera_id, now, emotion, conf)
        )
        conn.commit()


def insert_batch_records(camera_id, records):
    """
    批量写入表情记录，减少数据库 IO
    :param records: [(emotion, conf), ...]
    """
    if not records:
        return
    if _mysql_enabled():
        try:
            _mysql_import().insert_emotion_records(camera_id, records)
            return
        except Exception as e:
            print(f"[database] MySQL 批量写入失败，回退 SQLite: {e}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(camera_id, now, emotion, conf) for emotion, conf in records]
    with _db_lock:
        conn = _connect()
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT INTO emotion_record(camera_id,capture_time,emotion,conf) VALUES (?,?,?,?)",
            rows
        )
        conn.commit()


def get_statistic(start_time, end_time, camera_id=None):
    """
    按时间段统计表情数量
    :return: [(表情名称, 数量), ...]
    """
    if _mysql_enabled():
        try:
            return _mysql_import().get_emotion_statistic(start_time, end_time, camera_id)
        except Exception as e:
            print(f"[database] MySQL 统计失败，回退 SQLite: {e}")

    with _db_lock:
        conn = _connect()
        cursor = conn.cursor()
        if camera_id:
            cursor.execute('''
            SELECT emotion, COUNT(*) FROM emotion_record
            WHERE capture_time BETWEEN ? AND ? AND camera_id=?
            GROUP BY emotion
            ''', (start_time, end_time, camera_id))
        else:
            cursor.execute('''
            SELECT emotion, COUNT(*) FROM emotion_record
            WHERE capture_time BETWEEN ? AND ?
            GROUP BY emotion
            ''', (start_time, end_time))
        res = cursor.fetchall()
        return res


def get_latest_records(camera_id=None, limit=20):
    """查询最近的 N 条记录"""
    if _mysql_enabled():
        try:
            return _mysql_import().get_latest_emotion_records(camera_id, limit)
        except Exception as e:
            print(f"[database] MySQL 查询失败，回退 SQLite: {e}")

    with _db_lock:
        conn = _connect()
        cursor = conn.cursor()
        if camera_id:
            cursor.execute('''
            SELECT camera_id, capture_time, emotion, conf FROM emotion_record
            WHERE camera_id=?
            ORDER BY id DESC LIMIT ?
            ''', (camera_id, limit))
        else:
            cursor.execute('''
            SELECT camera_id, capture_time, emotion, conf FROM emotion_record
            ORDER BY id DESC LIMIT ?
            ''', (limit,))
        res = cursor.fetchall()
        return res


def get_record_count(camera_id=None, hours=24):
    """统计最近 N 小时内的记录总数"""
    if _mysql_enabled():
        try:
            return _mysql_import().get_emotion_record_count(camera_id, hours)
        except Exception as e:
            print(f"[database] MySQL 统计失败，回退 SQLite: {e}")

    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end.strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = _connect()
        cursor = conn.cursor()
        if camera_id:
            cursor.execute(
                "SELECT COUNT(*) FROM emotion_record "
                "WHERE capture_time BETWEEN ? AND ? AND camera_id=?",
                (start_str, end_str, camera_id),
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM emotion_record "
                "WHERE capture_time BETWEEN ? AND ?",
                (start_str, end_str),
            )
        return int(cursor.fetchone()[0])


def cleanup_old_records(days=30):
    """清理超过 N 天的旧记录，默认保留 30 天"""
    if _mysql_enabled():
        try:
            return _mysql_import().cleanup_emotion_records(days)
        except Exception as e:
            print(f"[database] MySQL 清理失败，回退 SQLite: {e}")

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM emotion_record WHERE capture_time < ?",
            (cutoff,)
        )
        deleted = cursor.rowcount
        conn.commit()
    return deleted


def majority_vote(emotions):
    """从表情列表中按出现次数取众数，平票时取列表中最近一次出现的"""
    if not emotions:
        return None
    counter = Counter(emotions)
    max_count = max(counter.values())
    candidates = [e for e, c in counter.items() if c == max_count]
    for e in reversed(emotions):
        if e in candidates:
            return e
    return candidates[0]


def generate_emotion_analysis(early, late):
    """生成前后半段表情对比分析结论"""
    early_dict = {k: v for k, v in early}
    late_dict = {k: v for k, v in late}
    positive = ["happy", "neutral"]
    negative = ["angry", "disgust", "fear", "sad"]

    early_pos = sum([early_dict.get(e, 0) for e in positive])
    early_neg = sum([early_dict.get(e, 0) for e in negative])
    late_pos = sum([late_dict.get(e, 0) for e in positive])
    late_neg = sum([late_dict.get(e, 0) for e in negative])

    total_early = early_pos + early_neg
    total_late = late_pos + late_neg
    if total_early == 0 or total_late == 0:
        return "本次采集数据不足，无法生成有效对比分析"

    early_pos_rate = round(early_pos / total_early * 100, 1)
    late_pos_rate = round(late_pos / total_late * 100, 1)
    pos_change = late_pos_rate - early_pos_rate

    if pos_change > 5:
        res = (f"【正面情绪提升】采集前期正面表情占比{early_pos_rate}%，"
               f"后期{late_pos_rate}%，顾客情绪整体变好，服务体验良好")
    elif pos_change < -5:
        res = (f"【负面情绪增多】采集前期正面表情占比{early_pos_rate}%，"
               f"后期{late_pos_rate}%，顾客情绪明显变差，需检查服务流程存在问题")
    else:
        res = (f"【情绪无明显变化】采集前期正面表情占比{early_pos_rate}%，"
               f"后期{late_pos_rate}%，顾客情绪稳定，服务无明显波动")
    return res
