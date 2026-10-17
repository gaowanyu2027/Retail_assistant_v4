// Package wsutil 实现 WebSocket **服务端**所需的 RFC 6455 子集（纯标准库）。
//
// 为什么自研：edge 的 go.mod 刻意零外部依赖 —— 部署服务器访问不了 GitHub，
// 构建期不能拉任何模块（见 AGENTS.md 已知陷阱）。视频数据面用到的只有
// 「握手 / 帧读写 / 掩码 / ping-pong-close」这几件事，不值得为此引入依赖树。
//
// 明确不做（用不上；少一个分支就少一处与浏览器行为不一致的可能）：
//
//   - 压缩扩展（permessage-deflate）：不协商 → RSV 位一律要求 0；
//   - 子协议协商：前端 `new WebSocket(url)` 不带子协议；
//   - 客户端侧（主动发起连接）：edge 只做服务端；
//   - 分片**发送**：下行只有「一帧 JPEG / 一条短文本」，整条一次写出（writev）。
//     分片**接收**按 RFC 支持 —— 浏览器对大帧是会分片的。
//
// 存活探测：写侧每 PingInterval 发一个 ping + 读侧 ReadIdleTimeout 未收到
// 任何入站帧即断开。这不是自选动作：迁移前 uvicorn 的 websockets 实现默认
// `ws_ping_interval=20s / ws_ping_timeout=20s`（即"20s 发 ping、20s 内没 pong
// 就掐"），迁到 Go 后不能把这个能力丢掉 —— 否则一个被黑洞掉的连接会永远
// 占着一个写者 goroutine 和一条 TCP。
package wsutil

import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// 操作码（RFC 6455 §5.2）。
const (
	TextMessage   byte = 0x1
	BinaryMessage byte = 0x2
	CloseMessage  byte = 0x8
	PingMessage   byte = 0x9
	PongMessage   byte = 0xA
)

// 关闭码（用到的几个；其余按 RFC 原样透传）。
const (
	StatusNormal          uint16 = 1000
	StatusGoingAway       uint16 = 1001
	StatusProtocolError   uint16 = 1002
	StatusPolicyViolation uint16 = 1008
	StatusTooLarge        uint16 = 1009
)

const (
	// MaxMessageBytes 单条消息上限。前端一帧 JPEG 实测 ~90KB（1280×720 q70），
	// 8MiB 留足余量，又能在被灌数据时立刻断开（1009）而不把内存吃穿。
	MaxMessageBytes int64 = 8 << 20
	// WriteTimeout 单次写出的截止时间。WS 协议本身没有"写超时"，
	// 但对端不读时 TCP 发送缓冲写满，Write 会一直阻塞 —— 一个不读数据的
	// 客户端就能把写者 goroutine 钉死。超时后按写失败处理（连接就此结束）。
	WriteTimeout = 15 * time.Second
	// PingInterval 心跳间隔（与 uvicorn 默认 20s 同量级）。
	PingInterval = 25 * time.Second
	// ReadIdleTimeout 静默上限：这么久没读到**任何**入站帧就断开。
	// 正常客户端会被上面的 ping 引出 pong 而不断续期；黑洞连接在这里回收。
	ReadIdleTimeout = 60 * time.Second
)

// wsGUID RFC 6455 §1.3 规定的握手魔术串。
const wsGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

// 哨兵错误（用 errors.Is 判定）。
var (
	// ErrNotUpgrade 不是合法的 WebSocket 升级请求（应交给反代/正常 HTTP 路径）。
	ErrNotUpgrade = errors.New("wsutil: 不是 WebSocket 升级请求")
	// ErrClosed 连接已关闭：对端发了 close，或本端已发过 close。
	ErrClosed = errors.New("wsutil: 连接已关闭")
	// ErrProtocol 协议错误（已回 close 1002）。
	ErrProtocol = errors.New("wsutil: 协议错误")
	// ErrTooLarge 消息超过上限（已回 close 1009）。
	ErrTooLarge = errors.New("wsutil: 消息超过上限")
)

// ==================== 握手 ====================

// IsUpgrade 判定一个请求是否为**合法的** WebSocket 升级请求。
//
// 比 api/security.py 那侧的 `is_websocket_upgrade` 更严：这里多校验版本与
// 16 字节的 Sec-WebSocket-Key。用途不同 —— auth 侧只管"别对 WS 做 401 早拒"，
// 宽松无妨；这里的结论决定"edge 是本地接管还是原样反代"，判错会改变响应字节，
// 所以宁可漏判（漏判 = 交给 Python，行为与迁移前完全一致）也不误判。
func IsUpgrade(r *http.Request) bool {
	if r == nil || r.Method != http.MethodGet {
		return false
	}
	if !headerHasToken(r.Header, "Connection", "upgrade") {
		return false
	}
	if !strings.EqualFold(strings.TrimSpace(r.Header.Get("Upgrade")), "websocket") {
		return false
	}
	if strings.TrimSpace(r.Header.Get("Sec-WebSocket-Version")) != "13" {
		return false
	}
	return validKey(r.Header.Get("Sec-WebSocket-Key"))
}

// Upgrade 完成握手：劫持连接 → 裸写 101 响应 → 返回可读写的 Conn。
// 调用方必须保证 IsUpgrade(r) 为真（否则返回 ErrNotUpgrade）。
func Upgrade(w http.ResponseWriter, r *http.Request) (*Conn, error) {
	if !IsUpgrade(r) {
		return nil, ErrNotUpgrade
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		return nil, errors.New("wsutil: 底层 ResponseWriter 不支持 Hijack")
	}
	netConn, brw, err := hj.Hijack()
	if err != nil {
		return nil, err
	}
	// 101 必须**裸写**到已劫持的连接上：再经过 http.ResponseWriter 会带上
	// 普通响应的头（Date/Content-Length 之类），与 Python 的 101 形状不符。
	resp := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + acceptKey(r.Header.Get("Sec-WebSocket-Key")) + "\r\n\r\n"
	_ = netConn.SetWriteDeadline(time.Now().Add(WriteTimeout))
	if _, err := netConn.Write([]byte(resp)); err != nil {
		_ = netConn.Close()
		return nil, err
	}
	_ = netConn.SetWriteDeadline(time.Time{})

	c := &Conn{
		conn:            netConn,
		br:              brw.Reader,
		maxMessageBytes: MaxMessageBytes,
		readIdle:        ReadIdleTimeout,
		writeTimeout:    WriteTimeout,
	}
	// 初始读截止时间：之后每次读到帧都会续期（见 readFrame）。
	_ = netConn.SetReadDeadline(time.Now().Add(c.readIdle))
	return c, nil
}

// acceptKey 计算 Sec-WebSocket-Accept（RFC 6455 §4.2.2）。
func acceptKey(key string) string {
	h := sha1.New()
	_, _ = io.WriteString(h, key)
	_, _ = io.WriteString(h, wsGUID)
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

// validKey 校验 Sec-WebSocket-Key：base64 解出必须正好 16 字节。
func validKey(key string) bool {
	if key == "" {
		return false
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(key))
	return err == nil && len(raw) == 16
}

// headerHasToken 判断某个多值头里是否含指定 token（逗号分隔、大小写不敏感）。
func headerHasToken(h http.Header, name, token string) bool {
	for _, v := range h.Values(name) {
		for _, part := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(part), token) {
				return true
			}
		}
	}
	return false
}

// ==================== 连接 ====================

// Conn 一条已握手完成的 WebSocket 连接。
//
// 读侧与写侧可以由不同 goroutine 使用（本项目就是「一个读 goroutine + 一个写
// goroutine」）；写出（含读侧自动回的 pong / close）一律经内部互斥量串行化，
// 不会在字节层面交错。
type Conn struct {
	conn net.Conn
	br   *bufio.Reader

	mu         sync.Mutex
	closeSent  bool
	connClosed bool

	maxMessageBytes int64
	readIdle        time.Duration
	writeTimeout    time.Duration
}

// RemoteAddr 对端地址（日志用）。
func (c *Conn) RemoteAddr() net.Addr { return c.conn.RemoteAddr() }

// SetMaxMessageBytes 调整单条消息上限（默认 MaxMessageBytes）。
func (c *Conn) SetMaxMessageBytes(n int64) { c.maxMessageBytes = n }

// SetReadIdleTimeout 调整静默上限（默认 ReadIdleTimeout）。测试用短值。
func (c *Conn) SetReadIdleTimeout(d time.Duration) {
	c.readIdle = d
	_ = c.conn.SetReadDeadline(time.Now().Add(d))
}

// SetWriteTimeout 调整单次写出的截止时间（默认 WriteTimeout）。测试用短值，
// 免得"对端不读"的用例要等满 15s。
func (c *Conn) SetWriteTimeout(d time.Duration) { c.writeTimeout = d }

// ReadMessage 读下一条完整的数据消息。返回 opcode（TextMessage/BinaryMessage）
// 与**已解掩码**的载荷。
//
// 控制帧在这里内部消化，调用方看不到：
//   - ping → 立刻回 pong（浏览器/小程序会自动回我们的 ping，靠的就是对端这一侧）；
//   - pong → 忽略（读截止时间已在 readFrame 里续期，这就是存活证据）；
//   - close → 原样回一条 close 并关闭 TCP，返回 ErrClosed。
//
// 出错时连接已不可用（内部已尽力发出对应的 close 帧）。
func (c *Conn) ReadMessage() (byte, []byte, error) {
	var (
		msgOp  byte
		msgBuf []byte
	)
	for {
		fin, opcode, payload, err := c.readFrame()
		if err != nil {
			return 0, nil, err
		}
		switch opcode {
		case PingMessage:
			_ = c.writeControl(PongMessage, payload)
			continue
		case PongMessage:
			continue
		case CloseMessage:
			c.echoClose(payload)
			return 0, nil, ErrClosed
		case TextMessage, BinaryMessage:
			if msgOp != 0 {
				// 上一条消息还没收完又来了新的数据帧（RFC 6455 §5.4 禁止）
				_ = c.CloseWith(StatusProtocolError, "分片未完成又收到新的数据帧")
				return 0, nil, ErrProtocol
			}
			msgOp = opcode
			msgBuf = append(msgBuf, payload...)
		case 0:
			// 续帧
			if msgOp == 0 {
				_ = c.CloseWith(StatusProtocolError, "没有起始数据帧的续帧")
				return 0, nil, ErrProtocol
			}
			msgBuf = append(msgBuf, payload...)
		default:
			_ = c.CloseWith(StatusProtocolError, "未知操作码")
			return 0, nil, ErrProtocol
		}
		if int64(len(msgBuf)) > c.maxMessageBytes {
			_ = c.CloseWith(StatusTooLarge, "消息超过上限")
			return 0, nil, ErrTooLarge
		}
		if fin {
			return msgOp, msgBuf, nil
		}
	}
}

// readFrame 读一个帧：**先长度扩展、再掩码键、最后载荷**（RFC 6455 §5.2 的线序）。
// 校验不过时已回对应的 close 帧，调用方只需处理错误。
func (c *Conn) readFrame() (fin bool, opcode byte, payload []byte, err error) {
	// 每次读帧都续期：静默太久（readIdle）即认定连接已死。
	_ = c.conn.SetReadDeadline(time.Now().Add(c.readIdle))

	var hdr [2]byte
	if _, err := io.ReadFull(c.br, hdr[:]); err != nil {
		return false, 0, nil, err // 对端直接断开/超时/网络错误：不再回 close
	}
	fin = hdr[0]&0x80 != 0
	if hdr[0]&0x70 != 0 {
		return false, 0, nil, c.fail(StatusProtocolError, "RSV 必须为 0（未协商任何扩展）")
	}
	opcode = hdr[0] & 0x0f
	masked := hdr[1]&0x80 != 0
	length := int64(hdr[1] & 0x7f)

	if opcode >= CloseMessage {
		// 控制帧：不得分片、载荷 ≤125 字节（RFC 6455 §5.5）
		if !fin || length > 125 {
			return false, 0, nil, c.fail(StatusProtocolError, "控制帧必须 FIN=1 且载荷 ≤125 字节")
		}
	}

	switch length {
	case 126:
		var ext [2]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			return false, 0, nil, err
		}
		length = int64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err := io.ReadFull(c.br, ext[:]); err != nil {
			return false, 0, nil, err
		}
		v := binary.BigEndian.Uint64(ext[:])
		if v > 1<<62 {
			return false, 0, nil, c.fail(StatusTooLarge, "帧长度超出可接受范围")
		}
		length = int64(v)
	}

	if !masked {
		// 客户端→服务端必须掩码（RFC 6455 §5.1）
		return false, 0, nil, c.fail(StatusProtocolError, "客户端发来的帧必须掩码")
	}

	if length > c.maxMessageBytes {
		// 长度检查放在读掩码键/载荷**之前**：超限时一个字节都不再读，
		// 立刻回 1009 断开（被灌数据时不给对方继续占带宽的机会）
		return false, 0, nil, c.fail(StatusTooLarge, "消息超过上限")
	}

	var key [4]byte
	if _, err := io.ReadFull(c.br, key[:]); err != nil {
		return false, 0, nil, err
	}

	payload = make([]byte, length)
	if _, err := io.ReadFull(c.br, payload); err != nil {
		return false, 0, nil, err
	}
	for i := range payload {
		payload[i] ^= key[i&3]
	}
	return fin, opcode, payload, nil
}

// fail 回一条 close 并关闭连接，返回原错误（供 readFrame/ReadMessage 内联使用）。
func (c *Conn) fail(code uint16, reason string) error {
	_ = c.CloseWith(code, reason)
	if code == StatusTooLarge {
		return ErrTooLarge
	}
	return ErrProtocol
}

// ==================== 写 ====================

// WriteText 发一条文本消息（UTF-8）。
func (c *Conn) WriteText(s string) error { return c.writeData(TextMessage, []byte(s)) }

// WriteBinary 发一条二进制消息。
func (c *Conn) WriteBinary(b []byte) error { return c.writeData(BinaryMessage, b) }

// WritePing 发一个 ping（心跳；对端应回 pong）。
func (c *Conn) WritePing(payload []byte) error { return c.writeControl(PingMessage, payload) }

// Keepalive 阻塞直到 ctx 结束，期间每 interval 发一个 ping（传 PingInterval）。
// 配合读侧的 ReadIdleTimeout 回收黑洞连接（处理方在自己的 goroutine 里调用）。
func (c *Conn) Keepalive(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = PingInterval
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := c.WritePing(nil); err != nil {
				return
			}
		}
	}
}

func (c *Conn) writeData(opcode byte, payload []byte) error {
	c.mu.Lock()
	if c.closeSent || c.connClosed {
		c.mu.Unlock()
		return ErrClosed
	}
	err := c.writeFrameLocked(opcode, payload)
	c.mu.Unlock()
	if err != nil {
		// 写失败即连接不可用：立刻关掉，让阻塞在 Read 的读侧醒过来
		_ = c.Close()
	}
	return err
}

func (c *Conn) writeControl(opcode byte, payload []byte) error {
	if len(payload) > 125 {
		payload = payload[:125]
	}
	c.mu.Lock()
	if c.closeSent || c.connClosed {
		c.mu.Unlock()
		return ErrClosed
	}
	err := c.writeFrameLocked(opcode, payload)
	c.mu.Unlock()
	if err != nil {
		_ = c.Close()
	}
	return err
}

// writeFrameLocked 写一个帧（调用方持锁）。服务端→客户端不加掩码。
func (c *Conn) writeFrameLocked(opcode byte, payload []byte) error {
	if err := c.conn.SetWriteDeadline(time.Now().Add(c.writeTimeout)); err != nil {
		return err
	}
	var hdr [10]byte
	hdr[0] = 0x80 | opcode // FIN=1、无 RSV
	n := 2
	switch {
	case len(payload) < 126:
		hdr[1] = byte(len(payload))
	case len(payload) <= 0xFFFF:
		hdr[1] = 126
		binary.BigEndian.PutUint16(hdr[2:], uint16(len(payload)))
		n = 4
	default:
		hdr[1] = 127
		binary.BigEndian.PutUint64(hdr[2:], uint64(len(payload)))
		n = 10
	}
	// net.Buffers：头与载荷一次 writev 写出，不出现"半条消息"的中间可见状态
	bufs := net.Buffers{hdr[:n], payload}
	_, err := bufs.WriteTo(c.conn)
	return err
}

// Close 幂等地关闭：若尚未发过 close，先发 1001（going away）再关 TCP。
func (c *Conn) Close() error { return c.CloseWith(StatusGoingAway, "") }

// CloseWith 发一条带关闭码的 close 帧并关闭 TCP（幂等；已发过 close 则只关 TCP）。
func (c *Conn) CloseWith(code uint16, reason string) error {
	c.mu.Lock()
	if c.connClosed {
		c.mu.Unlock()
		return nil
	}
	if !c.closeSent {
		c.closeSent = true
		if code != 0 {
			_ = c.writeFrameLocked(CloseMessage, closePayload(code, reason))
		}
	}
	c.connClosed = true
	c.mu.Unlock()
	return c.conn.Close()
}

// echoClose 对端先发了 close：原样回一条（RFC 6455 §5.5.1 要求回显状态码），
// 然后关 TCP。
func (c *Conn) echoClose(payload []byte) {
	c.mu.Lock()
	if !c.closeSent {
		c.closeSent = true
		_ = c.writeFrameLocked(CloseMessage, payload)
	}
	c.connClosed = true
	c.mu.Unlock()
	_ = c.conn.Close()
}

// closePayload 组装 close 帧载荷：2 字节大端状态码 + 理由（控制帧上限 125 字节）。
func closePayload(code uint16, reason string) []byte {
	b := make([]byte, 2, 2+len(reason))
	binary.BigEndian.PutUint16(b, code)
	b = append(b, reason...)
	if len(b) > 125 {
		b = b[:125]
	}
	return b
}
