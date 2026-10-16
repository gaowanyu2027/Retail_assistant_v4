package health

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"retail-assistant/edge/internal/httpx"
)

func serveLiveness(t *testing.T, method string, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(method, "/api/health", nil)
	for k, v := range headers {
		r.Header.Set(k, v)
	}
	ServeLiveness(httpx.NewRespWriter(rec), r)
	return rec
}

func TestLivenessExactBody(t *testing.T) {
	rec := serveLiveness(t, http.MethodGet, nil)
	if rec.Code != 200 {
		t.Fatalf("status = %d", rec.Code)
	}
	if rec.Body.String() != `{"status":"ok"}` {
		t.Errorf("body = %q（必须与 FastAPI 逐字一致）", rec.Body.String())
	}
	if rec.Header().Get("Content-Type") != "application/json" {
		t.Errorf("Content-Type = %q", rec.Header().Get("Content-Type"))
	}
	if rec.Header().Get("Content-Length") != "15" {
		t.Errorf("Content-Length = %q", rec.Header().Get("Content-Length"))
	}
	if rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Error("安全头缺失")
	}
}

func TestLivenessHead(t *testing.T) {
	rec := serveLiveness(t, http.MethodHead, nil)
	if rec.Code != 200 || rec.Body.Len() != 0 {
		t.Errorf("HEAD: code=%d len=%d", rec.Code, rec.Body.Len())
	}
}

func TestLivenessMethodNotAllowed(t *testing.T) {
	rec := serveLiveness(t, http.MethodPost, nil)
	if rec.Code != 405 || rec.Header().Get("Allow") != "GET, HEAD" {
		t.Errorf("POST: code=%d allow=%q", rec.Code, rec.Header().Get("Allow"))
	}
}

func TestHasCredentials(t *testing.T) {
	cases := []struct {
		name    string
		headers map[string]string
		query   string
		want    bool
	}{
		{"匿名", nil, "", false},
		{"会话 Cookie", map[string]string{"Cookie": "retail_sid=abc"}, "", true},
		{"无关 Cookie", map[string]string{"Cookie": "theme=dark"}, "", false},
		{"空值 Cookie", map[string]string{"Cookie": "retail_sid="}, "", false},
		{"混合 Cookie", map[string]string{"Cookie": "a=1; retail_sid=t; b=2"}, "", true},
		{"Bearer", map[string]string{"Authorization": "Bearer tok"}, "", true},
		{"ticket", nil, "?ticket=t1", true},
	}
	for _, c := range cases {
		r := httptest.NewRequest(http.MethodGet, "/api/health"+c.query, nil)
		for k, v := range c.headers {
			r.Header.Set(k, v)
		}
		if got := HasCredentials(r); got != c.want {
			t.Errorf("%s: HasCredentials = %v, want %v", c.name, got, c.want)
		}
	}
}
