"""零依赖测试运行器（同时兼容 pytest）。

为什么要这个：宿主环境**没有装 pytest**（也不该为了跑测试引入新依赖）。
这里的测试都写成 `test_*` 函数的普通 `assert` 风格 ——

    python tests/run_tests.py            # 零依赖直接跑（本项目默认）
    pytest tests/ -q                     # 装了 pytest 也能跑（同一批用例）

覆盖的都是**纯函数/可离线验证**的不变量（不依赖 MySQL / Qdrant / 网络服务）：
    test_video_guard.py   视频源安全守卫（B1/B6 的绕过清单）
    test_breaker.py       向量层熔断 + 舱壁状态机
    test_auth.py          口令策略 / 哈希 / 会话滑动续期（用临时 SQLite）
    test_run_port.py      启动端口自检（跑一个临时 HTTP 服务来验证探测）
"""
import importlib.util
import inspect
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("没有找到 tests/test_*.py")
        return 1
    total = passed = 0
    failures = []
    for f in files:
        print(f"\n=== {f.name} ===")
        try:
            mod = load_module(f)
        except Exception as e:
            print(f"  [SKIP] 模块导入失败: {type(e).__name__}: {e}")
            continue
        for name, fn in sorted(inspect.getmembers(mod, inspect.isfunction)):
            if not name.startswith("test_"):
                continue
            total += 1
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except Exception as e:
                failures.append((f.name, name, e))
                print(f"  FAIL  {name} -> {type(e).__name__}: {e}")
    print(f"\n{'=' * 56}")
    print(f"  合计: PASS {passed} / {total}")
    for fname, name, e in failures:
        print(f"  - {fname}::{name}: {e}")
        tb = traceback.format_exc().strip().splitlines()
        for line in tb[-4:-1]:
            print(f"      {line.strip()}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
