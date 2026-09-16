"""
MySQL 数据层 — Retail_assistant
连接信息通过环境变量配置，密码不写进代码。
"""
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pymysql

from config.settings import (
    PROJECT_ROOT,
    ROI_CONFIG_PATH,
    LOCAL_ROI_CONFIG_PATH,
    CHAT_SEARCH_LIMIT,
)

MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("mysql_root") or os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB = "retail_assistant"  # Docker MySQL(Linux) 库名大小写敏感，与容器内库名一致


# 同会话 seq_no 的「读-改-写」串行锁（原因见 save_query_history 的 docstring）。
# 单进程部署下用它即可；多进程化时需换成 DB 层方案。
_seq_no_lock = threading.Lock()


def mysql_available() -> bool:
    """是否配置了 MySQL 密码，用于决定优先写 MySQL。"""
    return bool(os.environ.get("mysql_root") or os.environ.get("MYSQL_PASSWORD"))


def get_connection(database: str = MYSQL_DB):
    """返回 MySQL 连接（SQLAlchemy 连接池；池化不可用时降级裸 pymysql）。

    返回的连接语义与旧实现完全一致（cursor()/close()/autocommit），
    close() 归还连接池而非真正断开。

    ⚠ 对 `DBPoolBusy`（池满）**必须原样抛出**，不能走下面的通用降级：
    实测这条 `except Exception` 会把池满异常吞掉、再新建一条不受限的裸连接——
    等于在 db_engine 的兜底之上**又叠了一层**，两层叠加后并发一高就会撞
    MySQL 的 max_connections。只修 db_engine.py 是无效的。
    """
    if database == MYSQL_DB:
        try:
            import db_engine
        except Exception as e:
            db_engine = None
            print(f"[mysql_db] 连接池模块不可用，降级裸连接: {e}")
        else:
            try:
                return db_engine.pooled_connection()
            except (db_engine.DBPoolBusy, db_engine.DBUnavailable):
                # 池满 / 已熔断：**快速失败**，绝不在此再叠一层裸连接
                raise
            except Exception as e:
                print(f"[mysql_db] 连接池获取失败，降级裸连接: {e}")

    # 走到这里说明：指定了别的库，或池模块不可用 → 用裸连接。
    # 同样要受熔断保护，否则 MySQL 掉线时每个请求都白等 connect_timeout（实测约 8s）。
    if db_engine is not None and db_engine.breaker_open():
        raise db_engine.DBUnavailable("数据库近期连续不可用，已熔断快速失败")
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=3,     # 降级路径，不该挂 8~10 秒
        )
        if db_engine is not None:
            db_engine.breaker_record(True)
        return conn
    except Exception:
        if db_engine is not None:
            db_engine.breaker_record(False)
        raise


def create_database():
    """创建 Retail_assistant 数据库。"""
    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DB}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.close()


def init_schema():
    """创建项目当前可落库的 MySQL 表。"""
    create_database()
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS emotion_record (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            camera_id VARCHAR(64) NOT NULL,
            capture_time DATETIME NOT NULL,
            emotion VARCHAR(32) NOT NULL,
            conf DECIMAL(6,4) NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_emotion_time_camera (capture_time, camera_id),
            INDEX idx_emotion_camera_id (camera_id, id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS retail_stats (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            zone_id VARCHAR(64) NOT NULL,
            zone_type VARCHAR(32) NOT NULL,
            zone_label VARCHAR(128) NOT NULL,
            period_key VARCHAR(64) NOT NULL DEFAULT '',
            period_start DATETIME NOT NULL,
            period_end DATETIME NOT NULL,
            visit_count INT UNSIGNED NOT NULL DEFAULT 0,
            total_dwell_seconds DECIMAL(12,3) NOT NULL DEFAULT 0,
            heat_score DECIMAL(10,3) NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_retail_period_zone (period_key, zone_id),
            INDEX idx_retail_zone_time (zone_id, period_start, period_end)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_record (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            alert_type VARCHAR(64) NOT NULL,
            zone_id VARCHAR(64) NOT NULL DEFAULT '',
            person_id INT UNSIGNED NOT NULL DEFAULT 0,
            level VARCHAR(16) NOT NULL DEFAULT 'watch',
            score INT UNSIGNED NOT NULL DEFAULT 0,
            reason TEXT,
            frame_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_alert_time_level (created_at, level),
            INDEX idx_alert_zone_time (zone_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS query_history (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            session_id VARCHAR(128) NOT NULL,
            conversation_id BIGINT UNSIGNED NULL,
            seq_no BIGINT UNSIGNED NOT NULL DEFAULT 0,
            question TEXT NOT NULL,
            answer TEXT,
            intent VARCHAR(64) NOT NULL DEFAULT 'general',
            confidence DECIMAL(6,4) NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_query_session_time (session_id, created_at),
            INDEX idx_query_created_at (created_at),
            INDEX idx_query_conversation_seq (conversation_id, seq_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS voice_command_log (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            session_id VARCHAR(128) NOT NULL,
            wake_word VARCHAR(64) NOT NULL DEFAULT '',
            raw_text TEXT,
            command VARCHAR(64) NOT NULL DEFAULT '',
            action VARCHAR(32) NOT NULL DEFAULT '',
            source VARCHAR(32) NOT NULL DEFAULT 'voice',
            status VARCHAR(16) NOT NULL DEFAULT 'ok',
            latency_ms INT UNSIGNED NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_voice_session_time (session_id, created_at),
            INDEX idx_voice_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS tts_cache (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            text_hash CHAR(40) NOT NULL,
            text TEXT NOT NULL,
            audio MEDIUMBLOB NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_tts_text_hash (text_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS roi_config (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            zone_id VARCHAR(64) NOT NULL,
            zone_type VARCHAR(32) NOT NULL,
            zone_label VARCHAR(128) NOT NULL,
            polygon JSON NOT NULL,
            source VARCHAR(32) NOT NULL DEFAULT 'server',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_roi_source_zone (source, zone_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS video_record (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            filename VARCHAR(255) NOT NULL,
            file_path VARCHAR(500) NOT NULL,
            file_size BIGINT UNSIGNED NOT NULL DEFAULT 0,
            source VARCHAR(32) NOT NULL DEFAULT 'upload',
            status VARCHAR(32) NOT NULL DEFAULT 'uploaded',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_video_created_at (created_at),
            INDEX idx_video_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_session (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            session_id VARCHAR(128) NOT NULL,
            title VARCHAR(255) NOT NULL DEFAULT '新会话',
            owner VARCHAR(64) NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_chat_session_id (session_id),
            KEY idx_chat_session_owner (owner)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS heat_report (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            report_time DATETIME NOT NULL,
            summary TEXT NOT NULL,
            data JSON,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_heat_report_time (report_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS product_sales (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            zone_id VARCHAR(64) NOT NULL,
            period_key VARCHAR(64) NOT NULL DEFAULT '',
            sold_count INT UNSIGNED NOT NULL DEFAULT 0,
            sales_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
            source VARCHAR(16) NOT NULL DEFAULT 'pos',
            period_start DATETIME NOT NULL,
            period_end DATETIME NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_sales_period_zone (period_key, zone_id),
            INDEX idx_sales_zone_time (zone_id, period_start, period_end),
            INDEX idx_sales_source (source)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS track_visit_paths (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            session_id VARCHAR(128) NOT NULL DEFAULT '',
            track_id INT UNSIGNED NOT NULL,
            source VARCHAR(16) NOT NULL DEFAULT 'video',   -- video=真实采集 / simulated=测试数据
            path JSON NOT NULL,                              -- [{zone_id, zone_label, dwell_seconds}, ...] 按访问顺序
            zone_count INT UNSIGNED NOT NULL DEFAULT 0,
            total_dwell_seconds DECIMAL(10,2) NOT NULL DEFAULT 0,
            recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_paths_time (recorded_at),
            INDEX idx_paths_source (source)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_tool_log (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            session_id VARCHAR(128) NOT NULL,
            tool_name VARCHAR(128) NOT NULL,
            arguments JSON NULL,
            result TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_tool_session_time (session_id, created_at),
            INDEX idx_tool_name (tool_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # product_sales.source：显式记录数据来源（pos=真实接入 / simulated=演示 / test=验证测试）。
        # 此前只能靠 period_key 是否以 demo 开头来「猜」来源——真实格式的测试数据会被误当真实数据，
        # 因此改为显式字段（对齐 track_visit_paths.source 的既有约定）。
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='product_sales' AND column_name='source'",
            (MYSQL_DB,),
        )
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "ALTER TABLE product_sales "
                "ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'pos' AFTER sales_amount, "
                "ADD INDEX idx_sales_source (source)"
            )
            # 回填历史数据：period_key 以 demo 开头的一律标为演示数据
            cur.execute(
                "UPDATE product_sales SET source='simulated' WHERE period_key LIKE 'demo%%'"
            )

        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='retail_stats' AND column_name='period_key'",
            (MYSQL_DB,),
        )
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "ALTER TABLE retail_stats "
                "ADD COLUMN period_key VARCHAR(64) NOT NULL DEFAULT '' AFTER zone_label, "
                "ADD UNIQUE KEY uq_retail_period_zone (period_key, zone_id)"
            )

        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='retail_stats' AND column_name='deep_interest_count'",
            (MYSQL_DB,),
        )
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "ALTER TABLE retail_stats "
                "ADD COLUMN deep_interest_count INT UNSIGNED NOT NULL DEFAULT 0 "
                "AFTER visit_count"
            )

        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='query_history' AND column_name='conversation_id'",
            (MYSQL_DB,),
        )
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "ALTER TABLE query_history "
                "ADD COLUMN conversation_id BIGINT UNSIGNED NULL AFTER session_id, "
                "ADD COLUMN seq_no BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER conversation_id, "
                "ADD INDEX idx_query_conversation_seq (conversation_id, seq_no)"
            )

        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='query_history' AND column_name='seq_no'",
            (MYSQL_DB,),
        )
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "ALTER TABLE query_history "
                "ADD COLUMN seq_no BIGINT UNSIGNED NOT NULL DEFAULT 0 AFTER conversation_id, "
                "ADD INDEX idx_query_conversation_seq (conversation_id, seq_no)"
            )

        # ===== B3：会话归属（owner）=====
        # 修复前 chat_session / query_history **没有归属字段**，任意登录账号都能
        # 列出、搜索、删除所有人的问答记录。这里给 chat_session 加 owner
        # （query_history 通过 JOIN 归属到会话，保持单一事实来源，不重复存）。
        #
        # 历史行保持 owner=''（**无归属**）：迁移前无法追溯是谁建的，
        # 不猜测、不乱认领；'' 只有审计视角（root / system:manage）可见，
        # 普通账号看不到 —— 这正是本次修复要达到的效果。
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='chat_session' AND column_name='owner'",
            (MYSQL_DB,),
        )
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "ALTER TABLE chat_session "
                "ADD COLUMN owner VARCHAR(64) NOT NULL DEFAULT '' AFTER title, "
                "ADD INDEX idx_chat_session_owner (owner)"
            )

        cur.execute(
            "INSERT IGNORE INTO chat_session(session_id, title) "
            "SELECT DISTINCT q.session_id, LEFT(q.question, 60) "
            "FROM query_history q "
            "WHERE NOT EXISTS (SELECT 1 FROM chat_session s WHERE s.session_id=q.session_id)"
        )
        cur.execute(
            "UPDATE query_history q "
            "JOIN chat_session s ON q.session_id=s.session_id "
            "SET q.conversation_id=s.id WHERE q.conversation_id IS NULL"
        )
        cur.execute(
            "UPDATE query_history q "
            "JOIN ("
            " SELECT id, ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY id) rn "
            " FROM query_history"
            ") x ON q.id=x.id "
            "SET q.seq_no=x.rn WHERE q.seq_no=0"
        )
    conn.close()


def migrate_sqlite_emotion_records():
    """把 SQLite 中已有的表情记录迁移到 MySQL。"""
    sqlite_path = PROJECT_ROOT / "data" / "shop_emotion.db"
    if not sqlite_path.exists():
        return 0

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    rows = sqlite_conn.execute(
        "SELECT id, camera_id, capture_time, emotion, conf "
        "FROM emotion_record ORDER BY id"
    ).fetchall()
    sqlite_conn.close()

    if not rows:
        return 0

    conn = get_connection()
    migrated = 0
    with conn.cursor() as cur:
        insert_rows = [
            (rid, camera_id, capture_time, emotion, conf)
            for rid, camera_id, capture_time, emotion, conf in rows
        ]
        if insert_rows:
            cur.executemany(
                "INSERT IGNORE INTO emotion_record "
                "(id, camera_id, capture_time, emotion, conf) "
                "VALUES (%s, %s, %s, %s, %s)",
                insert_rows,
            )
            migrated = cur.rowcount
    conn.close()
    return migrated


def import_roi_configs():
    """把当前 ROI YAML 配置同步到 MySQL。"""
    import json
    import yaml

    sources = [
        ("server", PROJECT_ROOT / ROI_CONFIG_PATH),
        ("local", PROJECT_ROOT / LOCAL_ROI_CONFIG_PATH),
    ]
    conn = get_connection()
    total = 0
    with conn.cursor() as cur:
        for source, path in sources:
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            for zone_id, info in (config or {}).get("zones", {}).items():
                cur.execute(
                    """
                    INSERT INTO roi_config
                    (zone_id, zone_type, zone_label, polygon, source)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        zone_type = VALUES(zone_type),
                        zone_label = VALUES(zone_label),
                        polygon = VALUES(polygon),
                        source = VALUES(source)
                    """,
                    (
                        zone_id,
                        info.get("type", "shelf"),
                        info.get("label", zone_id),
                        json.dumps(info.get("polygon", []), ensure_ascii=False),
                        source,
                    ),
                )
                total += cur.rowcount
    conn.close()
    return total


def insert_emotion_record(camera_id: str, emotion: str, conf: float):
    """写入一条表情识别记录。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO emotion_record(camera_id, capture_time, emotion, conf) "
                "VALUES (%s, %s, %s, %s)",
                (camera_id, now, emotion, conf),
            )
    finally:
        conn.close()


def insert_emotion_records(camera_id: str, records: list[tuple[str, float]]):
    """批量写入表情识别记录。"""
    if not records:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(camera_id, now, emotion, conf) for emotion, conf in records]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO emotion_record(camera_id, capture_time, emotion, conf) "
                "VALUES (%s, %s, %s, %s)",
                rows,
            )
    finally:
        conn.close()


def get_emotion_statistic(start_time: str, end_time: str, camera_id: str | None = None):
    """按时间段统计表情数量。"""
    conn = get_connection()
    with conn.cursor() as cur:
        if camera_id:
            cur.execute(
                """
                SELECT emotion, COUNT(*) FROM emotion_record
                WHERE capture_time BETWEEN %s AND %s AND camera_id=%s
                GROUP BY emotion
                """,
                (start_time, end_time, camera_id),
            )
        else:
            cur.execute(
                """
                SELECT emotion, COUNT(*) FROM emotion_record
                WHERE capture_time BETWEEN %s AND %s
                GROUP BY emotion
                """,
                (start_time, end_time),
            )
        result = cur.fetchall()
    conn.close()
    return result


def get_latest_emotion_records(camera_id: str | None = None, limit: int = 20):
    """查询最近表情记录，返回与 SQLite 相同的元组结构。"""
    conn = get_connection()
    with conn.cursor() as cur:
        if camera_id:
            cur.execute(
                """
                SELECT camera_id, capture_time, emotion, conf
                FROM emotion_record
                WHERE camera_id=%s
                ORDER BY id DESC LIMIT %s
                """,
                (camera_id, int(limit)),
            )
        else:
            cur.execute(
                """
                SELECT camera_id, capture_time, emotion, conf
                FROM emotion_record
                ORDER BY id DESC LIMIT %s
                """,
                (int(limit),),
            )
        result = cur.fetchall()
    conn.close()
    return result


def get_emotion_record_count(camera_id: str | None = None, hours: int = 24):
    """统计最近 N 小时表情记录数。"""
    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    with conn.cursor() as cur:
        if camera_id:
            cur.execute(
                """
                SELECT COUNT(*) FROM emotion_record
                WHERE capture_time BETWEEN %s AND %s AND camera_id=%s
                """,
                (start_str, end_str, camera_id),
            )
        else:
            cur.execute(
                """
                SELECT COUNT(*) FROM emotion_record
                WHERE capture_time BETWEEN %s AND %s
                """,
                (start_str, end_str),
            )
        count = int(cur.fetchone()[0])
    conn.close()
    return count


def cleanup_emotion_records(days: int = 30):
    """删除超过 N 天的表情记录。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM emotion_record WHERE capture_time < %s",
            (cutoff,),
        )
        deleted = cur.rowcount
    conn.close()
    return deleted


def save_query_history(
    session_id: str,
    question: str,
    answer: str | None,
    intent: str = "general",
    confidence: float | None = None,
    owner: str | None = None,
):
    """保存自然语言查询历史。

    `owner`（B3）：本次问答的归属账号。
    - 会话不存在 → 用它建会话；
    - 会话已存在 → **不改动原有 owner**（第二个账号拿着别人的 session_id 写入时，
      不会把会话"认领"走；写路径的越权判定在 `api/routes/query.py`）。

    ⚠ 同会话的 seq_no 分配必须**串行**：本函数是「SELECT MAX(seq_no)+1」再「INSERT」
    两条独立语句，而池化连接是 autocommit=True，中间没有任何保护。实测（8 线程
    用 barrier 同时写同一 session_id）：

        旧写法 -> 库中 seq_no = [1, 1, 1, 1, 1, 1, 1, 1]   （全部撞成 1）

    后果不止"顺序乱"：`api/routes/query.py` 用 `seq_no == 1` 判断"会话首条消息"
    来决定是否用 LLM 生成标题——撞号会让它**重复触发**（多余的 LLM 调用 + 标题被覆盖）。

    为什么在进程内加锁、而不是改成 SQL 层原子操作（两条路都实测过）：
      - 单条 `INSERT ... SELECT COALESCE(MAX(seq_no),0)+1`：并发下大量
        MySQL **死锁 1213**（实测 8 线程有 5~6 个失败），加重试也仍有失败
      - `SELECT ... FOR UPDATE` + 显式事务：需要对**池化连接**来回切换 autocommit，
        一旦异常路径没恢复，就会污染后续复用该连接的业务

    本项目是单进程部署（compose 单副本），进程内锁即可彻底解决；
    将来多进程化时需改为 DB 层方案（见 改进记录.md 待办）。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            with _seq_no_lock:
                cur.execute(
                    """
                    INSERT INTO chat_session(session_id, title, owner)
                    VALUES (%s, LEFT(%s, 60), %s)
                    ON DUPLICATE KEY UPDATE
                        title=CASE WHEN title='新会话' THEN VALUES(title) ELSE title END,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (session_id, question.strip(), owner or ""),
                )
                cur.execute(
                    "SELECT id FROM chat_session WHERE session_id=%s",
                    (session_id,),
                )
                conversation_row = cur.fetchone()
                conversation_id = conversation_row[0] if conversation_row else None
                cur.execute(
                    "SELECT COALESCE(MAX(seq_no), 0) + 1 FROM query_history WHERE session_id=%s",
                    (session_id,),
                )
                seq_no = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO query_history
                    (session_id, conversation_id, seq_no, question, answer, intent, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (session_id, conversation_id, seq_no, question, answer, intent, confidence),
            )
        message_id = cur.lastrowid
    finally:
        conn.close()
    return message_id, seq_no


def create_chat_session(session_id: str, title: str = "新会话", owner: str | None = None):
    """新建会话（`owner` 为归属账号，见 B3）。"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO chat_session(session_id, title, owner) VALUES (%s, %s, %s)",
            (session_id, title[:255], owner or ""),
        )
    conn.close()


def save_chat_session(session_id: str, title: str | None = None, owner: str | None = None):
    """保存会话标题并刷新更新时间。

    `owner=None` 表示**不限定归属**（审计视角）；否则只允许改动该归属的会话，
    影响 0 行即说明这个会话不属于调用方（路由据此返回 404）。
    """
    conn = get_connection()
    with conn.cursor() as cur:
        if title:
            cur.execute(
                "UPDATE chat_session SET title=%s, updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=%s AND (%s IS NULL OR owner=%s)",
                (title[:255], session_id, owner, owner),
            )
        else:
            cur.execute(
                "UPDATE chat_session SET updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=%s AND (%s IS NULL OR owner=%s)",
                (session_id, owner, owner),
            )
        affected = cur.rowcount
    conn.close()
    return affected


def get_chat_session_owner(session_id: str) -> str | None:
    """取会话归属；返回 None 表示**会话不存在**。

    写路径用它判定越权（`api/routes/query.py`）：会话属于他人时拒绝写入，
    避免把内容注入别人的问答记录。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT owner FROM chat_session WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return None if row is None else (row[0] or "")


def list_chat_sessions(owner: str | None = None):
    """列出会话及消息数。

    `owner=None` = 审计视角（全部账号）；否则只列该账号的会话（B3）。
    """
    conn = get_connection()
    sessions = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at, s.owner,
                   (SELECT COUNT(*) FROM query_history q WHERE q.session_id=s.session_id) AS message_count
            FROM chat_session s
            WHERE (%s IS NULL OR s.owner=%s)
            ORDER BY s.updated_at DESC, s.id DESC
            """,
            (owner, owner),
        )
        for row in cur.fetchall():
            sessions.append({
                "session_id": row[0],
                "title": row[1],
                "created_at": row[2].strftime("%Y-%m-%d %H:%M:%S") if row[2] else "",
                "updated_at": row[3].strftime("%Y-%m-%d %H:%M:%S") if row[3] else "",
                "owner": row[4] or "",
                "message_count": row[5],
            })
    conn.close()
    return sessions


def get_chat_messages(session_id: str, owner: str | None = None):
    """获取指定会话的消息列表（`owner` 限定归属；None = 审计视角）。

    归属通过 JOIN `chat_session` 过滤（单一事实来源），不在这里重复存 owner。
    """
    conn = get_connection()
    messages = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.seq_no, q.question, q.answer, q.intent, q.confidence, q.created_at
            FROM query_history q
            JOIN chat_session s ON q.session_id = s.session_id
            WHERE q.session_id=%s AND (%s IS NULL OR s.owner=%s)
            ORDER BY q.seq_no, q.id
            """,
            (session_id, owner, owner),
        )
        for row in cur.fetchall():
            messages.append({
                "seq_no": row[0],
                "question": row[1],
                "answer": row[2],
                "intent": row[3],
                "confidence": float(row[4]) if row[4] is not None else None,
                "created_at": row[5].strftime("%Y-%m-%d %H:%M:%S") if row[5] else "",
            })
    conn.close()
    return messages


def get_chat_message_ids(session_id: str, owner: str | None = None):
    """获取指定会话下的消息 ID（`owner` 限定归属）。"""
    conn = get_connection()
    ids = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.id
            FROM query_history q
            JOIN chat_session s ON q.session_id = s.session_id
            WHERE q.session_id=%s AND (%s IS NULL OR s.owner=%s)
            ORDER BY q.id
            """,
            (session_id, owner, owner),
        )
        ids = [row[0] for row in cur.fetchall()]
    conn.close()
    return ids


def delete_chat_session(session_id: str, owner: str | None = None):
    """删除会话及其全部消息，返回被删除的消息 ID。

    `owner` 限定归属（None = 审计视角）。**返回 None 表示会话不存在或不属于调用方**
    —— 路由据此返回 404，避免"能删别人的会话"或"用返回码泄露会话是否存在"。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 不用 FOR UPDATE：池化连接是 autocommit，锁在语句结束即释放，
            # 加了只会给人"已加锁"的错觉；并发删除本身是幂等的。
            cur.execute(
                "SELECT owner FROM chat_session WHERE session_id=%s",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if owner is not None and (row[0] or "") != owner:
                return None
            cur.execute(
                "SELECT id FROM query_history WHERE session_id=%s ORDER BY id",
                (session_id,),
            )
            message_ids = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM query_history WHERE session_id=%s", (session_id,))
            cur.execute("DELETE FROM chat_session WHERE session_id=%s", (session_id,))
    finally:
        conn.close()
    return message_ids


def get_all_query_history_records(owner: str | None = None):
    """获取查询历史，用于向量库重建。

    `owner=None`（默认）= 全部账号（重建/审计场景）；否则只取该归属。
    """
    conn = get_connection()
    records = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.id, q.session_id, q.seq_no, q.question, q.answer,
                   q.created_at, s.title, s.owner
            FROM query_history q
            JOIN chat_session s ON q.session_id = s.session_id
            WHERE (%s IS NULL OR s.owner=%s)
            ORDER BY q.id
            """,
            (owner, owner),
        )
        for row in cur.fetchall():
            records.append({
                "message_id": row[0],
                "session_id": row[1],
                "seq_no": row[2],
                "question": row[3],
                "answer": row[4],
                "created_at": row[5].strftime("%Y-%m-%d %H:%M:%S") if row[5] else "",
                "title": row[6],
                "owner": row[7] or "",
            })
    conn.close()
    return records


def search_chat_messages(keyword: str, limit: int = CHAT_SEARCH_LIMIT, owner: str | None = None):
    """按关键词搜索会话标题、问题或回答。

    ⚠ 这是**最容易被忽视的泄露路径**：它既给 `GET /api/chat/search` 用，
    也被 Agent 工具 `search_chat_history` 调用（检索结果会直接进入 LLM 上下文）。
    因此必须按 `owner` 过滤（None = 审计视角，见 B3）。
    """
    if not keyword or not keyword.strip():
        return []
    pattern = f"%{keyword.strip()}%"
    conn = get_connection()
    results = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT q.session_id, s.title, q.seq_no, q.question, q.answer, q.created_at
                FROM query_history q
                JOIN chat_session s ON q.session_id=s.session_id
                WHERE (q.question LIKE %s
                   OR q.answer LIKE %s
                   OR s.title LIKE %s)
                  AND (%s IS NULL OR s.owner=%s)
                ORDER BY q.created_at DESC
                LIMIT %s
                """,
                (pattern, pattern, pattern, owner, owner, int(limit)),
            )
            for row in cur.fetchall():
                results.append({
                    "session_id": row[0],
                    "title": row[1],
                    "seq_no": row[2],
                    "question": row[3],
                    "answer": row[4],
                    "created_at": row[5].strftime("%Y-%m-%d %H:%M:%S") if row[5] else "",
                })
    finally:
        conn.close()
    return results


def update_chat_session_title(session_id: str, title: str, owner: str | None = None):
    """更新会话标题（LLM 生成的简短标题）。`owner` 限定归属（None = 审计视角）。"""
    if not title or not title.strip():
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_session SET title=%s, updated_at=CURRENT_TIMESTAMP "
                "WHERE session_id=%s AND (%s IS NULL OR owner=%s)",
                (title.strip()[:255], session_id, owner, owner),
            )
    finally:
        conn.close()


def save_retail_stats(
    period_key: str,
    zones: dict,
    period_start: str | None = None,
    period_end: str | None = None,
):
    """保存零售热度统计快照，同一分钟同一区域只保留最新值。"""
    now = datetime.now()
    period_start = period_start or now.strftime("%Y-%m-%d %H:%M:%S")
    period_end = period_end or now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for zone_id, z in (zones or {}).items():
                cur.execute(
                    """
                    INSERT INTO retail_stats
                    (period_key, zone_id, zone_type, zone_label,
                     period_start, period_end, visit_count, deep_interest_count,
                     total_dwell_seconds, heat_score)
                    VALUES (%s, %s, 'shelf', %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        zone_label=VALUES(zone_label),
                        period_start=VALUES(period_start),
                        period_end=VALUES(period_end),
                        visit_count=VALUES(visit_count),
                        deep_interest_count=VALUES(deep_interest_count),
                        total_dwell_seconds=VALUES(total_dwell_seconds),
                        heat_score=VALUES(heat_score)
                    """,
                    (
                        period_key,
                        zone_id,
                        z.get("zone_label", zone_id),
                        period_start,
                        period_end,
                        z.get("visit_count", 0),
                        z.get("deep_interest_count", 0),
                        z.get("total_dwell_seconds", 0),
                        z.get("heat_score", 0),
                    ),
                )
    finally:
        conn.close()


def save_product_sales(
    zone_id: str,
    period_key: str,
    sold_count: int,
    sales_amount: float,
    period_start: str | None = None,
    period_end: str | None = None,
    source: str = "pos",
):
    """保存区域商品销量（同一时段同一区域 UPSERT 为最新值）。

    销量来源：真实 POS 接入或演示数据录入。用于与视频热度（retail_stats）比对，
    识别"高热度低销量"（货架吸客但商品品质/匹配度问题）等业务信号。

    source：数据来源标记——`pos`=真实接入 / `simulated`=演示数据 / `test`=验证测试数据。
    显式记录来源（而非从 period_key 猜），避免演示或测试数据被当成真实经营数据。
    """
    now = datetime.now()
    period_start = period_start or now.strftime("%Y-%m-%d %H:%M:%S")
    period_end = period_end or now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO product_sales
                (zone_id, period_key, sold_count, sales_amount, source, period_start, period_end)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    sold_count=VALUES(sold_count),
                    sales_amount=VALUES(sales_amount),
                    source=VALUES(source),
                    period_start=VALUES(period_start),
                    period_end=VALUES(period_end)
                """,
                (zone_id, period_key, int(sold_count or 0),
                 float(sales_amount or 0), source or "pos", period_start, period_end),
            )
    finally:
        conn.close()


def get_product_sales(period_key: str | None = None, hours: int = 1) -> list[dict]:
    """按区域聚合最近 N 小时销量。返回 [{"zone_id", "sold_count", "sales_amount"}, ...]"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if period_key:
                cur.execute(
                    "SELECT zone_id, SUM(sold_count), SUM(sales_amount)"
                    " FROM product_sales WHERE period_key=%s GROUP BY zone_id",
                    (period_key,),
                )
            else:
                cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "SELECT zone_id, SUM(sold_count), SUM(sales_amount)"
                    " FROM product_sales WHERE period_end >= %s GROUP BY zone_id",
                    (cutoff,),
                )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"zone_id": r[0], "sold_count": int(r[1] or 0), "sales_amount": float(r[2] or 0)}
        for r in rows
    ]


def get_sales_sources(period_key: str | None = None, hours: int = 1) -> list[str]:
    """返回该时间窗内出现的销量**来源标记**（pos / simulated / test）。

    这是判定数据来源的**权威依据**（显式字段），不再依赖 period_key 前缀猜测。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if period_key:
                cur.execute(
                    "SELECT DISTINCT source FROM product_sales WHERE period_key=%s",
                    (period_key,),
                )
            else:
                cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "SELECT DISTINCT source FROM product_sales WHERE period_end >= %s",
                    (cutoff,),
                )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


def get_sales_period_keys(period_key: str | None = None, hours: int = 1) -> list[str]:
    """返回该时间窗内出现过的销量 period_key 列表。

    用于判定销量数据来源（演示 vs 真实）：period_key 以 'demo' 开头为演示数据。
    只取 DISTINCT，不改变既有聚合查询行为。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if period_key:
                cur.execute(
                    "SELECT DISTINCT period_key FROM product_sales WHERE period_key=%s",
                    (period_key,),
                )
            else:
                cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "SELECT DISTINCT period_key FROM product_sales WHERE period_end >= %s",
                    (cutoff,),
                )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


def save_track_visit_path(
    session_id: str,
    track_id: int,
    path: list[dict],
    source: str = "video",
    zone_count: int = 0,
    total_dwell_seconds: float = 0.0,
):
    """保存一条顾客动线（轨迹访问的区域序列）。

    path: [{zone_id, zone_label, dwell_seconds}, ...] 按访问顺序。
    source: video=真实视频采集 / simulated=测试模拟数据。
    """
    import json
    if not path:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO track_visit_paths"
                " (session_id, track_id, source, path, zone_count, total_dwell_seconds)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    session_id[:128], int(track_id), source,
                    json.dumps(path, ensure_ascii=False),
                    int(zone_count or len(path)),
                    float(total_dwell_seconds or 0),
                ),
            )
    finally:
        conn.close()


def get_track_visit_paths(source: str | None = None, limit: int = 2000) -> list[dict]:
    """读取轨迹动线（供关联规则分析）。"""
    import json
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if source:
                cur.execute(
                    "SELECT id, session_id, track_id, source, path, zone_count,"
                    " total_dwell_seconds, recorded_at FROM track_visit_paths"
                    " WHERE source=%s ORDER BY id DESC LIMIT %s",
                    (source, int(limit)),
                )
            else:
                cur.execute(
                    "SELECT id, session_id, track_id, source, path, zone_count,"
                    " total_dwell_seconds, recorded_at FROM track_visit_paths"
                    " ORDER BY id DESC LIMIT %s",
                    (int(limit),),
                )
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            p = json.loads(r[4]) if isinstance(r[4], str) else (r[4] or [])
        except Exception:
            p = []
        out.append({
            "id": r[0], "session_id": r[1], "track_id": r[2], "source": r[3],
            "path": p, "zone_count": r[5],
            "total_dwell_seconds": float(r[6] or 0),
            "recorded_at": r[7].strftime("%Y-%m-%d %H:%M:%S") if r[7] else "",
        })
    return out


def get_retail_stats_by_zone(period_key: str | None = None, hours: int = 1,
                             until_key: str | None = None) -> list[dict]:
    """按区域聚合最近 N 小时视频热度（与销量比对用）。

    ⚠ period_key 有两种粒度，本函数**两种都要支持**：
      - 视频管线写的是 **12 位分钟**（`YYYYMMDDHHMM`，每 75 帧一条累计快照，
        为的是每分钟能 UPSERT 一行）
      - 销量侧与同期对比用的是 **10 位小时**（`YYYYMMDDHH`）

    原先这里是精确匹配 `WHERE period_key=%s`，于是拿小时 key 查热度**永远 0 行**：
    "今天客流比昨天怎么样"从来拿不到客流数据。实测（同一时刻）：
        销量 sold_count = 5，而客流 visit_count = 0
    长度不足 12 位时按**前缀**匹配整点，配合 MAX(visit_count) 即得到该小时末的累计值。

    `until_key`：可选的**上界**（同样按 period_key 字符串比较，因为它是 YYYYMMDDHHMM
    这种可直接字典序比较的格式）。用于"本小时只过了 N 分钟"时与历史**相同已过分钟数**
    对齐——否则会拿"5 分钟的数据"去比"昨天整小时"，得出 -93% 这种假暴跌。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if period_key:
                if len(period_key) >= 12:
                    cur.execute(
                        "SELECT zone_id, MAX(zone_label), MAX(visit_count), MAX(total_dwell_seconds), MAX(heat_score)"
                        " FROM retail_stats WHERE period_key=%s GROUP BY zone_id",
                        (period_key,),
                    )
                elif until_key:
                    # 小时粒度 + 截断上界：只取"到同一分钟为止"的快照
                    cur.execute(
                        "SELECT zone_id, MAX(zone_label), MAX(visit_count), MAX(total_dwell_seconds), MAX(heat_score)"
                        " FROM retail_stats WHERE period_key LIKE %s AND period_key <= %s"
                        " GROUP BY zone_id",
                        (period_key + "%", until_key),
                    )
                else:
                    # 小时（或更短）粒度：前缀匹配该整点的所有分钟快照
                    cur.execute(
                        "SELECT zone_id, MAX(zone_label), MAX(visit_count), MAX(total_dwell_seconds), MAX(heat_score)"
                        " FROM retail_stats WHERE period_key LIKE %s GROUP BY zone_id",
                        (period_key + "%",),
                    )
            else:
                cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "SELECT zone_id, MAX(zone_label), MAX(visit_count), MAX(total_dwell_seconds), MAX(heat_score)"
                    " FROM retail_stats WHERE period_end >= %s GROUP BY zone_id",
                    (cutoff,),
                )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "zone_id": r[0],
            "zone_label": r[1] or r[0],
            "visit_count": int(r[2] or 0),
            "total_dwell_seconds": float(r[3] or 0),
            "heat_score": float(r[4] or 0),
        }
        for r in rows
    ]


def save_heat_report(summary: str, data: dict | None = None):
    """保存定期热度汇报。"""
    import json
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO heat_report(report_time, summary, data)
                VALUES (%s, %s, %s)
                """,
                (now, summary, json.dumps(data or {}, ensure_ascii=False)),
            )
    finally:
        conn.close()


def save_agent_tool_log(
    session_id: str,
    tool_name: str,
    arguments: dict | None = None,
    result: str | None = None,
):
    """记录一次 Agent 工具调用（审计/可观测）。

    参数过长时自动截断，避免拖垮写入性能。
    """
    import json
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_tool_log(session_id, tool_name, arguments, result)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    session_id,
                    tool_name[:128],
                    json.dumps(arguments or {}, ensure_ascii=False)[:2000],
                    (result or "")[:4000],
                ),
            )
    except Exception:
        # 工具日志失败不影响主流程
        print(f"[AgentToolLog] 写入失败 session={session_id} tool={tool_name}")
    finally:
        conn.close()


def get_latest_heat_reports(limit: int = 20):
    """获取最近的热度汇报。"""
    conn = get_connection()
    reports = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT report_time, summary, data, created_at
            FROM heat_report
            ORDER BY id DESC
            LIMIT %s
            """,
            (int(limit),),
        )
        for row in cur.fetchall():
            reports.append({
                "report_time": row[0].strftime("%Y-%m-%d %H:%M:%S") if row[0] else "",
                "summary": row[1],
                "data": row[2],
                "created_at": row[3].strftime("%Y-%m-%d %H:%M:%S") if row[3] else "",
            })
    conn.close()
    return reports


def save_alert_record(
    alert_type: str,
    zone_id: str,
    person_id: int,
    level: str,
    score: int,
    reason: str,
    frame_id: int,
    created_at: str | None = None,
):
    """保存异常告警记录。"""
    created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alert_record
                (alert_type, zone_id, person_id, level, score, reason, frame_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    alert_type,
                    zone_id,
                    person_id,
                    level,
                    score,
                    reason,
                    frame_id,
                    created_at,
                ),
            )
    finally:
        conn.close()


def save_voice_command_log(
    session_id: str,
    raw_text: str,
    command: str,
    action: str,
    status: str = "ok",
    wake_word: str = "",
    source: str = "voice",
    latency_ms: int = 0,
):
    """保存语音指令日志。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO voice_command_log
                (session_id, wake_word, raw_text, command, action, source, status, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (session_id, wake_word, raw_text, command, action, source, status, latency_ms),
            )
    finally:
        conn.close()


def save_tts_cache(cache_key: str, text: str, audio: bytes):
    """保存 TTS 音频缓存。"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tts_cache(text_hash, text, audio)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE text=VALUES(text), audio=VALUES(audio)
            """,
            (cache_key, text, audio),
        )
    conn.close()


def get_tts_cache(cache_key: str) -> bytes | None:
    """从 MySQL 读取 TTS 音频缓存。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT audio FROM tts_cache WHERE text_hash=%s",
                (cache_key,),
            )
            row = cur.fetchone()
            return bytes(row[0]) if row else None
    finally:
        conn.close()


def save_video_record(
    filename: str,
    file_path: str,
    file_size: int,
    source: str = "upload",
    status: str = "uploaded",
):
    """保存视频上传记录。"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO video_record(filename, file_path, file_size, source, status)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (filename, file_path, file_size, source, status),
        )
    conn.close()


def upsert_roi_config(
    zone_id: str,
    zone_type: str,
    zone_label: str,
    polygon: list,
    source: str,
):
    """写入或更新 ROI 配置。"""
    import json

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO roi_config(zone_id, zone_type, zone_label, polygon, source)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                zone_type=VALUES(zone_type),
                zone_label=VALUES(zone_label),
                polygon=VALUES(polygon),
                source=VALUES(source)
            """,
            (zone_id, zone_type, zone_label, json.dumps(polygon, ensure_ascii=False), source),
        )
    conn.close()


def delete_roi_config(zone_id: str, source: str = "server"):
    """删除 ROI 配置。"""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM roi_config WHERE zone_id=%s AND source=%s",
            (zone_id, source),
        )
    conn.close()


def list_tables():
    """列出 Retail_assistant 数据库中的业务表。"""
    conn = get_connection()
    tables = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s ORDER BY table_name",
            (MYSQL_DB,),
        )
        tables = [row[0] for row in cur.fetchall()]
    conn.close()
    return tables


def table_schema(table_name: str) -> list[dict]:
    """返回表字段信息。"""
    conn = get_connection()
    fields = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, column_type, is_nullable, column_default, extra "
            "FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s "
            "ORDER BY ordinal_position",
            (MYSQL_DB, table_name),
        )
        for row in cur.fetchall():
            fields.append({
                "column": row[0],
                "type": row[1],
                "nullable": row[2],
                "default": row[3],
                "extra": row[4],
            })
    conn.close()
    return fields


def row_counts() -> dict[str, int]:
    """返回各表当前记录数。"""
    conn = get_connection()
    counts = {}
    with conn.cursor() as cur:
        for table in list_tables():
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            counts[table] = int(cur.fetchone()[0])
    conn.close()
    return counts


if __name__ == "__main__":
    init_schema()
    emotion_migrated = migrate_sqlite_emotion_records()
    roi_migrated = import_roi_configs()
    print("tables:", list_tables())
    print("counts:", row_counts())
    print("migrated_emotion:", emotion_migrated)
    print("migrated_roi:", roi_migrated)
