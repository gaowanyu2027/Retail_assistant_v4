// Package httpx 提供 edge 各处理路径共用的 HTTP 小工具。
//
// 承担三件事（对应 api/main.py 的同名中间件，取值逐字一致）：
//
//  1. 安全响应头（security_headers，setdefault 语义：已存在的头不覆盖，
//     反代响应里 Python 已设置的头原样保留）；
//  2. 响应状态码记录（供访问日志与 /metrics 统计）；
//  3. 固定字节的 JSON/文本输出助手。
//
// RespWriter 包装 ResponseWriter 后必须保持 WS 升级（Hijack）与 SSE（Flush）
// 能力可用：既实现可选接口的显式转发，也实现 Unwrap 供 http.ResponseController
// 使用（Go 1.20+ 的 ReverseProxy 走 ResponseController 取 Hijack/Flush）。
package httpx

import (
	"bufio"
	"errors"
	"io"
	"net"
	"net/http"
	"net/netip"
	"strconv"
)

// CSP 与 api/main.py 的 security_headers 逐字一致（含分号后的单个空格）。
const csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
	"img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' ws: wss:; " +
	"font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"

// ApplySecurityHeaders 按 setdefault 语义补齐安全头。
// proto 固定传 "http"：edge 是链路最外层且无 TLS，入站 X-Forwarded-Proto
// 会被反代层剥离、不被采信（与 Python 侧 HSTS 只在 https 下发送的意图一致）。
func ApplySecurityHeaders(h http.Header) {
	setIfAbsent(h, "X-Content-Type-Options", "nosniff")
	setIfAbsent(h, "X-Frame-Options", "DENY")
	setIfAbsent(h, "Referrer-Policy", "no-referrer")
	setIfAbsent(h, "Permissions-Policy",
		"camera=(self), microphone=(self), geolocation=(), payment=()")
	setIfAbsent(h, "Content-Security-Policy", csp)
}

func setIfAbsent(h http.Header, key, value string) {
	if h.Get(key) == "" {
		h.Set(key, value)
	}
}

// RespWriter 记录状态码，并在首次写出前补齐安全头。
type RespWriter struct {
	http.ResponseWriter
	status  int
	applied bool
}

func NewRespWriter(w http.ResponseWriter) *RespWriter {
	return &RespWriter{ResponseWriter: w}
}

// Status 已写出的状态码；尚未写出时按 net/http 语义视为 200。
func (w *RespWriter) Status() int {
	if w.status == 0 {
		return http.StatusOK
	}
	return w.status
}

func (w *RespWriter) WriteHeader(code int) {
	if w.status != 0 {
		return
	}
	w.status = code
	w.apply()
	w.ResponseWriter.WriteHeader(code)
}

func (w *RespWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
		w.apply()
	}
	return w.ResponseWriter.Write(b)
}

func (w *RespWriter) apply() {
	if w.applied {
		return
	}
	w.applied = true
	ApplySecurityHeaders(w.Header())
}

// Flush 转发给底层（SSE / 流式响应需要立即冲出）。
func (w *RespWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Hijack 转发给底层（WebSocket 升级依赖它）。
func (w *RespWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	if h, ok := w.ResponseWriter.(http.Hijacker); ok {
		return h.Hijack()
	}
	return nil, nil, errors.New("httpx: 底层 ResponseWriter 不支持 Hijack")
}

// ReadFrom 保留大响应的 sendfile/零拷贝快路径。
func (w *RespWriter) ReadFrom(r io.Reader) (int64, error) {
	if w.status == 0 {
		w.status = http.StatusOK
		w.apply()
	}
	if rf, ok := w.ResponseWriter.(io.ReaderFrom); ok {
		return rf.ReadFrom(r)
	}
	return io.Copy(w.ResponseWriter, r)
}

// Unwrap 供 http.ResponseController 解包（Go 1.20+ 取 Hijack/Flush 的推荐路径）。
func (w *RespWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }

// MarkUpgraded 记录一次协议升级（WebSocket 握手）。
//
// 升级响应是 Hijack 之后直写裸连接的，绕过了本包装器 —— 不显式告知，访问日志
// 与 /metrics 就会把它记成 200。只记第一个状态码，与 WriteHeader 同语义。
func (w *RespWriter) MarkUpgraded(code int) {
	if w.status == 0 {
		w.status = code
	}
}

// WriteString 固定字节输出：显式 Content-Length，HEAD 请求只回头部。
//
// 显式 Content-Length 是为了对齐 FastAPI/Starlette 的行为（响应体长度已知时
// 一律带 Content-Length，而非分块传输）。
func WriteString(w http.ResponseWriter, r *http.Request, status int, contentType, body string) {
	h := w.Header()
	h.Set("Content-Type", contentType)
	h.Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	if r != nil && r.Method == http.MethodHead {
		return
	}
	_, _ = io.WriteString(w, body)
}

// ClientIP 取直连对端 IP（不做端口拼接；解析失败时原样返回）。
// 入站 X-Forwarded-For 一律不采信——edge 就是最外层。
func ClientIP(remoteAddr string) string {
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		return remoteAddr
	}
	return host
}

// IsPrivateRemote 判定直连对端是否为环回 / 私网 / 链路本地地址。
// 解析失败（含空地址）一律拒绝——安全默认值。
//
// 用于所有"只允许内网同伴进程访问"的本地端点（/metrics、/internal/report/**）：
// edge 是最外层，RemoteAddr 就是真实对端，不存在需要信任的转发头。
func IsPrivateRemote(remoteAddr string) bool {
	ip, err := netip.ParseAddr(ClientIP(remoteAddr))
	if err != nil {
		return false
	}
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast()
}
