"""前端"静默失败"回归守卫（台账：演示数据按钮点了没反应 / 面板空白无解释）。

背景（用户两次报障，都是同一类根因）：
1. 「生成演示数据没反应」—— `bindSalesSimulate()` 里 `await fetch(...)` **不看 `resp.ok`**，
   `catch` 又是空语句 → 权限不足(403) 或后端 500 时按钮**毫无反馈**；
2. 「热度排行一直空着」—— `fetchPopularityReport()` 只有 `if (resp.ok) {...}` **没有 else**，
   MySQL 掉线导致的 500 静默丢弃；`fetchHotVsSales()` 是 `if (!resp.ok) return`。

**这类 bug 的共同点**：接口真的失败了，但界面与"真的没数据"长得一模一样。
服务端测试发现不了（接口返回 500 是正确的），所以这里做**静态守卫** +
`describeFetchFailure()` 这个统一出口的存在性断言。

对应修复见 `改进记录.md`「静默失败三件套」。
"""
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

APP_VUE = PROJECT_ROOT / "frontend-vue" / "src" / "App.vue"


def _read() -> str:
    return APP_VUE.read_text(encoding="utf-8")


def _strip_comments(js: str) -> str:
    """剥掉注释再断言。

    ⚠ 必须做：修复时**故意在注释里写了旧写法**（"原来是 `if (!resp.ok) return`"），
    第一版测试就是这么被自己的注释判失败的（4 条全红，实际代码是对的）。
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"//[^\n]*", "", js)
    return js


def _fn_body(src: str, signature: str) -> str:
    """粗粒度截取一个方法的函数体（到下一个同级 `},` 结束），并剥掉注释。"""
    assert signature in src, f"没找到 {signature}（方法被改名了？）"
    body = src.split(signature, 1)[1].split("\n    },", 1)[0]
    return _strip_comments(body)


def test_comments_are_stripped_so_old_snippets_do_not_false_positive():
    """守卫自身的行为：注释里的旧写法不该算违规。"""
    sample = "// 原来是 `if (!resp.ok) return` 和 catch (e) {}\nif (resp.ok) { ok() }"
    assert "if (!resp.ok) return" in sample          # 原样确实能匹配到
    assert "if (!resp.ok) return" not in _strip_comments(sample)


def test_failure_helper_exists_and_translates_http_errors():
    """统一失败出口必须存在：把 HTTP 状态 + 后端 detail 翻译成人话。"""
    src = _read()
    body = _fn_body(src, "async describeFetchFailure(")
    assert "resp.status" in body, "没有读 HTTP 状态"
    assert "detail" in body, "没有取后端的 detail（用户看到的信息会没有原因）"
    assert "updateStatus" in body, "没有写到状态栏"
    assert "401" in body and "403" in body, "没有把'登录过期/权限不足'翻译成人话"


def test_popularity_report_does_not_fail_silently():
    """热度排行接口失败时必须有 else 分支（原来只有 if (resp.ok)）。"""
    body = _fn_body(_read(), "async fetchPopularityReport(")
    assert "catch (e) {}" not in body, "又是空 catch —— 网络异常会静默"
    assert "describeFetchFailure" in body, "接口失败没有走统一失败出口"
    assert "ranking-list" in body, "失败信息没有落到排行榜容器里"


def test_hot_vs_sales_does_not_silently_return():
    """`if (!resp.ok) return` 这种静默早返回不许再出现。"""
    src = _read()
    body = _fn_body(src, "async fetchHotVsSales(")
    assert "if (!resp.ok) return" not in body, "又是静默早返回（面板空白、无解释）"
    assert "describeFetchFailure" in body, "接口失败没有走统一失败出口"
    assert "catch (e) {}" not in body, "空 catch 把网络异常吞掉了"


def test_sales_simulate_button_checks_ok_and_recovers():
    """演示数据按钮：必须检查 resp.ok、失败要有提示、结束要恢复按钮状态。

    ⚠ 签名要带缩进与 `{`：`bindSalesSimulate()` 在别处还有**调用点**
    （`this.bindSalesSimulate()`），只按名字切会切到调用点、截出无关代码（第一版就踩了）。
    """
    body = _fn_body(_read(), "\n    bindSalesSimulate() {")
    assert "resp.ok" in body, "按钮还是不看 HTTP 状态（用户看到的就是'点了没反应'）"
    assert "data:write" in body, "权限不足时没有给出可操作的解释"
    assert "catch (e) {}" not in body, "空 catch：网络异常静默"
    assert "finally" in body and "disabled = false" in body, \
        "失败后按钮没有恢复（会一直卡在'生成中…'）"


def test_no_silent_early_return_left_in_app_vue():
    """整文件级别：静默早返回这种写法不该再出现（已全部改为显式报错）。

    注意要先剥注释：修复说明里**故意引用**了旧写法，直接匹配会命中注释。
    """
    src = _strip_comments(_read())
    assert "if (!resp.ok) return" not in src, "App.vue 里又出现了静默早返回"


def test_fetch_functions_do_not_swallow_errors():
    """**发起 fetch 的方法**里不许有空 catch —— 那正是"面板空白、无解释"的来源。

    刻意不要求"全文件零空 catch"：画布绘制、图表 resize、WebSocket 收包等
    本来就允许尽力而为地失败（那种地方加提示只会变噪音）。真正要守的是
    请求失败必须可见 —— 所以只约束含 `await fetch(` 的方法。
    """
    import re as _re

    src = _read()
    clean = _strip_comments(src)
    offenders = []
    for m in _re.finditer(r"\n    (async )?([A-Za-z_]\w*)\(([^)]*)\) \{(.*?)\n    \},", clean, _re.S):
        name, body = m.group(2), m.group(4)
        if "await fetch(" in body and "catch (e) {}" in body:
            offenders.append(name)
    assert not offenders, f"这些发起 fetch 的方法仍静默吞异常：{offenders}"
