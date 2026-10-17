package report

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"retail-assistant/edge/internal/httpx"
	"retail-assistant/edge/internal/wsutil"
)

// ==================== 测试脚手架 ====================

// newTestServer 把 Handler 挂到一个真实的 HTTP 服务器上（WS 升级需要 Hijack，
// httptest.ResponseRecorder 不支持）。清理顺序：先关连接再停服务器 ——
// 被 Hijack 的连接不在 http.Server 管理范围内。
func newTestServer(t *testing.T, h *Handler) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rw := httpx.NewRespWriter(w)
		if h.HandleInternal(rw, r) {
			return
		}
		if _, handled := h.Handle(rw, r); handled {
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(func() {
		h.Close()
		srv.Close()
	})
	return srv
}

// wsClient 测试用的原始 WS 客户端（与 video 包同样的理由：要断言的正是字节）。
type wsClient struct {
	t    *testing.T
	conn net.Conn
	br   *bufio.Reader
}

// dialWS 建立一条 WS 连接（要求握手 101）。
func dialWS(t *testing.T, srv *httptest.Server, path string) *wsClient {
	t.Helper()
	status, conn, br := handshake(t, srv, path)
	if status != http.StatusSwitchingProtocols {
		_ = conn.Close()
		t.Fatalf("期望 101，得到 %d", status)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return &wsClient{t: t, conn: conn, br: br}
}

// handshake 发一次 WS 握手并返回状态码（不要求 101）。
func handshake(t *testing.T, srv *httptest.Server, path string) (int, net.Conn, *bufio.Reader) {
	t.Helper()
	u, err := url.Parse(srv.URL)
	if err != nil {
		t.Fatalf("解析 URL 失败: %v", err)
	}
	conn, err := net.Dial("tcp", u.Host)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	key := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{0x42}, 16))
	req := fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"+
		"Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
		path, u.Host, key)
	if _, err := io.WriteString(conn, req); err != nil {
		t.Fatalf("写握手失败: %v", err)
	}
	br := bufio.NewReader(conn)
	statusLine, err := br.ReadString('\n')
	if err != nil {
		t.Fatalf("读握手响应失败: %v", err)
	}
	var status int
	if _, err := fmt.Sscanf(statusLine, "HTTP/1.1 %d", &status); err != nil {
		t.Fatalf("状态行不合法: %q", statusLine)
	}
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatalf("读响应头失败: %v", err)
		}
		if line == "\r\n" || line == "\n" {
			break
		}
	}
	return status, conn, br
}

// readFrame 读一帧（服务端下发的帧不掩码）。
func (c *wsClient) readFrame(timeout time.Duration) (byte, []byte, error) {
	_ = c.conn.SetReadDeadline(time.Now().Add(timeout))
	var head [2]byte
	if _, err := io.ReadFull(c.br, head[:]); err != nil {
		return 0, nil, err
	}
	opcode := head[0] & 0x0F
	n := int64(head[1] & 0x7F)
	switch n {
	case 126:
		var ext [2]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			return 0, nil, err
		}
		n = int64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			return 0, nil, err
		}
		n = int64(binary.BigEndian.Uint64(ext[:]))
	}
	payload := make([]byte, n)
	if _, err := io.ReadFull(c.br, payload); err != nil {
		return 0, nil, err
	}
	return opcode, payload, nil
}

func (c *wsClient) readText(timeout time.Duration) string {
	c.t.Helper()
	op, payload, err := c.readFrame(timeout)
	if err != nil {
		c.t.Fatalf("读消息失败: %v", err)
	}
	if op != wsutil.TextMessage {
		c.t.Fatalf("期望文本帧，得到 opcode=%d", op)
	}
	return string(payload)
}

func waitFor(t *testing.T, what string, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("等待超时: %s", what)
}

// postBroadcast 调内网广播入口。
func postBroadcast(t *testing.T, srv *httptest.Server, token, body string) (*http.Response, string) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, srv.URL+internalPath, strings.NewReader(body))
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp, string(b)
}

// ==================== 用例 ====================

func TestDisabledDoesNotTakeOver(t *testing.T) {
	h := New(Options{Enabled: false, InternalToken: "tok"})
	if h.Enabled() {
		t.Fatal("EDGE_REPORT=0 时不应启用")
	}
	srv := newTestServer(t, h)

	status, conn, _ := handshake(t, srv, pathReports)
	_ = conn.Close()
	if status != http.StatusNotFound {
		t.Fatalf("未启用时 WS 应落回普通处理（404），得到 %d", status)
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, internalPath, strings.NewReader(`{"type":"report"}`))
	req.RemoteAddr = "127.0.0.1:1234"
	req.Header.Set("X-Internal-Token", "tok")
	if h.HandleInternal(httpx.NewRespWriter(rec), req) {
		t.Fatal("未启用时不应处理内网广播入口")
	}
}

func TestMissingTokenDisablesOwnership(t *testing.T) {
	h := New(Options{Enabled: true, InternalToken: ""})
	if h.Enabled() {
		t.Fatal("密钥为空时必须禁用（广播入口无从校验）")
	}
}

func TestInternalGate(t *testing.T) {
	h := New(Options{Enabled: true, InternalToken: "secret"})
	validBody := `{"type":"report","summary":"hi"}`

	cases := []struct {
		name   string
		method string
		peer   string
		token  string
		body   string
		want   int
	}{
		{"正确密钥与内网对端", http.MethodPost, "127.0.0.1:1", "secret", validBody, http.StatusOK},
		{"错误密钥", http.MethodPost, "127.0.0.1:1", "wrong", validBody, http.StatusNotFound},
		{"缺少密钥", http.MethodPost, "127.0.0.1:1", "", validBody, http.StatusNotFound},
		{"公网对端", http.MethodPost, "203.0.113.9:1", "secret", validBody, http.StatusNotFound},
		{"错误方法", http.MethodGet, "127.0.0.1:1", "secret", "", http.StatusNotFound},
		{"非法 JSON", http.MethodPost, "127.0.0.1:1", "secret", `not-json`, http.StatusBadRequest},
		{"JSON 不是对象", http.MethodPost, "127.0.0.1:1", "secret", `[1,2]`, http.StatusBadRequest},
		{"JSON null", http.MethodPost, "127.0.0.1:1", "secret", `null`, http.StatusBadRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(tc.method, internalPath, strings.NewReader(tc.body))
			req.RemoteAddr = tc.peer
			req.Header.Set("X-Internal-Token", tc.token)
			handled := h.HandleInternal(httpx.NewRespWriter(rec), req)
			if !handled {
				t.Fatalf("路径匹配时必须由 HandleInternal 处理")
			}
			if rec.Code != tc.want {
				t.Fatalf("期望 %d，得到 %d（body=%s）", tc.want, rec.Code, rec.Body.String())
			}
		})
	}

	// 其它 /internal/** 路径不归它管（继续走通用 404）。
	req := httptest.NewRequest(http.MethodPost, "/internal/auth/verify", nil)
	if h.HandleInternal(httpx.NewRespWriter(httptest.NewRecorder()), req) {
		t.Fatal("非汇报路径不应被 HandleInternal 接管")
	}
}

func TestWebSocketFanout(t *testing.T) {
	h := New(Options{Enabled: true, InternalToken: "secret"})
	srv := newTestServer(t, h)

	a := dialWS(t, srv, pathReports)
	b := dialWS(t, srv, pathReportsLegacy) // 兼容别名同样接管
	waitFor(t, "两条订阅连接登记完成", func() bool { return h.hub.Count() == 2 })

	payload := `{"type":"report","summary":"1号货架热度上升","data":{"zone":"A"},"ts":"2026-10-17T12:00:00"}`
	resp, body := postBroadcast(t, srv, "secret", payload)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("广播失败: %d %s", resp.StatusCode, body)
	}
	if !strings.Contains(body, `"delivered":2`) {
		t.Fatalf("期望投递 2 条连接，响应: %s", body)
	}

	if got := a.readText(2 * time.Second); got != payload {
		t.Fatalf("订阅者 A 收到的字节不一致:\n got=%s\nwant=%s", got, payload)
	}
	if got := b.readText(2 * time.Second); got != payload {
		t.Fatalf("订阅者 B（兼容别名）收到的字节不一致:\n got=%s\nwant=%s", got, payload)
	}

	// /metrics 的 report 段计数。
	snap := h.stats.Snapshot().(reportSnapshot)
	if snap.Enabled != true || snap.Clients != 2 || snap.Broadcasts != 1 || snap.Delivered != 2 {
		t.Fatalf("快照计数不符: %+v", snap)
	}
}

func TestCloseDisconnectsSubscribers(t *testing.T) {
	h := New(Options{Enabled: true, InternalToken: "secret"})
	srv := newTestServer(t, h)
	c := dialWS(t, srv, pathReports)
	waitFor(t, "订阅连接登记完成", func() bool { return h.hub.Count() == 1 })

	h.Close()

	op, payload, err := c.readFrame(2 * time.Second)
	if err == nil && op != wsutil.CloseMessage {
		t.Fatalf("期望 close 帧，得到 opcode=%d payload=%q", op, payload)
	}
	waitFor(t, "连接表清空", func() bool { return h.hub.Count() == 0 })
}

func TestClientDisconnectRemovesFromHub(t *testing.T) {
	h := New(Options{Enabled: true, InternalToken: "secret"})
	srv := newTestServer(t, h)
	c := dialWS(t, srv, pathReports)
	waitFor(t, "订阅连接登记完成", func() bool { return h.hub.Count() == 1 })

	_ = c.conn.Close()
	waitFor(t, "断开后连接表清空", func() bool { return h.hub.Count() == 0 })
}

func TestOfferDropsOldestWhenQueueFull(t *testing.T) {
	stats := newStats(true)
	c := newClient(nil, stats)
	for i := 0; i < maxPendingReports+3; i++ {
		c.offer([]byte{byte(i)})
	}
	if got := len(c.queue); got != maxPendingReports {
		t.Fatalf("队列长度应为 %d，得到 %d", maxPendingReports, got)
	}
	for i := 0; i < maxPendingReports; i++ {
		want := byte(i + 3) // 前 3 条被丢
		if got := c.take()[0]; got != want {
			t.Fatalf("第 %d 条应为 %d，得到 %d", i, want, got)
		}
	}
	if snap := stats.Snapshot().(reportSnapshot); snap.Dropped != 3 {
		t.Fatalf("期望丢弃 3 条，得到 %d", snap.Dropped)
	}
}
