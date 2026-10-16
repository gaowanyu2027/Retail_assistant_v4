package auth

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"retail-assistant/edge/internal/httpx"
)

func newGate(t *testing.T, backendURL string) *Gate {
	t.Helper()
	return NewGate(NewVerifier(backendURL, "k", Observer{}), true)
}

func req(method, target string, hdr map[string]string) *http.Request {
	r := httptest.NewRequest(method, target, nil)
	for k, v := range hdr {
		if v == "" {
			r.Header.Del(k)
			continue
		}
		r.Header.Set(k, v)
	}
	return r
}

func TestGateInspect(t *testing.T) {
	_, srv := verdictBackend(t)
	g := newGate(t, srv.URL)

	cases := []struct {
		name   string
		r      *http.Request
		want   Decision
		reason string
	}{
		{"公开路径放行", req("POST", "/api/auth/login", nil), Allow, "public"},
		{"静态资源不经闸门", req("GET", "/assets/x.js", nil), Allow, "public"},
		{"WS 升级放行（1008 语义在 Python）", req("GET", "/api/stream", map[string]string{
			"Connection": "Upgrade", "Upgrade": "websocket"}), Allow, "websocket"},
		{"票据由 Python 消费", req("GET", "/api/stream?ticket=t1", nil), Allow, "query-credential"},
		{"URL 令牌交 Python 判", req("GET", "/api/queries?token=t1", nil), Allow, "query-credential"},
		{"无凭据直接拒", req("GET", "/api/queries", nil), Deny, "no-credential"},
		{"有效 Bearer 放行", req("GET", "/api/queries", map[string]string{
			"Authorization": "Bearer good"}), Allow, "valid-bearer"},
		{"有效 Cookie 放行", req("GET", "/api/queries", map[string]string{
			"Cookie": "retail_sid=good"}), Allow, "valid-cookie"},
		{"无效 Bearer 拒", req("GET", "/api/queries", map[string]string{
			"Authorization": "Bearer bad"}), Deny, "invalid-credential"},
		{"无效 Cookie 拒", req("GET", "/api/queries", map[string]string{
			"Cookie": "retail_sid=bad"}), Deny, "invalid-credential"},
		// 台账 B13：浏览器里别的应用留下的垃圾 Cookie 不该把有效 Bearer 挡掉
		{"垃圾 Cookie + 有效 Bearer 放行", req("GET", "/api/queries", map[string]string{
			"Cookie": "retail_sid=bad", "Authorization": "Bearer good"}), Allow, "valid-bearer"},
		{"有效 Cookie + 无效 Bearer：任一有效即放行", req("GET", "/api/queries", map[string]string{
			"Cookie": "retail_sid=good", "Authorization": "Bearer bad"}), Allow, "valid-cookie"},
	}
	for _, c := range cases {
		got, reason := g.Inspect(c.r)
		if got != c.want || reason != c.reason {
			t.Errorf("%s：(%v,%q)，期望 (%v,%q)", c.name, got, reason, c.want, c.reason)
		}
	}
}

func TestGateFailsOpenWhenVerifyUnavailable(t *testing.T) {
	// 回源不可用（这里用悬空端口模拟 Python 内部端点不在）：带凭据的请求一律放行。
	g := newGate(t, "http://127.0.0.1:1")
	got, reason := g.Inspect(req("GET", "/api/queries", map[string]string{
		"Authorization": "Bearer whatever"}))
	if got != Allow || reason != "verify-unknown" {
		t.Errorf("回源不可用时应放行交 Python：(%v,%q)", got, reason)
	}
	// 但"完全没凭据"这类零风险判定不受回源影响，仍然拒。
	if got, reason := g.Inspect(req("GET", "/api/queries", nil)); got != Deny || reason != "no-credential" {
		t.Errorf("无凭据仍应拒：(%v,%q)", got, reason)
	}
}

func TestGateDisabled(t *testing.T) {
	g := NewGate(NewVerifier("http://127.0.0.1:1", "k", Observer{}), false)
	for _, r := range []*http.Request{
		req("GET", "/api/queries", nil),
		req("GET", "/api/queries", map[string]string{"Cookie": "retail_sid=bad"}),
	} {
		if got, reason := g.Inspect(r); got != Allow || reason != "disabled" {
			t.Errorf("禁用时一切放行：(%v,%q)", got, reason)
		}
	}
	// 显式禁用优先于"verifier 为 nil"的接线错误
	if NewGate(nil, true).Enabled() {
		t.Error("verifier 为 nil 时应自动禁用")
	}
}

// TestDeniedResponseMatchesPythonShape 早拒响应必须与 Python 的未登录响应逐字一致
// （安全头由 RespWriter 补齐，与直连 Python 一致）。
func TestDeniedResponseMatchesPythonShape(t *testing.T) {
	w := httptest.NewRecorder()
	rw := httpx.NewRespWriter(w)
	authReq := req("GET", "/api/queries", nil)
	ServeDenied(rw, authReq)

	if w.Code != UnauthorizedStatus {
		t.Errorf("状态码 = %d，期望 %d", w.Code, UnauthorizedStatus)
	}
	if ct := w.Header().Get("Content-Type"); ct != UnauthorizedCT {
		t.Errorf("Content-Type = %q，期望 %q", ct, UnauthorizedCT)
	}
	if body := w.Body.String(); body != UnauthorizedBody {
		t.Errorf("响应体 = %q，期望 %q", body, UnauthorizedBody)
	}
	// 安全头与直连 Python 时同形
	if w.Header().Get("X-Content-Type-Options") != "nosniff" ||
		w.Header().Get("X-Frame-Options") != "DENY" ||
		!strings.Contains(w.Header().Get("Content-Security-Policy"), "frame-ancestors 'none'") {
		t.Errorf("安全头缺失：%v", w.Header())
	}
	// HEAD 请求只回头部（httpx.WriteString 的既有语义）
	w2 := httptest.NewRecorder()
	ServeDenied(httpx.NewRespWriter(w2), req("HEAD", "/api/queries", nil))
	if w2.Body.Len() != 0 {
		t.Errorf("HEAD 不应有响应体，实得 %q", w2.Body.String())
	}
}

func TestServeInternalNotFound(t *testing.T) {
	w := httptest.NewRecorder()
	ServeInternalNotFound(httpx.NewRespWriter(w), req("POST", "/internal/auth/verify", nil))
	if w.Code != NotFoundStatus || w.Body.String() != NotFoundBody ||
		w.Header().Get("Content-Type") != NotFoundCT {
		t.Errorf("内部路径本地 404 形状不符：%d %q %q",
			w.Code, w.Header().Get("Content-Type"), w.Body.String())
	}
}
