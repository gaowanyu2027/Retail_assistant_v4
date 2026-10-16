package metrics

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"retail-assistant/edge/internal/httpx"
	"retail-assistant/edge/internal/routing"
)

func serveMetrics(m *Metrics, method, remoteAddr string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(method, "/metrics", nil)
	r.RemoteAddr = remoteAddr
	m.Serve(httpx.NewRespWriter(rec), r)
	return rec
}

func TestMetricsPrivateOnly(t *testing.T) {
	m := New("http://backend:8000")
	for _, addr := range []string{"127.0.0.1:1111", "192.168.1.5:1", "10.0.0.1:1",
		"172.17.0.1:1", "[::1]:9"} {
		if rec := serveMetrics(m, http.MethodGet, addr); rec.Code != 200 {
			t.Errorf("%s 应可读 /metrics，得到 %d", addr, rec.Code)
		}
	}
	for _, addr := range []string{"8.8.8.8:5", "203.0.113.7:9", "example.com:80", ""} {
		rec := serveMetrics(m, http.MethodGet, addr)
		if rec.Code != 404 {
			t.Errorf("%s 应 404（对外不暴露该路径），得到 %d", addr, rec.Code)
		}
		if rec.Body.String() != `{"detail":"Not Found"}` {
			t.Errorf("%s body = %q", addr, rec.Body.String())
		}
	}
}

func TestMetricsSnapshot(t *testing.T) {
	m := New("http://backend:8000")
	m.IncKind(routing.Index)
	m.IncKind(routing.Index)
	m.IncKind(routing.Proxy)
	m.IncStatus(200)
	m.IncStatus(404)
	m.IncStatus(502)
	m.IncProxyError()
	m.IncInflight()

	rec := serveMetrics(m, http.MethodGet, "127.0.0.1:1")
	if rec.Code != 200 {
		t.Fatalf("status = %d", rec.Code)
	}
	var snap snapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("JSON 解析失败: %v", err)
	}
	if snap.Requests["index"] != 2 || snap.Requests["proxy"] != 1 {
		t.Errorf("requests = %v", snap.Requests)
	}
	if snap.Responses["2xx"] != 1 || snap.Responses["4xx"] != 1 || snap.Responses["5xx"] != 1 {
		t.Errorf("responses = %v", snap.Responses)
	}
	if snap.ProxyErrors != 1 || snap.Inflight != 1 {
		t.Errorf("proxy_errors=%d inflight=%d", snap.ProxyErrors, snap.Inflight)
	}
	if snap.BackendURL != "http://backend:8000" {
		t.Errorf("backend_url = %q", snap.BackendURL)
	}
}

func TestMetricsMethodNotAllowed(t *testing.T) {
	rec := serveMetrics(New("http://b:8000"), http.MethodPost, "127.0.0.1:1")
	if rec.Code != 405 {
		t.Errorf("status = %d", rec.Code)
	}
}
