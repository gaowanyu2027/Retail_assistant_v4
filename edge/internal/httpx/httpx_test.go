package httpx

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// CSP 期望值与 api/main.py 的 security_headers 逐字一致；
// 若 Python 侧改了 CSP，此测试与 benchmark/route_parity.py 都会变红。
const wantCSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
	"img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss:; " +
	"font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"

func TestApplySecurityHeadersFillsGaps(t *testing.T) {
	h := http.Header{}
	ApplySecurityHeaders(h)
	checks := map[string]string{
		"X-Content-Type-Options":  "nosniff",
		"X-Frame-Options":         "DENY",
		"Referrer-Policy":         "no-referrer",
		"Permissions-Policy":      "camera=(self), microphone=(self), geolocation=(), payment=()",
		"Content-Security-Policy": wantCSP,
	}
	for k, v := range checks {
		if got := h.Get(k); got != v {
			t.Errorf("%s = %q, want %q", k, got, v)
		}
	}
	if h.Get("Strict-Transport-Security") != "" {
		t.Error("HTTP 下不应发送 HSTS")
	}
}

func TestApplySecurityHeadersKeepsExisting(t *testing.T) {
	// 反代场景：Python 已设置的头必须原样保留（setdefault 语义）。
	h := http.Header{}
	h.Set("Content-Security-Policy", "python-set")
	h.Set("X-Frame-Options", "SAMEORIGIN")
	ApplySecurityHeaders(h)
	if h.Get("Content-Security-Policy") != "python-set" {
		t.Error("已存在的 CSP 被覆盖")
	}
	if h.Get("X-Frame-Options") != "SAMEORIGIN" {
		t.Error("已存在的 X-Frame-Options 被覆盖")
	}
	if h.Get("Referrer-Policy") != "no-referrer" {
		t.Error("缺失的头应补齐")
	}
}

func TestRespWriterStatusCapture(t *testing.T) {
	rec := httptest.NewRecorder()
	w := NewRespWriter(rec)
	if w.Status() != 200 {
		t.Error("未写出时状态应按 200")
	}
	_, _ = w.Write([]byte("a")) // 隐式 200
	if w.Status() != 200 {
		t.Errorf("隐式 200 记录失败: %d", w.Status())
	}
	w.WriteHeader(500) // 太晚，应被忽略
	if w.Status() != 200 {
		t.Errorf("重复 WriteHeader 不应改写状态: %d", w.Status())
	}

	rec2 := httptest.NewRecorder()
	w2 := NewRespWriter(rec2)
	w2.WriteHeader(418)
	if w2.Status() != 418 {
		t.Errorf("显式状态码记录失败: %d", w2.Status())
	}
}

func TestRespWriterAppliesHeadersBeforeFirstWrite(t *testing.T) {
	rec := httptest.NewRecorder()
	w := NewRespWriter(rec)
	_, _ = w.Write([]byte("x"))
	if got := rec.Header().Get("X-Content-Type-Options"); got != "nosniff" {
		t.Error("安全头应在首次写出前补齐")
	}
	if got := rec.Header().Get("Content-Security-Policy"); got != wantCSP {
		t.Error("CSP 缺失")
	}
}

func TestWriteStringHeadOmitsBody(t *testing.T) {
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodHead, "/api/health", nil)
	WriteString(rec, r, 200, "application/json", `{"status":"ok"}`)
	if rec.Body.Len() != 0 {
		t.Errorf("HEAD 不应有 body: %q", rec.Body.String())
	}
	if rec.Header().Get("Content-Length") != "15" {
		t.Errorf("HEAD 应带 Content-Length: %q", rec.Header().Get("Content-Length"))
	}
}

func TestClientIP(t *testing.T) {
	cases := []struct{ in, want string }{
		{"127.0.0.1:5678", "127.0.0.1"},
		{"[::1]:99", "::1"},
		{"1.2.3.4", "1.2.3.4"},
	}
	for _, c := range cases {
		if got := ClientIP(c.in); got != c.want {
			t.Errorf("ClientIP(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestUnwrapReturnsUnderlying(t *testing.T) {
	rec := httptest.NewRecorder()
	w := NewRespWriter(rec)
	if w.Unwrap() != http.ResponseWriter(rec) {
		t.Error("Unwrap 应返回底层 ResponseWriter（ResponseController 依赖它取 Hijack）")
	}
}
