"""视频派生表入库等价性对账（S4）：Python 写者 vs Go 写者。

为什么需要它：S4 要把三张视频派生表（`retail_stats` / `alert_record` /
`track_visit_paths`）的写入从 Python 换到 Go。"页面看起来正常"不能作为
换写者的验收证据——必须把两次运行的落库结果逐行、逐字段摆在一起比较。

用法（同一段视频跑两遍）：

    # 1) 保持 Python 写者（GO_OWNS_INGEST_WRITES=0），播一遍视频，然后快照时间窗
    python benchmark/ingest_parity.py snapshot --label python \\
        --since "2026-10-17 16:00:00" --until "2026-10-17 16:05:00"

    # 2) 切到 Go 写者（GO_OWNS_INGEST_WRITES=1）重启，用同一段视频再跑一遍
    python benchmark/ingest_parity.py snapshot --label go \\
        --since "2026-10-17 16:10:00" --until "2026-10-17 16:15:00"

    # 3) 对账；差异非 0 时退出码为 1
    python benchmark/ingest_parity.py compare \\
        benchmark/reports/ingest_parity_python_20261017_160500.json \\
        benchmark/reports/ingest_parity_go_20261017_161500.json

口径（为什么这样比）：

- 快照按**时间窗**读取（`created_at` / `recorded_at`，左闭右开），只取
  该窗口内的行；两次运行应使用**互不重叠**的窗口，避免把同一批行读进来。
- 剔除**运行相关**字段后再比：`id`、`session_id`、`period_key`、
  `period_start/end`、`created_at/recorded_at`（以及 `retail_stats` 的
  分钟键）。它们天然因运行时间不同而不同，与"写者是否等价"无关。
- `retail_stats` 是 `ON DUPLICATE KEY UPDATE`，同一 zone 在一轮运行里可能
  横跨多个分钟桶。快照时按 `zone_id` 折叠为**最新一条**（`period_start`
  最大），比较的是"一轮运行结束时的最终状态"；原始行数另记在
  `raw_row_count` 供参考。
- `track_visit_paths` 按 `track_id` 配对；`alert_record` 没有自然键，按
  `(alert_type, zone_id, person_id, level, reason)` 分组后按内容配对。
- 数值字段允许容差（默认 0.2）：两次运行的 dwell / heat 受 CPU 调度影响
  会有小抖动，字符串字段仍严格相等。抖动更大的字段可用
  `--ignore table.field` 排除（例如 `alert_record.frame_id`）。

已知限制：这是"两次真实运行"的对账，不是"同一份 meta 喂两个写者"的
回放。管线本身的时间抖动会体现在 dwell/heat 上，因此容差与忽略项要按
实测结果显式给出，报告里也原样记录所用参数。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPORT_DIR = Path(__file__).resolve().parent / "reports"
TOOL_VERSION = 1
DEFAULT_TOLERANCE = 0.2
DEFAULT_MAX_SAMPLES = 20
DEFAULT_MAX_ROWS = 50_000

# 三张表各自的"参与比较的字段"由**剔除运行相关字段**反推（SELECT * 后
# 用 drop_fields 过滤），这样将来加列会自动进入比较，不会被静默漏掉。
#
# key_fields：自然键，同一轮运行内应唯一（组内重复时按内容排序配对）。
# match_fields：没有自然键时的分组字段（组内按内容排序配对）。
# json_fields：MySQL JSON 列在 pymysql 里是字符串，先解析再比较。
TABLE_SPECS = {
    "retail_stats": {
        "time_column": "created_at",
        "drop_fields": (
            "id", "period_key", "period_start", "period_end", "created_at",
        ),
        "key_fields": ("zone_id",),
        "match_fields": (),
        "json_fields": (),
        # 分钟桶在两次运行里切分点不同，折叠为每 zone 的最新一条
        "collapse": {"key": "zone_id", "order_by": ("period_start", "period_key", "id")},
    },
    "alert_record": {
        "time_column": "created_at",
        "drop_fields": ("id", "created_at"),
        "key_fields": (),
        "match_fields": ("alert_type", "zone_id", "person_id", "level", "reason"),
        "json_fields": (),
        "collapse": None,
    },
    "track_visit_paths": {
        "time_column": "recorded_at",
        "drop_fields": ("id", "session_id", "recorded_at"),
        "key_fields": ("track_id",),
        "match_fields": (),
        "json_fields": ("path",),
        "collapse": None,
    },
}


# ==================== 归一化（纯函数，可离线测试） ====================

def _normalize_value(value):
    """把 DB 驱动返回的类型收敛成可 JSON 序列化、可比较的值。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def normalize_row(table: str, row: dict) -> dict:
    """剔除运行相关字段，并把数值/JSON 列转成稳定形态。"""
    spec = TABLE_SPECS[table]
    out = {}
    for key, value in row.items():
        if key in spec["drop_fields"]:
            continue
        value = _normalize_value(value)
        if key in spec["json_fields"] and isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                # 解析失败就保留原字符串，比较时会被当作字符串差异报出来
                pass
        out[key] = value
    return out


def normalize_rows(table: str, rows) -> list[dict]:
    return [normalize_row(table, row) for row in rows]


def _order_value(value):
    value = _normalize_value(value)
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:020.3f}"
    return str(value)


def collapse_rows(table: str, rows) -> list[dict]:
    """按 spec.collapse 把同一键的多行折叠为"最新一条"（原始行，未归一化）。"""
    spec = TABLE_SPECS[table]
    collapse = spec.get("collapse")
    if not collapse:
        return list(rows)
    best = {}
    for row in rows:
        key = row.get(collapse["key"])
        order = tuple(_order_value(row.get(f)) for f in collapse["order_by"])
        if key not in best or order > best[key][0]:
            best[key] = (order, row)
    return [item[1] for item in best.values()]


def snapshot_table(table: str, rows) -> dict:
    """把一批原始 DB 行变成快照中的一张表。"""
    collapse = TABLE_SPECS[table].get("collapse")
    collapsed = collapse_rows(table, rows)
    normalized = normalize_rows(table, collapsed)
    normalized.sort(key=_canonical)
    return {
        "raw_row_count": len(rows),
        "row_count": len(normalized),
        "collapsed_by": [collapse["key"]] if collapse else [],
        "rows": normalized,
    }


# ==================== 比较（纯函数，可离线测试） ====================

def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _group_by(rows, fields):
    groups = {}
    for row in rows:
        key = tuple(row.get(f) for f in fields)
        groups.setdefault(key, []).append(row)
    return groups


def _sortable_key(key):
    return tuple(_canonical(part) for part in key)


def diff_values(base, cand, tolerance: float, path: str = "$") -> list[dict]:
    """递归比较两个值；数值走容差，字符串/布尔严格相等。"""
    diffs: list[dict] = []
    if isinstance(base, bool) or isinstance(cand, bool):
        if base != cand:
            diffs.append({"field": path, "baseline": base, "candidate": cand})
        return diffs
    if isinstance(base, (int, float)) and isinstance(cand, (int, float)):
        delta = abs(float(base) - float(cand))
        # 加 1e-9 吸收二进制浮点误差（如 1.6*2 - 1.5*2 = 0.20000000000000018）
        if delta > tolerance + 1e-9:
            diffs.append({
                "field": path,
                "baseline": base,
                "candidate": cand,
                "delta": round(delta, 6),
            })
        return diffs
    if isinstance(base, list) and isinstance(cand, list):
        if len(base) != len(cand):
            diffs.append({
                "field": f"{path}.length",
                "baseline": len(base),
                "candidate": len(cand),
            })
        for i, (a, b) in enumerate(zip(base, cand)):
            diffs.extend(diff_values(a, b, tolerance, f"{path}[{i}]"))
        return diffs
    if isinstance(base, dict) and isinstance(cand, dict):
        for key in sorted(set(base) | set(cand)):
            if key not in base:
                diffs.append({"field": f"{path}.{key}", "baseline": None,
                              "candidate": cand[key]})
            elif key not in cand:
                diffs.append({"field": f"{path}.{key}", "baseline": base[key],
                              "candidate": None})
            else:
                diffs.extend(diff_values(base[key], cand[key], tolerance,
                                         f"{path}.{key}"))
        return diffs
    if base != cand:
        diffs.append({"field": path, "baseline": base, "candidate": cand})
    return diffs


def _apply_ignore(table: str, rows, ignore: set[str]):
    fields = {f.split(".", 1)[1] for f in ignore if f.startswith(table + ".")}
    if not fields:
        return rows
    return [{k: v for k, v in row.items() if k not in fields} for row in rows]


def compare_table(table: str, base_rows, cand_rows, tolerance: float,
                  ignore: set[str]) -> dict:
    """按自然键/身份字段配对两轮快照，返回该表的差异汇总。"""
    spec = TABLE_SPECS[table]
    base_rows = _apply_ignore(table, base_rows, ignore)
    cand_rows = _apply_ignore(table, cand_rows, ignore)

    group_fields = spec["key_fields"] or spec["match_fields"]
    base_groups = _group_by(base_rows, group_fields)
    cand_groups = _group_by(cand_rows, group_fields)

    skip_fields = set(group_fields)
    missing, extra, mismatches = [], [], []
    matched = 0

    for key in sorted(set(base_groups) | set(cand_groups), key=_sortable_key):
        base_group = sorted(base_groups.get(key, []), key=_canonical)
        cand_group = sorted(cand_groups.get(key, []), key=_canonical)
        for i in range(max(len(base_group), len(cand_group))):
            if i >= len(base_group):
                extra.append(cand_group[i])
                continue
            if i >= len(cand_group):
                missing.append(base_group[i])
                continue
            matched += 1
            fields = []
            for field in sorted(set(base_group[i]) | set(cand_group[i])):
                if field in skip_fields:
                    continue
                fields.extend(diff_values(
                    base_group[i].get(field), cand_group[i].get(field),
                    tolerance, field,
                ))
            if fields:
                mismatches.append({
                    "key": dict(zip(group_fields, key)),
                    "baseline": base_group[i],
                    "candidate": cand_group[i],
                    "fields": fields,
                })

    return {
        "baseline_rows": len(base_rows),
        "candidate_rows": len(cand_rows),
        "matched": matched,
        "missing_total": len(missing),
        "extra_total": len(extra),
        "field_mismatch_total": len(mismatches),
        "missing": missing,
        "extra": extra,
        "field_mismatches": mismatches,
        "ok": not missing and not extra and not mismatches,
    }


def compare_snapshots(baseline: dict, candidate: dict,
                      tolerance: float = DEFAULT_TOLERANCE,
                      ignore=(), max_samples: int = DEFAULT_MAX_SAMPLES) -> dict:
    """比较两份快照；返回可直接写 JSON 的报告。"""
    if baseline.get("kind") != "snapshot" or candidate.get("kind") != "snapshot":
        raise ValueError("输入不是 ingest_parity snapshot（kind != 'snapshot'）")

    ignore = set(ignore or ())
    base_tables = baseline.get("tables") or {}
    cand_tables = candidate.get("tables") or {}
    if not base_tables or not cand_tables:
        raise ValueError("快照里没有任何表数据")
    common = [t for t in TABLE_SPECS if t in base_tables and t in cand_tables]
    # 两份快照表集合不一致（例如用了不同的 --table）本身就是差异，
    # 不当作"无法比较"直接报错——报告里列出 tables_only_in_one。
    only_one = sorted(set(base_tables) ^ set(cand_tables))

    report_tables = {}
    for table in common:
        result = compare_table(
            table,
            base_tables[table].get("rows") or [],
            cand_tables[table].get("rows") or [],
            tolerance,
            ignore,
        )
        # 报告只保留前 N 条样本，避免大表把文件撑爆；总数仍完整
        result["missing"] = result["missing"][:max_samples]
        result["extra"] = result["extra"][:max_samples]
        result["field_mismatches"] = result["field_mismatches"][:max_samples]
        report_tables[table] = result

    ok = all(r["ok"] for r in report_tables.values()) and not only_one
    return {
        "tool": "ingest_parity",
        "version": TOOL_VERSION,
        "kind": "diff",
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tolerance": tolerance,
        "ignore": sorted(ignore),
        "max_samples": max_samples,
        "baseline": {
            "label": baseline.get("label"),
            "snapshot_at": baseline.get("snapshot_at"),
            "window": baseline.get("window"),
        },
        "candidate": {
            "label": candidate.get("label"),
            "snapshot_at": candidate.get("snapshot_at"),
            "window": candidate.get("window"),
        },
        "tables_compared": common,
        "tables_only_in_one": only_one,
        "tables": report_tables,
        "ok": ok,
    }


# ==================== MySQL 快照 ====================

def _parse_time(text: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"时间格式无法解析: {text!r}（示例 2026-10-17 16:00:00）"
    )


def _read_table(conn, table: str, since: datetime, until: datetime,
                max_rows: int):
    spec = TABLE_SPECS[table]
    sql = (
        f"SELECT * FROM `{table}` "
        f"WHERE `{spec['time_column']}` >= %s AND `{spec['time_column']}` < %s "
        f"ORDER BY `{spec['time_column']}`, `id` LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (since, until, max_rows + 1))
        columns = [d[0] for d in (cur.description or ())]
        fetched = cur.fetchall()
    truncated = len(fetched) > max_rows
    rows = [dict(zip(columns, item)) for item in fetched[:max_rows]]
    return rows, truncated


def _snapshot_source(database: str) -> dict:
    import mysql_db
    return {
        "host": mysql_db.MYSQL_HOST,
        "port": mysql_db.MYSQL_PORT,
        "database": database,
        # 只记录凭据是否存在，绝不写入口令本身
        "has_password": bool(mysql_db.MYSQL_PASSWORD),
    }


def take_snapshot(args) -> int:
    import mysql_db

    since = args.since
    until = args.until or datetime.now()
    if until <= since:
        print(f"[ingest_parity] --until 必须晚于 --since（{since} >= {until}）")
        return 2

    tables = args.table or list(TABLE_SPECS)
    database = args.database or mysql_db.MYSQL_DB

    try:
        conn = mysql_db.get_connection(database)
    except Exception as e:
        print(f"[ingest_parity] 连接 MySQL 失败: {type(e).__name__}: {e}")
        return 2

    snapshot = {
        "tool": "ingest_parity",
        "version": TOOL_VERSION,
        "kind": "snapshot",
        "label": args.label,
        "snapshot_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {
            "since": since.strftime("%Y-%m-%d %H:%M:%S"),
            "until": until.strftime("%Y-%m-%d %H:%M:%S"),
            "bounds": "[since, until)",
        },
        "source": _snapshot_source(database),
        "tables": {},
    }

    try:
        for table in tables:
            rows, truncated = _read_table(conn, table, since, until, args.max_rows)
            entry = snapshot_table(table, rows)
            entry["truncated"] = truncated
            snapshot["tables"][table] = entry
    except Exception as e:
        print(f"[ingest_parity] 读取失败: {type(e).__name__}: {e}")
        return 2
    finally:
        conn.close()

    out = Path(args.out) if args.out else _default_report_path(
        f"ingest_parity_{_safe_label(args.label)}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print(f"[ingest_parity] 快照 {args.label} → {out}")
    print(f"  窗口 [{snapshot['window']['since']}, {snapshot['window']['until']})")
    for table, entry in snapshot["tables"].items():
        collapsed = ""
        if entry["collapsed_by"]:
            by = ", ".join(entry["collapsed_by"])
            collapsed = (f"（原始 {entry['raw_row_count']} 行 → 按 {by} "
                         f"折叠为 {entry['row_count']} 行）")
        else:
            collapsed = f"（{entry['row_count']} 行）"
        truncated = "  [!] 达到 --max-rows 上限，快照被截断" if entry["truncated"] else ""
        print(f"  {table}: {collapsed}{truncated}")
    return 0


# ==================== 命令行 ====================

def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", label or "").strip("._-")
    return cleaned or "run"


def _default_report_path(prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPORT_DIR / f"{prefix}_{stamp}.json"


def _validate_ignore(ignore) -> set[str]:
    valid_tables = set(TABLE_SPECS)
    result = set()
    for item in ignore or ():
        if "." not in item:
            raise argparse.ArgumentTypeError(
                f"--ignore 需要 TABLE.FIELD 形式: {item!r}")
        table, field = item.split(".", 1)
        if table not in valid_tables or not field:
            raise argparse.ArgumentTypeError(
                f"--ignore 的表名无效: {item!r}（可选 {sorted(valid_tables)}）")
        matching = set(TABLE_SPECS[table]["key_fields"]) | set(
            TABLE_SPECS[table]["match_fields"])
        if field in matching:
            raise argparse.ArgumentTypeError(
                f"不能忽略配对字段: {item!r}（会影响行匹配，先改脚本里的 key_fields）")
        result.add(item)
    return result


def _print_diff(report: dict) -> None:
    base = report["baseline"]["label"] or "baseline"
    cand = report["candidate"]["label"] or "candidate"
    print(f"[ingest_parity] 对账 {base} vs {cand}"
          f"（数值容差 {report['tolerance']}）")
    for table, res in report["tables"].items():
        status = "OK" if res["ok"] else "DIFF"
        print(
            f"  {table:20s} 基线 {res['baseline_rows']:>5} / 候选 "
            f"{res['candidate_rows']:>5} 行；匹配 {res['matched']:>5}，"
            f"缺失 {res['missing_total']:>3}，多余 {res['extra_total']:>3}，"
            f"字段差异 {res['field_mismatch_total']:>3}  → {status}"
        )
        for sample in res["field_mismatches"][:3]:
            key = json.dumps(sample["key"], ensure_ascii=False, sort_keys=True)
            for field in sample["fields"][:3]:
                print(f"      {key} {field['field']}: "
                      f"{field['baseline']!r} → {field['candidate']!r}")
        for row in res["missing"][:2]:
            print(f"      缺失行: {json.dumps(row, ensure_ascii=False)}")
        for row in res["extra"][:2]:
            print(f"      多余行: {json.dumps(row, ensure_ascii=False)}")
    if report["tables_only_in_one"]:
        print(f"  [!] 仅出现在一份快照里的表: {report['tables_only_in_one']}")
    print(f"[ingest_parity] 结论: "
          f"{'一致（差异 = 0）' if report['ok'] else '不一致'}")


def run_compare(args) -> int:
    try:
        ignore = _validate_ignore(args.ignore)
    except argparse.ArgumentTypeError as e:
        print(f"[ingest_parity] {e}")
        return 2
    if args.tolerance < 0:
        print("[ingest_parity] --tolerance 不能为负数")
        return 2
    try:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        report = compare_snapshots(
            baseline, candidate,
            tolerance=args.tolerance,
            ignore=ignore,
            max_samples=args.max_samples,
        )
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[ingest_parity] 对账失败: {type(e).__name__}: {e}")
        return 2

    out = Path(args.out) if args.out else _default_report_path("ingest_parity_diff")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    _print_diff(report)
    print(f"[ingest_parity] 报告: {out}")
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="三张视频派生表的入库等价性对账（Python 写者 vs Go 写者）")
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="按时间窗从 MySQL 读取并保存快照")
    snap.add_argument("--label", default="run", help="快照标签（写入文件名与报告）")
    snap.add_argument("--since", required=True, type=_parse_time,
                      help="窗口起点（含），如 '2026-10-17 16:00:00'")
    snap.add_argument("--until", type=_parse_time, default=None,
                      help="窗口终点（不含），默认当前时间")
    snap.add_argument("--table", action="append", choices=list(TABLE_SPECS),
                      help="只快照指定表（可重复）；默认三张表")
    snap.add_argument("--database", default=None, help="覆盖库名（默认 retail_assistant）")
    snap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                      help=f"单表最多读取行数（默认 {DEFAULT_MAX_ROWS}，超出会标记截断）")
    snap.add_argument("--out", default=None, help="快照输出路径")
    snap.set_defaults(func=take_snapshot)

    cmp = sub.add_parser("compare", help="比较两份快照；差异非 0 时退出码为 1")
    cmp.add_argument("baseline", help="基线快照 JSON")
    cmp.add_argument("candidate", help="候选快照 JSON")
    cmp.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                     help=f"数值字段绝对容差（默认 {DEFAULT_TOLERANCE}）")
    cmp.add_argument("--ignore", action="append", default=[],
                     help="忽略字段，TABLE.FIELD 形式（可重复），如 alert_record.frame_id")
    cmp.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES,
                     help=f"每类差异最多保留的样本数（默认 {DEFAULT_MAX_SAMPLES}）")
    cmp.add_argument("--out", default=None, help="报告输出路径")
    cmp.set_defaults(func=run_compare)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
