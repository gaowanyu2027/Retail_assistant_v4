package static

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"retail-assistant/edge/internal/httpx"
)

// newDist 构造一个"构建完整"的最小 dist 目录。
func newDist(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, sub := range []string{"js", "css", "assets"} {
		if err := os.MkdirAll(filepath.Join(dir, sub), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	files := map[string]string{
		"index.html": `<html><head><link rel="stylesheet" href="/assets/index-x.css">` +
			`<link rel="icon" href="data:image/svg+xml,<svg/>"></head>` +
			`<body><div id="app"></div><script type="module" src="/assets/index-x.js"></script></body></html>`,
		"js/vue-fallback.js": "// fallback",
		"css/dashboard.css":  "body{}",
		"assets/index-x.js":  "console.log(1)",
		"assets/index-x.css": "x{}",
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, filepath.FromSlash(name)), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// serve 走完整包装（含 RespWriter 的安全头），贴近真实链路。
func serve(h *Handler, method, target string, index bool) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(method, target, nil)
	w := httpx.NewRespWriter(rec)
	if index {
		h.ServeIndex(w, r)
	} else {
		h.ServeStatic(w, r)
	}
	return rec
}

func TestIndexInjectsFallbackScript(t *testing.T) {
	h := New(newDist(t))
	rec := serve(h, http.MethodGet, "/", true)
	if rec.Code != 200 {
		t.Fatalf("status = %d", rec.Code)
	}
	body := rec.Body.String()
	if !strings.Contains(body, `<script src="/js/vue-fallback.js"></script></body>`) {
		t.Errorf("未在 </body> 前注入脚本: %s", body)
	}
	// 注入脚本含 src 引用，但引用的 /js/vue-fallback.js 存在时不应触发"完整性复查失败"
	if strings.Contains(body, "前端未构建") {
		t.Error("构建完整时不应显示提示页")
	}
	hd := rec.Header()
	if hd.Get("Cache-Control") != "no-store, no-cache, must-revalidate, max-age=0" {
		t.Errorf("Cache-Control = %q", hd.Get("Cache-Control"))
	}
	if hd.Get("Pragma") != "no-cache" || hd.Get("Expires") != "0" {
		t.Error("NO_CACHE_HEADERS 不完整")
	}
	if hd.Get("Content-Type") != "text/html; charset=utf-8" {
		t.Errorf("Content-Type = %q", hd.Get("Content-Type"))
	}
	if hd.Get("X-Content-Type-Options") != "nosniff" {
		t.Error("安全头缺失（RespWriter 应补齐）")
	}
}

func TestIndexInjectionOnlyFirstBody(t *testing.T) {
	dir := newDist(t)
	os.WriteFile(filepath.Join(dir, "index.html"),
		[]byte("A</body>B</body>"), 0o644)
	rec := serve(New(dir), http.MethodGet, "/", true)
	if got := strings.Count(rec.Body.String(), VueFallbackScript); got != 1 {
		t.Errorf("注入次数 = %d, want 1", got)
	}
}

func TestIndexUnbuiltWhenMissing(t *testing.T) {
	rec := serve(New(t.TempDir()), http.MethodGet, "/", true)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "前端未构建") {
		t.Errorf("缺失时应显示提示页: code=%d body=%q", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Cache-Control") != "no-store, no-cache, must-revalidate, max-age=0" {
		t.Error("提示页也带 NO_CACHE_HEADERS")
	}
}

func TestIndexUnbuiltWhenRefMissing(t *testing.T) {
	dir := newDist(t)
	os.Remove(filepath.Join(dir, "assets", "index-x.js")) // 引用断裂 → 复查失败
	rec := serve(New(dir), http.MethodGet, "/", true)
	if !strings.Contains(rec.Body.String(), "前端未构建") {
		t.Error("引用缺失时应显示提示页")
	}
}

func TestIndexMethodNotAllowed(t *testing.T) {
	rec := serve(New(newDist(t)), http.MethodPost, "/", true)
	if rec.Code != 405 {
		t.Fatalf("status = %d", rec.Code)
	}
	if rec.Header().Get("Allow") != "GET, HEAD" {
		t.Errorf("Allow = %q", rec.Header().Get("Allow"))
	}
	if rec.Body.String() != methodNotAllowedBody {
		t.Errorf("body = %q", rec.Body.String())
	}
}

func TestIndexHeadNoBody(t *testing.T) {
	rec := serve(New(newDist(t)), http.MethodHead, "/", true)
	if rec.Code != 200 || rec.Body.Len() != 0 {
		t.Errorf("HEAD 应 200 且无 body: code=%d len=%d", rec.Code, rec.Body.Len())
	}
	if rec.Header().Get("Content-Length") == "" {
		t.Error("HEAD 应带 Content-Length")
	}
}

func TestStaticJSNoCache(t *testing.T) {
	rec := serve(New(newDist(t)), http.MethodGet, "/js/vue-fallback.js", false)
	if rec.Code != 200 {
		t.Fatalf("status = %d", rec.Code)
	}
	if rec.Header().Get("Cache-Control") != "no-store, no-cache, must-revalidate" {
		t.Errorf("Cache-Control = %q", rec.Header().Get("Cache-Control"))
	}
	if rec.Header().Get("Content-Type") != "text/javascript; charset=utf-8" {
		t.Errorf("Content-Type = %q（Starlette 会给 text/* 补 charset）", rec.Header().Get("Content-Type"))
	}
	if rec.Body.String() != "// fallback" {
		t.Errorf("body = %q", rec.Body.String())
	}
}

func TestStaticCSSNoCacheAndType(t *testing.T) {
	rec := serve(New(newDist(t)), http.MethodGet, "/css/dashboard.css", false)
	if rec.Header().Get("Cache-Control") != "no-store, no-cache, must-revalidate" {
		t.Errorf("Cache-Control = %q", rec.Header().Get("Cache-Control"))
	}
	if rec.Header().Get("Content-Type") != "text/css; charset=utf-8" {
		t.Errorf("Content-Type = %q", rec.Header().Get("Content-Type"))
	}
}

func TestStaticAssetsNoCacheControl(t *testing.T) {
	rec := serve(New(newDist(t)), http.MethodGet, "/assets/index-x.js", false)
	if rec.Code != 200 {
		t.Fatalf("status = %d", rec.Code)
	}
	if cc := rec.Header().Get("Cache-Control"); cc != "" {
		t.Errorf("/assets 不应设 Cache-Control，得到 %q", cc)
	}
	if rec.Header().Get("Content-Type") != "text/javascript; charset=utf-8" {
		t.Errorf("Content-Type = %q", rec.Header().Get("Content-Type"))
	}
}

func TestWithCharset(t *testing.T) {
	cases := []struct{ in, want string }{
		{"text/javascript", "text/javascript; charset=utf-8"},
		{"text/css", "text/css; charset=utf-8"},
		{"text/plain", "text/plain; charset=utf-8"},
		{"application/json", "application/json"},
		{"image/svg+xml", "image/svg+xml"},
		{"text/html; charset=gbk", "text/html; charset=gbk"},   // 已有 charset 不重复补
		{"text/html; CHARSET=UTF-8", "text/html; CHARSET=UTF-8"}, // 大小写不敏感
	}
	for _, c := range cases {
		if got := withCharset(c.in); got != c.want {
			t.Errorf("withCharset(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestStatic404Shapes(t *testing.T) {
	h := New(newDist(t))
	for _, p := range []string{"/js/nope.js", "/js/", "/js/../assets/index-x.js", "/css/a/../b.css"} {
		rec := serve(h, http.MethodGet, p, false)
		if rec.Code != 404 || rec.Body.String() != notFoundBody {
			t.Errorf("%s: code=%d body=%q", p, rec.Code, rec.Body.String())
		}
		if rec.Header().Get("Content-Type") != "application/json" {
			t.Errorf("%s: Content-Type = %q", p, rec.Header().Get("Content-Type"))
		}
	}
}

func TestStaticMethodNotAllowedHasNoAllowHeader(t *testing.T) {
	rec := serve(New(newDist(t)), http.MethodPost, "/js/vue-fallback.js", false)
	if rec.Code != 405 {
		t.Fatalf("status = %d", rec.Code)
	}
	if rec.Header().Get("Allow") != "" {
		t.Errorf("StaticFiles 的 405 不带 Allow，得到 %q", rec.Header().Get("Allow"))
	}
}

func TestStaticDirectoryRejected(t *testing.T) {
	rec := serve(New(newDist(t)), http.MethodGet, "/js/", false)
	if rec.Code != 404 {
		t.Errorf("目录应 404，得到 %d", rec.Code)
	}
}
