package wsutil

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// ==================== 测试用原始客户端 ====================
//
// 刻意自己实现最简客户端（而不是引库）：要断言的正是**字节** ——
// 掩码、长度扩展、控制帧、关闭码，用库会被抽象挡在门外。

type testClient struct {
	t    *testing.T
	conn net.Conn
	br   *bufio.Reader
}

// wsServer 起一个只做握手、把 *Conn 交给 handle 的测试服务，返回 ws:// 地址。
func wsServer(t *testing.T, handle func(c *Conn)) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := Upgrade(w, r)
		if err != nil {
			t.Errorf("Upgrade 失败: %v", err)
			return
		}
		defer c.Close()
		handle(c)
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http")
}

func dialWS(t *testing.T, wsURL string) *testClient {
	t.Helper()
	u, err := url.Parse(wsURL)
	if err != nil {
		t.Fatalf("解析 URL 失败: %v", err)
	}
	conn, err := net.Dial("tcp", u.Host)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	if u.Path == "" {
		u.Path = "/"
	}
	key := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{0x37}, 16))
	req := "GET " + u.Path + " HTTP/1.1\r\n" +
		"Host: " + u.Host + "\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Key: " + key + "\r\n" +
		"Sec-WebSocket-Version: 13\r\n\r\n"
	if _, err := io.WriteString(conn, req); err != nil {
		t.Fatalf("写握手失败: %v", err)
	}
	br := bufio.NewReader(conn)
	status, err := br.ReadString('\n')
	if err != nil {
		t.Fatalf("读握手响应失败: %v", err)
	}
	if !strings.Contains(status, "101") {
		t.Fatalf("期望 101，得到 %q", strings.TrimSpace(status))
	}
	want := acceptKey(key)
	var gotAccept string
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatalf("读响应头失败: %v", err)
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		if name, value, ok := strings.Cut(line, ":"); ok &&
			strings.EqualFold(strings.TrimSpace(name), "Sec-WebSocket-Accept") {
			gotAccept = strings.TrimSpace(value)
		}
	}
	if gotAccept != want {
		t.Fatalf("Sec-WebSocket-Accept 不符：期望 %q 实际 %q", want, gotAccept)
	}
	return &testClient{t: t, conn: conn, br: br}
}

// writeRaw 原样写字节（构造畸形帧用：只写服务端**会读到**的那部分，
// 不留下未读字节 —— 服务端 close 时若接收缓冲还有数据会发 RST，
// 把刚写出的 close 帧一起丢掉，测试就会随机失败）。
func (c *testClient) writeRaw(b ...byte) {
	c.t.Helper()
	if _, err := c.conn.Write(b); err != nil {
		c.t.Fatalf("写原始字节失败: %v", err)
	}
}

// writeFrame 客户端→服务端：默认加掩码（masked=false 是违规用例）。
func (c *testClient) writeFrame(fin bool, opcode byte, payload []byte, masked bool) {
	c.t.Helper()
	var buf []byte
	b0 := opcode
	if fin {
		b0 |= 0x80
	}
	buf = append(buf, b0)
	n := len(payload)
	maskBit := byte(0)
	if masked {
		maskBit = 0x80
	}
	switch {
	case n < 126:
		buf = append(buf, maskBit|byte(n))
	case n <= 0xFFFF:
		buf = append(buf, maskBit|126)
		buf = binary.BigEndian.AppendUint16(buf, uint16(n))
	default:
		buf = append(buf, maskBit|127)
		buf = binary.BigEndian.AppendUint64(buf, uint64(n))
	}
	if masked {
		key := []byte{0xA1, 0xB2, 0xC3, 0xD4}
		buf = append(buf, key...)
		for i, b := range payload {
			buf = append(buf, b^key[i&3])
		}
	} else {
		buf = append(buf, payload...)
	}
	if _, err := c.conn.Write(buf); err != nil {
		c.t.Fatalf("写帧失败: %v", err)
	}
}

// readFrame 读服务端下发的帧（服务端不加掩码）。
func (c *testClient) readFrame(timeout time.Duration) (opcode byte, payload []byte) {
	c.t.Helper()
	_ = c.conn.SetReadDeadline(time.Now().Add(timeout))
	var hdr [2]byte
	if _, err := io.ReadFull(c.br, hdr[:]); err != nil {
		c.t.Fatalf("读帧头失败: %v", err)
	}
	if hdr[1]&0x80 != 0 {
		c.t.Fatalf("服务端不该加掩码：%#x", hdr[1])
	}
	if hdr[0]&0x70 != 0 {
		c.t.Fatalf("服务端不该设置 RSV：%#x", hdr[0])
	}
	length := int64(hdr[1] & 0x7f)
	switch length {
	case 126:
		var ext [2]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			c.t.Fatalf("读长度失败: %v", err)
		}
		length = int64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			c.t.Fatalf("读长度失败: %v", err)
		}
		length = int64(binary.BigEndian.Uint64(ext[:]))
	}
	payload = make([]byte, length)
	if _, err := io.ReadFull(c.br, payload); err != nil {
		c.t.Fatalf("读载荷失败: %v", err)
	}
	return hdr[0] & 0x0f, payload
}

// expectClose 断言下一帧是 close 且关闭码相符。
func (c *testClient) expectClose(code uint16) {
	c.t.Helper()
	op, payload := c.readFrame(3 * time.Second)
	if op != CloseMessage {
		c.t.Fatalf("期望 close 帧，得到 opcode=%#x payload=%q", op, payload)
	}
	if len(payload) < 2 {
		c.t.Fatalf("close 帧应带状态码，实际载荷 %q", payload)
	}
	if got := binary.BigEndian.Uint16(payload[:2]); got != code {
		c.t.Fatalf("关闭码期望 %d，实际 %d", code, got)
	}
}

// ==================== 用例 ====================

func TestIsUpgrade(t *testing.T) {
	key := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{1}, 16))
	build := func(method string, hdr map[string]string) *http.Request {
		r := httptest.NewRequest(method, "/api/ws/stream", nil)
		for k, v := range hdr {
			r.Header.Set(k, v)
		}
		return r
	}
	base := map[string]string{
		"Upgrade": "websocket", "Connection": "Upgrade",
		"Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": key,
	}

	cases := []struct {
		name string
		req  *http.Request
		want bool
	}{
		{"标准升级", build(http.MethodGet, base), true},
		{"Connection 多值且大小写不一", build(http.MethodGet, map[string]string{
			"Upgrade": "WebSocket", "Connection": "keep-alive, Upgrade",
			"Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": key}), true},
		{"POST 不是升级", build(http.MethodPost, base), false},
		{"缺 Upgrade 头", build(http.MethodGet, map[string]string{
			"Connection": "Upgrade", "Sec-WebSocket-Version": "13",
			"Sec-WebSocket-Key": key}), false},
		{"版本不是 13", build(http.MethodGet, map[string]string{
			"Upgrade": "websocket", "Connection": "Upgrade",
			"Sec-WebSocket-Version": "8", "Sec-WebSocket-Key": key}), false},
		{"Key 不是 16 字节", build(http.MethodGet, map[string]string{
			"Upgrade": "websocket", "Connection": "Upgrade",
			"Sec-WebSocket-Version": "13",
			"Sec-WebSocket-Key":     base64.StdEncoding.EncodeToString([]byte("short"))}), false},
		{"Key 非法 base64", build(http.MethodGet, map[string]string{
			"Upgrade": "websocket", "Connection": "Upgrade",
			"Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "!!!!"}), false},
		{"什么都没有", build(http.MethodGet, nil), false},
	}
	for _, c := range cases {
		if got := IsUpgrade(c.req); got != c.want {
			t.Errorf("%s: IsUpgrade=%v 期望 %v", c.name, got, c.want)
		}
	}
	// 非升级请求直接 Upgrade → ErrNotUpgrade（调用方据此走反代）
	if _, err := Upgrade(httptest.NewRecorder(), build(http.MethodGet, nil)); err != ErrNotUpgrade {
		t.Fatalf("非升级请求应返回 ErrNotUpgrade，实际 %v", err)
	}
}

func TestAcceptKeyRfcExample(t *testing.T) {
	// RFC 6455 §1.3 的示例值，逐字比对
	if got := acceptKey("dGhlIHNhbXBsZSBub25jZQ=="); got != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" {
		t.Fatalf("accept 计算不符 RFC 示例: %s", got)
	}
}

func TestReadWriteRoundTrip(t *testing.T) {
	got := make(chan string, 4)
	big := bytes.Repeat([]byte{0xAB}, 70000) // >0xFFFF：走 8 字节长度分支
	url := wsServer(t, func(c *Conn) {
		op, payload, err := c.ReadMessage()
		if err != nil || op != TextMessage {
			got <- "文本收帧失败: " + err.Error()
			return
		}
		got <- string(payload)
		if err := c.WriteText(`{"type":"status"}`); err != nil {
			got <- "写文本失败: " + err.Error()
			return
		}
		if err := c.WriteBinary(big); err != nil {
			got <- "写二进制失败: " + err.Error()
		}
	})
	c := dialWS(t, url)
	c.writeFrame(true, TextMessage, []byte("你好，世界"), true)
	if s := <-got; s != "你好，世界" {
		t.Fatalf("服务端收到的文本不符: %q", s)
	}
	if op, p := c.readFrame(3 * time.Second); op != TextMessage || string(p) != `{"type":"status"}` {
		t.Fatalf("读到的文本帧不符: op=%#x payload=%q", op, p)
	}
	if op, p := c.readFrame(3 * time.Second); op != BinaryMessage || !bytes.Equal(p, big) {
		t.Fatalf("读到的二进制帧不符: op=%#x len=%d", op, len(p))
	}
}

func TestFragmentedMessageWithInterleavedPing(t *testing.T) {
	body := make(chan string, 1)
	url := wsServer(t, func(c *Conn) {
		_, payload, err := c.ReadMessage()
		if err != nil {
			body <- "读失败: " + err.Error()
			return
		}
		body <- string(payload)
	})
	c := dialWS(t, url)
	c.writeFrame(false, TextMessage, []byte("he"), true) // 分片 1（FIN=0）
	c.writeFrame(true, PingMessage, []byte("hb"), true)  // 分片之间夹一个控制帧
	c.writeFrame(true, 0x0, []byte("llo"), true)         // 续帧收尾
	if op, p := c.readFrame(3 * time.Second); op != PongMessage || string(p) != "hb" {
		t.Fatalf("分片间夹的 ping 应回 pong: op=%#x payload=%q", op, p)
	}
	if s := <-body; s != "hello" {
		t.Fatalf("分片重组结果不符: %q", s)
	}
}

func TestCloseEcho(t *testing.T) {
	done := make(chan error, 1)
	url := wsServer(t, func(c *Conn) {
		_, _, err := c.ReadMessage()
		done <- err
	})
	c := dialWS(t, url)
	c.writeFrame(true, CloseMessage, []byte{0x03, 0xE8}, true) // 1000
	c.expectClose(1000)
	if err := <-done; err != ErrClosed {
		t.Fatalf("收到 close 后 ReadMessage 应返回 ErrClosed，实际 %v", err)
	}
}

// TestReadIdleTimeout 迁前等价：uvicorn 的 ws_ping_timeout 语义 ——
// 一句话不说的连接必须被回收（否则写者 goroutine 与 TCP 都收不回来）。
func TestReadIdleTimeout(t *testing.T) {
	done := make(chan error, 1)
	url := wsServer(t, func(c *Conn) {
		c.SetReadIdleTimeout(150 * time.Millisecond)
		_, _, err := c.ReadMessage()
		done <- err
	})
	_ = dialWS(t, url) // 连上后什么都不发
	select {
	case err := <-done:
		if err == nil || err == ErrClosed {
			t.Fatalf("静默超时应以读错误结束，实际 %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("静默超时没有触发（读永久阻塞 = 连接回收不了）")
	}
}

func TestKeepaliveSendsPing(t *testing.T) {
	ready := make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	url := wsServer(t, func(c *Conn) {
		close(ready)
		c.Keepalive(ctx, 40*time.Millisecond)
	})
	c := dialWS(t, url)
	<-ready
	op, payload := c.readFrame(3 * time.Second)
	if op != PingMessage {
		t.Fatalf("期望 ping，得到 opcode=%#x", op)
	}
	if len(payload) != 0 {
		t.Fatalf("ping 载荷应为空，实际 %q", payload)
	}
}

func TestWriteAfterClose(t *testing.T) {
	url := wsServer(t, func(c *Conn) {
		if err := c.CloseWith(StatusPolicyViolation, ""); err != nil {
			t.Errorf("CloseWith: %v", err)
		}
		if err := c.WriteText("late"); err != ErrClosed {
			t.Errorf("关闭后写入应返回 ErrClosed，实际 %v", err)
		}
		if err := c.Close(); err != nil { // 幂等
			t.Errorf("重复 Close 应无错，实际 %v", err)
		}
	})
	c := dialWS(t, url)
	c.expectClose(StatusPolicyViolation) // 1008：与 Python 未鉴权 WS 的关闭码一致
}

func TestProtocolViolations(t *testing.T) {
	cases := []struct {
		name  string
		code  uint16
		limit int64 // >0 时服务端把消息上限压到这个值
		write func(c *testClient)
	}{
		{"未掩码", StatusProtocolError, 0, func(c *testClient) {
			// FIN+text、len=1、**没有** mask 位：服务端在掩码检查处即拒（1 字节都不读）
			c.writeRaw(0x81, 0x01)
		}},
		{"RSV 位非 0", StatusProtocolError, 0, func(c *testClient) {
			c.writeRaw(0x80|0x40|TextMessage, 0x80|0x01)
		}},
		{"控制帧分片", StatusProtocolError, 0, func(c *testClient) {
			c.writeRaw(0x09, 0x80) // ping、FIN=0、掩码、空载荷
		}},
		{"控制帧载荷超长", StatusProtocolError, 0, func(c *testClient) {
			c.writeRaw(0x89, 0x80|126) // ping 声明 126 字节载荷
		}},
		{"未知操作码", StatusProtocolError, 0, func(c *testClient) {
			c.writeRaw(0x83, 0x80, 1, 2, 3, 4) // FIN=1、opcode=3、掩码、len=0
		}},
		{"没有起始帧的续帧", StatusProtocolError, 0, func(c *testClient) {
			c.writeRaw(0x80, 0x80, 1, 2, 3, 4) // FIN=1、opcode=0（续帧）、len=0
		}},
		{"消息超上限", StatusTooLarge, 16, func(c *testClient) {
			c.writeRaw(0x82, 0x80|64) // 声明 64 字节，超过 16 的上限
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			limit, want := tc.limit, tc.code
			url := wsServer(t, func(c *Conn) {
				if limit > 0 {
					c.SetMaxMessageBytes(limit)
				}
				_, _, err := c.ReadMessage()
				if err == nil {
					t.Errorf("违规帧应报错")
				}
			})
			c := dialWS(t, url)
			tc.write(c)
			c.expectClose(want)
		})
	}
}
