package routing

import "testing"

func TestClassify(t *testing.T) {
	cases := []struct {
		path string
		want Kind
	}{
		{"/", Index},
		{"/js", Proxy}, // 无尾斜杠：交回 Python（Mount 的 307 跳转在原侧）
		{"/js/stream.js", Static},
		{"/css/dashboard.css", Static},
		{"/assets/index-D6NgqGLH.js", Static},
		{"/api/health", Health},
		{"/api/health/", Proxy},      // 尾斜杠：Python 的 307 跳转原样保留
		{"/api/health/ready", Proxy}, // 真探测必须打 Python
		{"/api/frontend/fallback", Fallback},
		{"/metrics", Metrics},
		// 兜底反代面（零枚举）：文档、兼容别名、WS、一切未知路径
		{"/docs", Proxy},
		{"/openapi.json", Proxy},
		{"/api/queries", Proxy},
		{"/api/ws/stream", Proxy},
		{"/favicon.ico", Proxy},
		{"/unknown", Proxy},
	}
	for _, c := range cases {
		if got := Classify(c.path); got != c.want {
			t.Errorf("Classify(%q) = %v, want %v", c.path, got, c.want)
		}
	}
}

func TestKindString(t *testing.T) {
	for _, k := range AllKinds {
		if k.String() == "unknown" {
			t.Errorf("Kind(%d) 缺少名称", k)
		}
	}
	if Kind(99).String() != "unknown" {
		t.Error("越界 Kind 应返回 unknown")
	}
}
