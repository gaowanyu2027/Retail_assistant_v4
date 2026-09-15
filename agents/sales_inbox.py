"""
销量自动同步（MVP）— 定时目录扫描

形态（对齐真实零售做法）：
    POS/ERP 定时把导出的 CSV 投递到约定目录（共享目录 / SFTP / 定时任务），
    本模块后台定时扫描并自动导入，取代「人手调接口导入」。

流程：
    扫描 inbox/*.csv
      → 解析（复用 sales_ingest.parse_sales_csv）
      → 导入（复用 sales_ingest.import_sales：整点口径 + 来源标记 pos）
      → 成功：归档到 processed/
      → 失败：移到 failed/（文件保留，便于排查）

约定：
    - 文件名含 10 位数字（YYYYMMDDHH）→ 作为该批数据的 period_key
      例：sales_2026091314.csv → period_key=2026091314
      否则使用当前整点
    - CSV 列：zone_id,sold_count,sales_amount（表头可带可省）
    - 幂等：文件处理后即被移走，同一文件不会被重复导入

目录结构：
    data/sales_inbox/{inbox,processed,failed}
"""
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from config.settings import (
    SALES_INBOX_DIR,
    SALES_INBOX_ENABLED,
    SALES_INBOX_INTERVAL_SECONDS,
)

# 文件名中的整点时段（YYYYMMDDHH）
_PERIOD_RE = re.compile(r"(20\d{8})")

_worker_started = False
_worker_lock = threading.Lock()


# ==================== 目录 ====================

def inbox_dir() -> Path:
    d = Path(SALES_INBOX_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def processed_dir() -> Path:
    d = inbox_dir() / "processed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def failed_dir() -> Path:
    d = inbox_dir() / "failed"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ==================== 工具 ====================

def extract_period_key(filename: str) -> str | None:
    """从文件名提取整点时段（YYYYMMDDHH）；无则返回 None。"""
    m = _PERIOD_RE.search(filename or "")
    return m.group(1) if m else None


def _unique_dest(target_dir: Path, filename: str) -> Path:
    """目标目录中的唯一落点（同名时加时间戳，避免覆盖历史归档）。"""
    dest = target_dir / filename
    if not dest.exists():
        return dest
    ts = datetime.now().strftime("%H%M%S")
    return target_dir / f"{Path(filename).stem}_{ts}{Path(filename).suffix}"


def _move(path: Path, target_dir: Path) -> Path:
    dest = _unique_dest(target_dir, path.name)
    try:
        shutil.move(str(path), str(dest))
    except Exception:
        # 跨卷等场景退化为「复制 + 删除」
        shutil.copy2(str(path), str(dest))
        path.unlink(missing_ok=True)
    return dest


# ==================== 扫描 ====================

def scan_once() -> dict:
    """扫描一次 inbox 并导入全部 CSV。

    Returns:
        {"scanned": n, "imported": n, "files": [...], "errors": [{file, error, moved_to}]}
    """
    from agents.sales_ingest import import_sales, parse_sales_csv, period_key_now

    src = inbox_dir()
    result: dict = {"scanned": 0, "imported": 0, "files": [], "errors": []}

    # 只取文件（跳过 processed/ failed/ 子目录），按名称排序保证可预期
    files = sorted(p for p in src.glob("*.csv") if p.is_file())

    for path in files:
        result["scanned"] += 1
        try:
            # utf-8-sig：兼容 Windows/POS 导出常见的 BOM
            text = path.read_text(encoding="utf-8-sig")
            records = parse_sales_csv(text)
            pk = extract_period_key(path.name) or period_key_now()
            res = import_sales(records, period_key=pk)
            dest = _move(path, processed_dir())
            result["imported"] += res["imported"]
            result["files"].append({
                "file": path.name,
                "period_key": pk,
                "imported": res["imported"],
                "archived_to": str(dest),
            })
        except Exception as e:
            try:
                dest = _move(path, failed_dir())
                moved = str(dest)
            except Exception as me:
                moved = f"移动失败: {me}"
            result["errors"].append({
                "file": path.name,
                "error": f"{type(e).__name__}: {e}",
                "moved_to": moved,
            })

    return result


def _run_loop():
    interval = max(5, SALES_INBOX_INTERVAL_SECONDS)
    print(f"[SalesInbox] 已启动：每 {interval}s 扫描 {inbox_dir()}")
    while True:
        try:
            r = scan_once()
            if r["scanned"]:
                print(f"[SalesInbox] 本轮 {r['scanned']} 个文件："
                      f"导入 {r['imported']} 条，失败 {len(r['errors'])} 个")
                for f in r["files"]:
                    print(f"[SalesInbox]   [OK] {f['file']} → period_key={f['period_key']} "
                          f"导入 {f['imported']} 条")
                for e in r["errors"]:
                    print(f"[SalesInbox]   [FAIL] {e['file']}: {e['error']}")
        except Exception as e:
            print(f"[SalesInbox] 扫描异常: {e}")
        time.sleep(interval)


def start_inbox_worker() -> threading.Thread | None:
    """启动后台扫描线程（幂等：重复调用只启动一次）。"""
    global _worker_started
    if not SALES_INBOX_ENABLED:
        print("[SalesInbox] 未启用（SALES_INBOX_ENABLED=0）")
        return None
    with _worker_lock:
        if _worker_started:
            return None
        _worker_started = True
        thread = threading.Thread(target=_run_loop, daemon=True)
        thread.start()
        return thread
