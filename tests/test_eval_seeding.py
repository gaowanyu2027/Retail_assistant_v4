"""评测种子数据注入的**接线**测试（离线：不连 MySQL、不调 LLM）。

背景（CI run#21）：多轮用例断言"回答要提到货架"，而 CI 的 MySQL 是每次新建的空库 →
模型只能说"无数据"，断言变成考环境。做法是评测前用**产品自带的演示数据生成器**注入夹具
（`evals/run_evals.py::_seed_minimal_data` → `agents/traffic_analytics.seed_traffic_demo`）。

本文件只验证接线（是否调用、能否关闭、失败是否兜住）；真实写库由 CI 的 eval job 覆盖。
"""
import importlib.util
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "run_evals_seed_mod", PROJECT_ROOT / "evals" / "run_evals.py")
run_evals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_evals)


def _with_generator(fake, fn):
    """临时把 `seed_traffic_demo` 换成替身（装卸都在这一处，避免用例之间互相污染）。"""
    try:
        import agents.traffic_analytics as ta
    except Exception as e:                                  # 最小依赖环境
        print(f"       [SKIP] 无法导入 traffic_analytics：{e}")
        return None
    orig = getattr(ta, "seed_traffic_demo", None)
    ta.seed_traffic_demo = fake
    try:
        return fn()
    finally:
        if orig is not None:
            ta.seed_traffic_demo = orig


def test_seed_runs_by_default():
    """默认（未设 EVAL_SEED_DATA）→ 必须调用生成器。"""
    calls = []

    def fake():
        calls.append(1)
        return 24

    os.environ.pop("EVAL_SEED_DATA", None)
    _with_generator(fake, run_evals._seed_minimal_data)
    assert calls, "默认应该注入演示数据（否则空库会让数据类断言失真）"


def test_seed_can_be_disabled():
    """`EVAL_SEED_DATA=0` → 跳过（想跑"空库"场景时用）。"""
    calls = []

    def fake():
        calls.append(1)
        return 24

    os.environ["EVAL_SEED_DATA"] = "0"
    try:
        _with_generator(fake, run_evals._seed_minimal_data)
    finally:
        os.environ.pop("EVAL_SEED_DATA", None)
    assert not calls, "设了 EVAL_SEED_DATA=0 仍然注入了数据"


def test_seed_failure_does_not_break_the_run():
    """生成器炸了 → 不能把整轮评测带崩（但要打印告警，便于发现夹具失效）。"""
    def boom():
        raise RuntimeError("库连不上")

    os.environ.pop("EVAL_SEED_DATA", None)
    _with_generator(boom, run_evals._seed_minimal_data)      # 不该抛异常
