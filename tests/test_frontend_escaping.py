"""前端渲染转义回归守卫（台账 B5）。

问题：ECharts `tooltip.formatter` 与 `innerHTML` 拼的是 **HTML 字符串**，
而 `zone_label`（区域名）可由任意 `system:manage` 账号通过 `PUT /api/zones` 修改 ——
不转义就是**存储型 XSS**：改个区域名，别人打开看板就中招。

本测试是**静态回归守卫**（断言"危险写法"不再出现、转义调用在场），
真正的转义行为由一次性 Node 脚本实测（结论记在 改进记录 · 模块 B/E）。
"""
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHART_JS = PROJECT_ROOT / "frontend-vue" / "public" / "js" / "chart.js"
APP_VUE = PROJECT_ROOT / "frontend-vue" / "src" / "App.vue"
VOICE_JS = PROJECT_ROOT / "frontend-vue" / "public" / "js" / "voice.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_chart_tooltip_escapes_zone_label():
    """tooltip 里的区域名必须转义（B5 的正主）。"""
    src = _read(CHART_JS)
    assert "escapeHtml(params[0].name)" in src, "tooltip 未对区域名做 HTML 转义"
    # 反例守卫：不能再出现"裸拼"的写法
    assert "${params[0].name}" not in src, "tooltip 又出现了未转义的裸拼写法"


def test_chart_js_defines_escape_helper():
    src = _read(CHART_JS)
    assert re.search(r"function escapeHtml\(", src), "缺少 escapeHtml 助手"
    # 五个字符都要覆盖，漏一个就还有绕法
    for ch in ("&", "<", ">", '"', "'"):
        assert ch in src.split("function escapeHtml(", 1)[1][:400], f"escapeHtml 未覆盖 {ch}"


def test_app_vue_alert_reasons_are_escaped():
    """告警 reasons 曾经裸拼 innerHTML（附三那条低危项），现在必须走 escapeHtml。"""
    src = _read(APP_VUE)
    assert "this.escapeHtml((a.reasons || [])" in src, "告警 reasons 未转义"
    assert "${(a.reasons || []).join" not in src, "又出现未转义的 reasons 拼接"


def test_app_vue_zone_labels_are_escaped():
    """区域名出现的两处都必须转义。"""
    src = _read(APP_VUE)
    for m in re.finditer(r"z\.zone_label", src):
        line = src[:m.start()].count("\n") + 1
        line_text = src.splitlines()[line - 1]
        if "innerHTML" in line_text or "<span" in line_text or "<div" in line_text:
            assert "escapeHtml" in line_text, f"App.vue:{line} 的 zone_label 未转义：{line_text.strip()}"


def test_voice_js_keeps_escaping():
    """voice.js 原本就转义（作为参照实现），别被后来的改动破坏。"""
    src = _read(VOICE_JS)
    assert "escapeHtml(text)" in src, "voice.js 的转义被移除了"
