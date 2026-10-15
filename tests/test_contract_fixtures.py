"""契约夹具测试（混合架构迁移 · S0 冻结）：permissions.json / guard_cases.json 与当前实现比对。

夹具是**冻结快照**：改了实现就必须同步跑 `python contracts/export_fixtures.py` 更新夹具，
让 diff 显式可见 —— 这正是"契约冻结"的目的，不得让夹具悄悄漂移。
（期望值全部来自实测导出，不是手抄；更新流程见 contracts/README.md。）
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api.security as sec  # noqa: E402
import video_sources as vs  # noqa: E402

CONTRACTS_DIR = PROJECT_ROOT / "contracts"


def _fixture(name: str) -> dict:
    # 每次读盘：run_tests.py 在同一进程里跑全部用例，避免"改了夹具还是旧值"的假绿
    return json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))


def _assert_expect(call, expect: dict, ctx: str) -> None:
    """按夹具的 expect 结构断言一次守卫调用。

    expect 三选一：
      {"ok": ...}            —— 必须放行且结果等于 ok（Path 结果转 str 比对）
      {"error": "code"}      —— 必须以该错误码拒绝
      {"error_in": [...]}    —— 必须拒绝，错误码属于集合（跨平台差异，见 README）
    """
    try:
        out = call()
    except vs.SourceError as e:
        if "error" in expect:
            assert e.code == expect["error"], f"{ctx}: 期望拒绝码 {expect['error']}，实际 {e.code}"
            return
        if "error_in" in expect:
            assert e.code in expect["error_in"], \
                f"{ctx}: 期望拒绝码属于 {expect['error_in']}，实际 {e.code}"
            return
        raise AssertionError(f"{ctx}: 应当放行，却被拒绝 [{e.code}] {e}")
    if "ok" not in expect:
        raise AssertionError(f"{ctx}: 应当被拒绝，却通过了：{out!r}")
    if isinstance(out, Path):
        out = str(out)
    assert out == expect["ok"], f"{ctx}: 期望 {expect['ok']!r}，实际 {out!r}"


def test_permissions_fixture_matches_security_module():
    """权限矩阵：夹具 == api/security.py 的当前值（安全契约，改动必须显式化）。"""
    fx = _fixture("permissions.json")
    assert fx["roles"] == list(sec.ROLES)
    assert fx["role_labels"] == {r: sec.ROLE_LABELS[r] for r in sec.ROLES}
    assert fx["perms_of"] == {r: sec.perms_of(r) for r in sec.ROLES}


def test_guard_url_cases_match_fixture():
    for case in _fixture("guard_cases.json")["guard_url"]:
        url = case["url"]
        _assert_expect(lambda u=url: vs.guard_url(u), case["expect"], f"guard_url({url!r})")


def test_guard_path_cases_match_fixture():
    for case in _fixture("guard_cases.json")["guard_path"]:
        path, cs = case["path"], case["client_supplied"]
        _assert_expect(lambda p=path, c=cs: vs.guard_path(p, client_supplied=c),
                       case["expect"], f"guard_path({path!r}, client_supplied={cs})")


def test_guard_source_cases_match_fixture():
    for case in _fixture("guard_cases.json")["guard_source"]:
        src = case["in"]
        _assert_expect(lambda s=src: vs.guard_source(s), case["expect"], f"guard_source({src!r})")


def test_normalize_cases_match_fixture():
    for case in _fixture("guard_cases.json")["normalize"]:
        src = case["in"]
        # 深拷贝：normalize 不应被输入对象的复用影响（None 原样传入）
        _assert_expect(lambda s=src: vs.normalize(json.loads(json.dumps(s))),
                       case["expect"], f"normalize({src!r})")


def test_mask_credentials_cases_match_fixture():
    for case in _fixture("guard_cases.json")["mask_credentials"]:
        text = case["in"]
        _assert_expect(lambda t=text: vs.mask_credentials(t), case["expect"],
                       f"mask_credentials({text!r})")


def test_echo_for_legacy_cases_match_fixture():
    for case in _fixture("guard_cases.json")["echo_for_legacy"]:
        action, params = case["action"], case["params"]
        _assert_expect(lambda a=action, p=params: vs.echo_for_legacy(a, p), case["expect"],
                       f"echo_for_legacy({action!r}, {params!r})")
