package video

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"
)

// 测试用原始 WS 客户端：刻意自己实现（不引库）。
// 要断言的正是**字节与时机** —— 关闭码、消息顺序、"慢客户端不读"这种场景，
// 用库会被抽象挡在门外（同 internal/wsutil 的做法）。

type rawWS struct {
	t    *testing.T
	conn net.Conn
	br   *bufio.Reader
}

// dialOpts 握手参数。"慢客户端"用例不在这里体现：它靠**连上之后不再读**实现
// （见 handler_test.go 的扇出公平性用例）。
type dialOpts struct {
	headers map[string]string
}

// handshake 一次握手请求的结果。服务端可能回 101（升级成功），
// 也可能回别的（被反代、被拒 —— 用例要能分辨）。
type handshake struct {
	status  int
	headers map[string]string
	body    []byte
	conn    net.Conn
	br      *bufio.Reader
}

// rawHandshake 发一次 WS 握手请求并解析响应（不要求 101）。
func rawHandshake(t *testing.T, wsURL string, headers map[string]string) *handshake {
	t.Helper()
	u, err := url.Parse(wsURL)
	if err != nil {
		t.Fatalf("解析 URL 失败: %v", err)
	}
	if u.Path == "" {
		u.Path = "/"
	}
	conn, err := net.Dial("tcp", u.Host)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	key := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{0x37}, 16))
	var req strings.Builder
	req.WriteString("GET " + u.RequestURI() + " HTTP/1.1\r\n")
	req.WriteString("Host: " + u.Host + "\r\n")
	req.WriteString("Upgrade: websocket\r\nConnection: Upgrade\r\n")
	req.WriteString("Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n")
	for k, v := range headers {
		req.WriteString(k + ": " + v + "\r\n")
	}
	req.WriteString("\r\n")
	if _, err := io.WriteString(conn, req.String()); err != nil {
		t.Fatalf("写握手失败: %v", err)
	}
	br := bufio.NewReader(conn)
	statusLine, err := br.ReadString('\n')
	if err != nil {
		t.Fatalf("读握手响应失败: %v", err)
	}
	fields := strings.SplitN(strings.TrimSpace(statusLine), " ", 3)
	if len(fields) < 2 {
		t.Fatalf("状态行不合法: %q", statusLine)
	}
	code, err := strconv.Atoi(fields[1])
	if err != nil {
		t.Fatalf("状态码不合法: %q", statusLine)
	}
	hs := &handshake{status: code, headers: map[string]string{}, conn: conn, br: br}
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatalf("读响应头失败: %v", err)
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		if name, value, ok := strings.Cut(line, ":"); ok {
			hs.headers[strings.ToLower(strings.TrimSpace(name))] = strings.TrimSpace(value)
		}
	}
	if code != 101 {
		if n, err := strconv.Atoi(hs.headers["content-length"]); err == nil && n > 0 {
			hs.body = make([]byte, n)
			if _, err := io.ReadFull(br, hs.body); err != nil {
				t.Fatalf("读响应体失败: %v", err)
			}
		}
	}
	return hs
}

// dialRaw 建立一条 WS 连接（**要求**握手成功，否则 Fatal）。
func dialRaw(t *testing.T, wsURL string, opts dialOpts) *rawWS {
	t.Helper()
	hs := rawHandshake(t, wsURL, opts.headers)
	if hs.status != 101 {
		t.Fatalf("期望 101，得到 %d（body=%q）", hs.status, hs.body)
	}
	return &rawWS{t: t, conn: hs.conn, br: hs.br}
}

// shrinkBuffers 把本连接的接收缓冲压到很小，让"客户端不读"更容易变成写入阻塞。
//
// **只是尽力而为，不构成保证**：Windows 环回一次 2MB 的写实测能直接吞下
// （SO_RCVBUF 在已连接的 socket 上未必立刻收缩通告窗口），所以别拿它当"慢客户端"
// 的判据 —— 用到它的用例都自己**实测**写者是否真的卡住（frames_sent 追不追得上），
// 见 handler_test.go 的扇出公平性与控制文本优先两条。
func (c *rawWS) shrinkBuffers() {
	if tcp, ok := c.conn.(*net.TCPConn); ok {
		_ = tcp.SetReadBuffer(4096)
	}
}

// readFrame 读服务端下发的帧（出错即 Fatal）。
func (c *rawWS) readFrame(timeout time.Duration) (byte, []byte) {
	c.t.Helper()
	op, payload, err := c.tryReadFrame(timeout)
	if err != nil {
		c.t.Fatalf("读帧失败: %v", err)
	}
	return op, payload
}

// tryReadFrame 同 readFrame，但把错误交回调用方（用于"这里不该有消息"的断言，
// 以及在测试自己的 goroutine 里读取 —— 那里不允许调用 t.Fatalf）。
func (c *rawWS) tryReadFrame(timeout time.Duration) (byte, []byte, error) {
	_ = c.conn.SetReadDeadline(time.Now().Add(timeout))
	var hdr [2]byte
	if _, err := io.ReadFull(c.br, hdr[:]); err != nil {
		return 0, nil, err
	}
	if hdr[1]&0x80 != 0 || hdr[0]&0x70 != 0 {
		return 0, nil, fmt.Errorf("服务端帧头不合法：%#x %#x", hdr[0], hdr[1])
	}
	length := int64(hdr[1] & 0x7f)
	switch length {
	case 126:
		var ext [2]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			return 0, nil, err
		}
		length = int64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			return 0, nil, err
		}
		length = int64(binary.BigEndian.Uint64(ext[:]))
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(c.br, payload); err != nil {
		return 0, nil, err
	}
	return hdr[0] & 0x0f, payload, nil
}

// expectClose 断言下一帧是 close 且关闭码相符。
func (c *rawWS) expectClose(code uint16) {
	c.t.Helper()
	op, payload := c.readFrame(3 * time.Second)
	if op != wsClose {
		c.t.Fatalf("期望 close 帧，得到 opcode=%#x payload=%q", op, payload)
	}
	if len(payload) < 2 {
		c.t.Fatalf("close 帧应带状态码，实际载荷 %q", payload)
	}
	if got := binary.BigEndian.Uint16(payload[:2]); got != code {
		c.t.Fatalf("关闭码期望 %d，实际 %d", code, got)
	}
}

// tryClose 读下一帧；是 close 就返回其状态码，否则返回 ok=false（不 Fatal）。
func (c *rawWS) tryClose(timeout time.Duration) (uint16, bool, error) {
	op, payload, err := c.tryReadFrame(timeout)
	if err != nil {
		return 0, false, err
	}
	if op != wsClose || len(payload) < 2 {
		return 0, false, nil
	}
	return binary.BigEndian.Uint16(payload[:2]), true, nil
}

// writeText 发一条文本消息（加掩码，按 RFC 客户端必须加）。
func (c *rawWS) writeText(s string) {
	c.t.Helper()
	payload := []byte(s)
	key := []byte{0x11, 0x22, 0x33, 0x44}
	head := []byte{0x81}
	n := len(payload)
	switch {
	case n < 126:
		head = append(head, 0x80|byte(n))
	case n <= 0xFFFF:
		head = append(head, 0x80|126)
		head = binary.BigEndian.AppendUint16(head, uint16(n))
	default:
		head = append(head, 0x80|127)
		head = binary.BigEndian.AppendUint64(head, uint64(n))
	}
	head = append(head, key...)
	masked := make([]byte, n)
	for i, b := range payload {
		masked[i] = b ^ key[i&3]
	}
	if _, err := c.conn.Write(append(head, masked...)); err != nil {
		c.t.Fatalf("写文本帧失败: %v", err)
	}
}

// writeBinary 发一条二进制消息。
func (c *rawWS) writeBinary(payload []byte) {
	c.t.Helper()
	key := []byte{0x11, 0x22, 0x33, 0x44}
	head := []byte{0x82}
	n := len(payload)
	switch {
	case n < 126:
		head = append(head, 0x80|byte(n))
	case n <= 0xFFFF:
		head = append(head, 0x80|126)
		head = binary.BigEndian.AppendUint16(head, uint16(n))
	default:
		head = append(head, 0x80|127)
		head = binary.BigEndian.AppendUint64(head, uint64(n))
	}
	head = append(head, key...)
	masked := make([]byte, n)
	for i, b := range payload {
		masked[i] = b ^ key[i&3]
	}
	if _, err := c.conn.Write(append(head, masked...)); err != nil {
		c.t.Fatalf("写二进制帧失败: %v", err)
	}
}

// wsClose / wsText / wsBinary 与 wsutil 的 opcode 常量对应（测试内独立一份，
// 避免为测试导出内部常量）。
const (
	wsText   byte = 0x1
	wsBinary byte = 0x2
	wsClose  byte = 0x8
)
