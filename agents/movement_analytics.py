"""
购物动线分析 — 顾客访问序列的关联规则挖掘

业务问题：顾客逛完 A 货架后最可能去哪个区域？哪些商品该相邻陈列？

方法：对持久化的轨迹动线（track_visit_paths）做 A→B 转移分析：
- 支持度 support(A→B) = A→B 出现的轨迹数 / 总轨迹数（A、B 共同出现的普遍性）
- 置信度 confidence(A→B) = A→B 出现的轨迹数 / 访问过 A 的轨迹数（A 之后去 B 的比例）
- 提升度可选（后续扩展）

数据源：track_visit_paths（视频管线自动落库 / 测试模拟数据）
"""
import json
from collections import Counter, defaultdict


def analyze_movement_paths(source: str | None = None, limit: int = 2000, top: int = 10) -> dict:
    """统计 A→B 转移，输出置信度最高的关联（用于陈列/促销决策）。

    返回：
    {
        "total_paths": N,
        "top_pairs": [{from_zone, from_label, to_zone, to_label,
                       count, confidence, support}, ...],
        "zone_flow": {zone_id: {label, visits, top_next: [...]}},   # 每个区域最常去的下一站
        "summary": "整体结论",
    }
    """
    import mysql_db

    paths = mysql_db.get_track_visit_paths(source=source, limit=limit)
    total = len(paths)

    # 转移计数：count[(A,B)]；A 的访问次数：from_count[A]
    trans = Counter()
    from_count = Counter()
    zone_label: dict[str, str] = {}
    for p in paths:
        zones = [z for z in (p.get("path") or []) if isinstance(z, dict) and z.get("zone_id")]
        for i in range(len(zones) - 1):
            a = zones[i]["zone_id"]
            b = zones[i + 1]["zone_id"]
            zone_label[a] = zones[i].get("zone_label") or zone_label.get(a, a)
            zone_label[b] = zones[i + 1].get("zone_label") or zone_label.get(b, b)
            if a != b:  # 同区连续访问不算转移
                trans[(a, b)] += 1
                from_count[a] += 1

    # 置信度排序
    pairs = []
    for (a, b), cnt in trans.items():
        conf = cnt / from_count[a] if from_count[a] else 0
        pairs.append({
            "from_zone": a, "from_label": zone_label.get(a, a),
            "to_zone": b, "to_label": zone_label.get(b, b),
            "count": cnt,
            "confidence": round(conf, 3),
            "support": round(cnt / max(total, 1), 4),
        })
    pairs.sort(key=lambda x: (x["confidence"], x["count"]), reverse=True)

    # 每区域最常去的下一站
    zone_flow = {}
    for a in from_count:
        nexts = defaultdict(int)
        for (fa, fb), cnt in trans.items():
            if fa == a:
                nexts[fb] += cnt
        top_next = sorted(nexts.items(), key=lambda x: x[1], reverse=True)[:3]
        zone_flow[a] = {
            "label": zone_label.get(a, a),
            "visits": from_count[a],
            "top_next": [
                {"zone_id": z, "label": zone_label.get(z, z), "count": c}
                for z, c in top_next
            ],
        }

    summary = _summarize(pairs, total)
    return {
        "total_paths": total,
        "top_pairs": pairs[:top],
        "zone_flow": zone_flow,
        "summary": summary,
    }


def _summarize(pairs: list[dict], total: int) -> str:
    if total == 0:
        return "暂无动线数据。请先运行视频采集（或生成测试模拟数据）后再分析。"
    if not pairs:
        return f"已采集 {total} 条动线，但未发现区域间的跨区转移（顾客可能只停留单一区域）。"
    top = pairs[0]
    return (
        f"共分析 {total} 条顾客动线。最显著的关联："
        f"{top['from_label']} → {top['to_label']}（置信度 {top['confidence']*100:.0f}%"
        f"，共 {top['count']} 次转移），建议将两区域商品相邻陈列或做交叉促销。"
    )


def seed_simulated_paths(count: int = 200) -> int:
    """生成测试用模拟动线数据（source=simulated，明确区别于真实采集）。

    构造可验证的业务模式：
    - 模式1 购物（60%）：货架A/B/C 中 2-3 个 → 收银台
    - 模式2 闲逛（25%）：多个货架 → 出口
    - 模式3 直奔（15%）：单货架 → 出口
    并让 shelf_A → shelf_B 的置信度明显偏高（模拟"零食区后常去饮料区"）。
    """
    import random
    import mysql_db

    zones = [
        ("shelf_A", "1号货架 - 零食区"), ("shelf_B", "2号货架 - 饮料区"),
        ("shelf_C", "3号货架 - 日用品区"), ("checkout", "收银台"), ("exit", "出口"),
    ]
    zone_id = {z: lbl for z, lbl in zones}
    rand = random.Random(42)  # 固定种子，结果可复现

    def make_path(seq: list[str]) -> list[dict]:
        out = []
        for i, z in enumerate(seq):
            out.append({
                "zone_id": z, "zone_label": zone_id[z],
                "dwell_seconds": round(rand.uniform(8, 120), 1) if z.startswith("shelf") else round(rand.uniform(3, 20), 1),
            })
        return out

    paths = []
    for i in range(count):
        r = rand.random()
        if r < 0.45:
            # 模式1a：零食区 → 饮料区（制造强关联）
            seq = ["shelf_A", "shelf_B"] + rand.sample(["shelf_C", "checkout"], k=1)
        elif r < 0.60:
            # 模式1b：其他货架组合 → 收银台
            seq = rand.sample(["shelf_A", "shelf_B", "shelf_C"], k=2) + ["checkout"]
        elif r < 0.85:
            # 模式2：闲逛 → 出口
            seq = rand.sample(["shelf_A", "shelf_B", "shelf_C"], k=3) + ["exit"]
        else:
            # 模式3：直奔单一货架 → 出口
            seq = [rand.choice(["shelf_A", "shelf_B", "shelf_C"]), "exit"]
        paths.append((f"sim_seed_{i}", i, make_path(seq)))

    for session_id, track_id, path in paths:
        mysql_db.save_track_visit_path(
            session_id=session_id, track_id=track_id, path=path,
            source="simulated",
            total_dwell_seconds=round(sum(p["dwell_seconds"] for p in path), 1),
        )
    return len(paths)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(analyze_movement_paths(), ensure_ascii=False, indent=2))
