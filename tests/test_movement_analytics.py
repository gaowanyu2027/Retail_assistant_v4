"""动线关联分析口径测试（台账 A16）。

被修的错误：支持度/置信度的分子按"转移**发生次数**"累加，分母却按"**轨迹数**"，
于是一条轨迹里逛 A→B→A→B 会让支持度超过 100%（文档写的口径是按轨迹去重）。

`_aggregate_paths` 是纯函数，不需要数据库/摄像头，直接喂构造好的动线明细。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.movement_analytics import _aggregate_paths  # noqa: E402


def _path(*zone_ids):
    return {"path": [{"zone_id": z, "zone_label": f"{z}-区"} for z in zone_ids]}


def _pair(pairs, a, b):
    for p in pairs:
        if p["from_zone"] == a and p["to_zone"] == b:
            return p
    return None


def test_support_and_confidence_never_exceed_one():
    """核心回归：支持度/置信度都必须 ≤ 1（原实现支持度可 >100%）。"""
    # 一条轨迹在 A、B 之间来回走 4 次 → 旧实现里 trans[(A,B)] = 4、total = 1 → support = 4.0
    paths = [_path("A", "B", "A", "B", "A", "B", "A", "B")]
    pairs, _, total = _aggregate_paths(paths)
    p = _pair(pairs, "A", "B")
    assert p is not None, pairs
    assert p["support"] <= 1.0, f"支持度超过 100%: {p}"
    assert p["confidence"] <= 1.0, f"置信度超过 100%: {p}"
    assert p["count"] == 1, f"同一轨迹内重复转移应只算一次: {p}"
    assert total == 1


def test_transition_counted_once_per_trajectory():
    """两条轨迹各出现一次 A→B + 一条轨迹来回两次 → 计 3 条轨迹（不是 5 次）。"""
    paths = [_path("A", "B"), _path("A", "B"), _path("A", "B", "A", "B")]
    pairs, _, _ = _aggregate_paths(paths)
    p = _pair(pairs, "A", "B")
    assert p["count"] == 3, p
    assert p["support"] == 1.0, p            # 3 条轨迹都出现过 A→B
    assert p["confidence"] == 1.0, p         # 分母 = 访问过 A 的轨迹数 = 3


def test_confidence_denominator_is_trajectories_visiting_a():
    """置信度分母 = 访问过 A 的**轨迹数**（不是 A 的出边次数）。"""
    paths = [
        _path("A", "B"),          # A→B
        _path("A", "C"),          # A→C（A 的两条出边各一次）
        _path("B", "C"),          # 不含 A
    ]
    pairs, flow, total = _aggregate_paths(paths)
    ab = _pair(pairs, "A", "B")
    ac = _pair(pairs, "A", "C")
    assert ab["count"] == 1 and ac["count"] == 1
    assert ab["confidence"] == 0.5 and ac["confidence"] == 0.5, (ab, ac)
    assert ab["support"] == round(1 / 3, 4), ab
    assert flow["A"]["visits"] == 2, flow["A"]       # 2 条轨迹访问过 A
    assert total == 3


def test_zone_flow_includes_terminal_zones():
    """只进不出的区域（如出口）也要出现在 zone_flow 里，top_next 为空。"""
    paths = [_path("A", "exit")]
    _, flow, _ = _aggregate_paths(paths)
    assert flow["exit"]["visits"] == 1, flow
    assert flow["exit"]["top_next"] == [], flow["exit"]
    assert flow["A"]["top_next"][0] == {"zone_id": "exit", "label": "exit-区", "count": 1}


def test_same_zone_consecutive_is_not_a_transition():
    """同区连续访问（A→A）不算转移，但仍算"访问过 A"。"""
    paths = [_path("A", "A", "A", "B")]
    pairs, flow, _ = _aggregate_paths(paths)
    assert _pair(pairs, "A", "A") is None, pairs
    assert _pair(pairs, "A", "B")["count"] == 1, pairs
    assert flow["A"]["visits"] == 1


def test_zone_label_reused_when_missing():
    """标签缺失时沿用同一区域已知的标签，不要退化成 zone_id。"""
    paths = [{"path": [{"zone_id": "A", "zone_label": "零食区"}, {"zone_id": "B"}]}]
    pairs, _, _ = _aggregate_paths(paths)
    p = _pair(pairs, "A", "B")
    assert p["from_label"] == "零食区", p
    assert p["to_label"] == "B", p          # 未知区域只能用它自己的 id


def test_empty_and_malformed_paths_are_tolerated():
    """空/畸形明细不能让分析炸掉（历史脏数据很常见）。"""
    paths = [{"path": None}, {"path": []}, {"path": [{"zone_id": None}]}, {}]
    pairs, flow, total = _aggregate_paths(paths)
    assert pairs == [] and flow == {}
    assert total == 4          # 分母仍是"拿到的轨迹条数"，与旧实现一致


def test_simulated_pattern_matches_docstring_expectation():
    """`seed_simulated_paths` 声称 shelf_A→shelf_B 置信度明显偏高 —— 用同一模式验证口径。"""
    paths = [_path("shelf_A", "shelf_B", "checkout")] * 45
    paths += [_path("shelf_A", "shelf_C", "checkout")] * 5
    pairs, _, total = _aggregate_paths(paths)
    ab = _pair(pairs, "shelf_A", "shelf_B")
    assert ab["count"] == 45 and total == 50
    assert ab["confidence"] == 0.9, ab
    assert ab["support"] == 0.9, ab
    # 从 shelf_A 出发的最高置信度必须是 shelf_B（这正是"零食区后常去饮料区"的业务结论）
    from_a = [p for p in pairs if p["from_zone"] == "shelf_A"]
    assert from_a[0]["to_zone"] == "shelf_B", from_a
    # 排序契约：整体按置信度降序（shelf_B→checkout 是 1.0，所以排在 shelf_A→shelf_B 前面）
    confs = [p["confidence"] for p in pairs]
    assert confs == sorted(confs, reverse=True), confs
