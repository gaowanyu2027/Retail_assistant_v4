package proxy

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"retail-assistant/edge/internal/httpx"
)

// newFront 起一个"只有反代"的前端（带 RespWriter，贴近真实链路）。
func newFront(t *testing.T, backendURL string, onErr func()) *httptest.Server {
	t.Helper()
	px, err := New(backendURL, onErr)
	if err != nil {
		t.Fatal(err)
	}
	front := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		px.ServeHTTP(httpx.NewRespWriter(w), r)
	}))
	t.Cleanup(front.Close)
	return front
}

func TestProxyStripsSpoofedForwardHeaders(t *testing.T) {
	var got http.Header
	var gotHost string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		gotHost = r.Host
		w.Header().Set("X-Backend", "1")
		_, _ = io.WriteString(w, "hello")
	}))
	defer backend.Close()
	front := newFront(t, backend.URL, nil)

	req, _ := http.NewRequest(http.MethodGet, front.URL+"/api/queries", nil)
	// 客户端伪造的转发/内部头必须全部被剥离
	req.Header.Set("X-Forwarded-For", "6.6.6.6")
	req.Header.Set("X-Forwarded-Proto", "https")
	req.Header.Set("X-Real-IP", "6.6.6.7")
	req.Header.Set("X-Internal-Token", "spoof")
	req.Header.Set("Cookie", "retail_sid=tok")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if string(body) != "hello" {
		t.Errorf("body = %q", body)
	}
	if resp.Header.Get("X-Backend") != "1" {
		t.Error("响应头未透传")
	}
	if resp.Header.Get("X-Content-Type-Options") != "nosniff" {
		t.Error("边缘未补安全头")
	}
	if got.Get("X-Forwarded-For") != "127.0.0.1" {
		t.Errorf("XFF = %q（应只含直连对端 IP）", got.Get("X-Forwarded-For"))
	}
	if got.Get("X-Real-IP") != "127.0.0.1" {
		t.Errorf("X-Real-IP = %q", got.Get("X-Real-IP"))
	}
	if got.Get("X-Forwarded-Proto") != "http" {
		t.Errorf("XFP = %q（不采信客户端伪造值）", got.Get("X-Forwarded-Proto"))
	}
	if got.Get("X-Internal-Token") != "" {
		t.Error("X-Internal-* 应被剥离")
	}
	if got.Get("Cookie") != "retail_sid=tok" {
		t.Error("Cookie 应原样透传")
	}
	u, _ := url.Parse(front.URL)
	if gotHost != u.Host {
		t.Errorf("Host = %q, want %q（应保持客户端看到的 Host）", gotHost, u.Host)
	}
}

func TestProxy502WhenBackendDown(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	backendURL := backend.URL
	backend.Close() // 立即关闭 → 连接拒绝

	var errCount atomic.Int64
	front := newFront(t, backendURL, func() { errCount.Add(1) })

	resp, err := http.Get(front.URL + "/api/x")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 502 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if string(body) != `{"detail":"Bad Gateway"}` {
		t.Errorf("body = %q", body)
	}
	if resp.Header.Get("Content-Type") != "application/json" {
		t.Errorf("Content-Type = %q", resp.Header.Get("Content-Type"))
	}
	if errCount.Load() != 1 {
		t.Errorf("onError 调用 %d 次, want 1", errCount.Load())
	}
}

func TestProxyStreamsSSEImmediately(t *testing.T) {
	release := make(chan struct{})
	var once sync.Once
	doRelease := func() { once.Do(func() { close(release) }) }
	defer doRelease()

	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		fl, _ := w.(http.Flusher)
		_, _ = io.WriteString(w, "data: 1\n\n")
		fl.Flush()
		<-release
		_, _ = io.WriteString(w, "data: 2\n\n")
		fl.Flush()
	}))
	defer backend.Close()
	front := newFront(t, backend.URL, nil)

	resp, err := http.Get(front.URL + "/api/queries/stream")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	br := bufio.NewReader(resp.Body)

	type readResult struct {
		line string
		err  error
	}
	first := make(chan readResult, 1)
	go func() {
		l, err := br.ReadString('\n')
		first <- readResult{l, err}
	}()
	select {
	case res := <-first:
		if res.err != nil {
			t.Fatal(res.err)
		}
		if res.line != "data: 1\n" {
			t.Fatalf("首块 = %q", res.line)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("首块未及时到达（FlushInterval=-1 失效？）")
	}

	doRelease() // 放行后端第二块
	rest, _ := io.ReadAll(br)
	if !strings.Contains(string(rest), "data: 2") {
		t.Errorf("第二块缺失: %q", rest)
	}
}

// TestProxyWebSocketUpgradePassthrough 端到端验证 WS 升级与双向字节透传：
// 裸 TCP 客户端 → edge（RespWriter + ReverseProxy）→ 后端（Hijack 后回显）。
func TestProxyWebSocketUpgradePassthrough(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		hj, ok := w.(http.Hijacker)
		if !ok {
			t.Error("后端不支持 Hijack")
			return
		}
		conn, buf, err := hj.Hijack()
		if err != nil {
			t.Errorf("hijack: %v", err)
			return
		}
		defer conn.Close()
		_, _ = buf.WriteString("HTTP/1.1 101 Switching Protocols\r\n" +
			"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
		_ = buf.Flush()
		_, _ = io.Copy(conn, conn) // 回显
	}))
	defer backend.Close()
	front := newFront(t, backend.URL, nil)

	u, err := url.Parse(front.URL)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := net.Dial("tcp", u.Host)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))

	fmt.Fprintf(conn, "GET /api/ws/stream HTTP/1.1\r\nHost: %s\r\n"+
		"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n", u.Host)

	br := bufio.NewReader(conn)
	status, err := br.ReadString('\n')
	if err != nil {
		t.Fatalf("读状态行: %v", err)
	}
	if !strings.Contains(status, "101") {
		t.Fatalf("状态行 = %q（WS 升级未透传）", status)
	}
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatalf("读头部: %v", err)
		}
		if line == "\r\n" {
			break
		}
	}
	if _, err := conn.Write([]byte("ping")); err != nil {
		t.Fatal(err)
	}
	echo := make([]byte, 4)
	if _, err := io.ReadFull(br, echo); err != nil {
		t.Fatalf("回声读取失败: %v", err)
	}
	if string(echo) != "ping" {
		t.Fatalf("回声 = %q", echo)
	}
}
