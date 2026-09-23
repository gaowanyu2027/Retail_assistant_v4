"""前端权限门（置灰）与"模板回调必须存在"的静态守卫（2026-09-22）。

## 背景

新增只读角色 `viewer` 后，后端会在写接口强制 403；但**界面仍把写按钮摆在眼前**，
点了才报错 —— 演示时观感差（"按钮能点但失败"）。所以在 `App.vue` 里按权限置灰/隐藏，
并给处理函数加一道兜底提示。

## 本文件守什么

1. **写操作控件必须绑定权限**（`canSystem` / `canWrite`）—— 新增写按钮若忘了绑，测试直接红；
2. **模板里的回调必须真实存在** —— 这条不是凑数：检查中发现模板里 `@click="simulateSales"`
   引用了**一个从未定义的方法**（点了只会抛错），这类"悬空回调"靠人眼很难发现。
"""
import io
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_VUE = PROJECT_ROOT / "frontend-vue" / "src" / "App.vue"

# 必须绑定 system:manage（设备/视频源控制）的控件 id
SYSTEM_CONTROLS = ["btn-camera-menu", "btn-local-cam", "btn-webcam", "btn-upload", "btn-stop"]


def _source():
    return io.open(APP_VUE, encoding="utf-8").read()


def _tag_of(src, element_id):
    """取出包含该 id 的整个标签（从 `<` 到匹配的 `>`）。"""
    m = re.search(r"<[^>]*id=\"" + re.escape(element_id) + r"\"[^>]*>", src, re.S)
    return m.group(0) if m else None


def test_system_controls_are_permission_gated():
    """设备/视频源类按钮必须绑 `canSystem`（否则只读账号点了会 403）。"""
    src = _source()
    missing = []
    for cid in SYSTEM_CONTROLS:
        tag = _tag_of(src, cid)
        if tag is None:
            missing.append(f"{cid}: 找不到该控件（改名了就更新本测试的白名单）")
        elif "canSystem" not in tag:
            missing.append(f"{cid}: 未绑定 canSystem")
    assert not missing, "以下写操作控件没有按权限置灰：\n  " + "\n  ".join(missing)


def test_demo_sales_button_is_permission_gated():
    """「生成演示数据」的三种渲染路径（模板 + 两处 JS 注入）都要走权限判断。"""
    src = _source()
    assert "_demoSalesBtnHtml" in src, "缺少按权限渲染演示数据按钮的辅助方法"
    # 模板里的那一处（`@click="simulateSales"` 的按钮要绑 canWrite）
    assert re.search(r'@click="simulateSales"[\s\S]{0,300}?canWrite', src), \
        "模板中的演示数据按钮没有绑定 canWrite"
    # 两处 JS 注入都必须改用辅助方法（数量与被调用次数一致：2 处调用）
    assert src.count("this._demoSalesBtnHtml(") >= 2, "JS 注入的演示数据按钮没有全部走权限辅助方法"
    # 更硬的判据：手写的按钮 HTML 只允许出现在辅助方法内部
    total = src.count('id="btn-sales-simulate-vue"')
    helper = re.search(r"_demoSalesBtnHtml\(label[\s\S]*?\n    \},", src)
    inside = helper.group(0).count('id="btn-sales-simulate-vue"') if helper else 0
    assert total == inside and total > 0, \
        f"还有手写的演示数据按钮 HTML（未走权限辅助方法）：总 {total} 处，辅助方法内 {inside} 处"


def test_template_click_handlers_exist():
    """模板里 `@click="foo"` / `@click="foo(...)"` 调用的方法必须在 methods 里定义。

    这条能挡住"悬空回调"——例如模板引用了一个从未实现的方法，点击时只在控制台抛错，
    界面上表现为"点了没反应"（本项目真出现过：`simulateSales`）。
    """
    src = _source()
    # 模板部分 = <template> ... </template> 的第一个完整块；脚本部分取其后的内容
    tpl_end = src.find("</template>")
    template, script = src[:tpl_end], src[tpl_end:]

    called = set()
    for m in re.finditer(r'@click(?:\.\w+)*="([^"]+)"', template):
        expr = m.group(1).strip()
        # 取首个标识符；跳过内联表达式/全局/以 $ 开头的
        mm = re.match(r"([A-Za-z_$][\w$]*)", expr)
        if not mm:
            continue
        name = mm.group(1)
        if name.startswith("$") or name in {"true", "false", "null"}:
            continue
        # 只校验"看起来是方法调用/方法引用"的写法（含括号或整个表达式就是标识符）
        if "(" in expr or expr == name:
            called.add(name)

    # methods 区里的定义形如 `    foo(` / `    async foo(`（含多行签名情况）
    defined = set(re.findall(r"^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", script, re.M))
    # computed / data 也可能作为 `@click="canWrite ? a() : b()"` 之类的引用，这里只查方法名
    missing = sorted(n for n in called if n not in defined)
    assert not missing, f"模板里调用了未定义的方法（点击会抛错）：{missing}"
