"""CSP 与"注入脚本"的兼容性回归守卫（2026-09-19 实测踩坑）。

## 背景：两个功能各自都对，叠加起来互相打架

- **安全加固**（台账 B7）给所有响应加了 CSP：`script-src 'self'`，理由写得很清楚：
  "前端产物全部是同源外部脚本（Vite 构建，index.html 无内联脚本），所以不需要 unsafe-inline" ✓
- **前端兜底提示**（Vue 加载失败时弹"前端资源加载异常"）由 `api/main.py` 用
  `html.replace("</body>", VUE_FALLBACK_SCRIPT + "</body>")` **以一段内联 `<script>` 注入** ✓

叠加后的实测现象（浏览器 Console）：

    Refused to execute inline script because it violates the following
    Content Security Policy directive: "script-src 'self'".

代价不只是报错：**兜底提示本身失效** —— 在"前端资源挂了/Vue 没挂载"这种最需要它的场景下，
用户看到的是白屏，一个字都没有（这正是它要解决的场景）。

## 修法与守卫

逻辑搬到 `frontend-vue/public/js/vue-fallback.js`（构建后 `/js/vue-fallback.js`），
服务端只注入一行 `<script src=...>`。本文件把这个不变量钉住：
不允许任何"注入到页面的脚本"是内联的。
"""
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MAIN_PY = PROJECT_ROOT / "api" / "main.py"
FALLBACK_JS = PROJECT_ROOT / "frontend-vue" / "public" / "js" / "vue-fallback.js"

# 匹配"没有 src 属性的 script 开标签"——那就是内联脚本
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)


def _fallback_script_value() -> str:
    """取出 api/main.py 里 VUE_FALLBACK_SCRIPT 的字符串值。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    m = re.search(r"VUE_FALLBACK_SCRIPT\s*=\s*(['\"])(.*?)\1", src, re.S)
    assert m, "api/main.py 里找不到 VUE_FALLBACK_SCRIPT"
    return m.group(2)


def test_injected_fallback_script_is_not_inline():
    """注入到页面的脚本必须是外部引用 —— 内联会被 `script-src 'self'` 拦掉。"""
    value = _fallback_script_value()
    hit = _INLINE_SCRIPT.search(value)
    assert not hit, (
        "VUE_FALLBACK_SCRIPT 又变成内联脚本了（会被 CSP script-src 'self' 拦掉、兜底失效）："
        f"{hit.group(0)[:80]}"
    )
    assert "src=" in value, "注入的应该是带 src 的外部脚本引用"


def test_fallback_file_exists_and_has_the_logic():
    """外部文件必须存在，且包含三段兜底逻辑（资源失败 / Promise 异常 / 挂载超时）。"""
    assert FALLBACK_JS.exists(), f"缺少 {FALLBACK_JS.relative_to(PROJECT_ROOT)}"
    js = FALLBACK_JS.read_text(encoding="utf-8")
    assert "__dshVueFallback" in js, "防重复提示的标记不见了"
    assert "/api/frontend/fallback" in js, "失败上报不见了"
    assert "unhandledrejection" in js and "挂载超时" in js, "兜底触发条件不完整"


def test_csp_stays_strict():
    """CSP 不许为了迁就内联脚本而放宽（`unsafe-inline` 只能出现在 style-src）。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    m = re.search(r'"Content-Security-Policy",\s*(.*?)\)\s*\n', src, re.S)
    assert m, "找不到 CSP 头设置"
    block = m.group(1)
    assert "script-src 'self'" in block, "script-src 不再是 'self'"
    # style-src 允许 unsafe-inline（index.html 有内联 <style>），script-src 绝不允许
    script_part = block.split("style-src")[0]
    assert "unsafe-inline" not in script_part, "script-src 里出现了 unsafe-inline —— 加固被放宽了"


def test_built_index_html_has_no_inline_script():
    """构建产物本身也不该有内联脚本（否则 CSP 同样会拦，且必须重建才能发现）。"""
    index = PROJECT_ROOT / "frontend-vue" / "dist" / "index.html"
    if not index.exists():
        return          # 未构建时跳过（CI 不构建前端，这里保持离线可跑）
    html = index.read_text(encoding="utf-8")
    hit = _INLINE_SCRIPT.search(html)
    # 允许内联 <style>（v-cloak），但不允许内联 <script>
    assert not hit, f"dist/index.html 出现了内联脚本：{hit.group(0)[:80]}"
