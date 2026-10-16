// Package proxy 兜底反代：把不归 edge 本地处理的请求原样转给 Python。
//
// 关键属性：
//
//   - 字节透明：请求头/体、响应头/体原样转发（SSE 的 data: 分帧、WS 的升级握手
//     都不做任何加工）；
//   - WS 升级与 SSE：FlushInterval=-1（每次写出立即 flush），升级走
//     ReverseProxy 的协议切换（依赖 RespWriter 的 Hijack 透传）；
//   - 信任边界：客户端发来的 X-Forwarded-* / X-Real-IP / X-Internal-* 一律**剥离**，
//     只按直连对端重设 X-Forwarded-For / X-Forwarded-Proto / X-Forwarded-Host /
//     X-Real-IP。Python 侧 `AUTH_TRUST_FORWARDED_FOR=1` 时才会采信这些值
//     （登录限流/IP 展示的口径修正依赖这条链路，见 api/security.py）。
//   - Host 保持客户端原样（Python 生成的绝对跳转地址与直连时一致）。
package proxy

import (
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"

	"retail-assistant/edge/internal/httpx"
)

// Proxy 反代处理器。
type Proxy struct {
	rp      *httputil.ReverseProxy
	backend string
}

// New 构造反代；onError 在每次上游失败时回调（计入指标），可为 nil。
func New(backendURL string, onError func()) (*Proxy, error) {
	target, err := url.Parse(backendURL)
	if err != nil || target.Scheme == "" || target.Host == "" {
		return nil, fmt.Errorf("EDGE_BACKEND_URL 无效: %q", backendURL)
	}
	tr := &http.Transport{
		// 显式禁用环境代理：后端在容器网络 / 本机回环，绝不该走 HTTP_PROXY。
		Proxy: nil,
		DialContext: (&net.Dialer{
			Timeout:   10 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		MaxIdleConns:          128,
		MaxIdleConnsPerHost:   64, // 视频 WS + SSE 常驻连接 + 常规 API 并发
		IdleConnTimeout:       90 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		// 不设 ResponseHeaderTimeout：/api/queries 等同步 Agent 调用在 Python 侧
		// 要跑数十秒到分钟级，设短了会把长请求误杀。超时语义由 Python 掌握，
		// edge 只做透明转发（客户端断开时 context 取消会正常传导）。
	}
	rp := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(target)
			// 保留客户端看到的 Host：Python 生成的绝对跳转/链接与直连一致。
			pr.Out.Host = pr.In.Host

			// 信任边界：剥离一切客户端可伪造的转发/内部头，只按直连对端重设。
			pr.Out.Header.Del("X-Forwarded-For")
			pr.Out.Header.Del("X-Forwarded-Proto")
			pr.Out.Header.Del("X-Forwarded-Host")
			pr.Out.Header.Del("X-Real-IP")
			stripPrefix(pr.Out.Header, "X-Internal-")

			pr.SetXForwarded() // XFF=直连对端 IP；XFH=In.Host；XFP=http(s)
			if ip := httpx.ClientIP(pr.In.RemoteAddr); ip != "" {
				pr.Out.Header.Set("X-Real-IP", ip)
			}
		},
		Transport: tr,
		// 每次写出立即 flush：SSE（/api/queries/stream）与其它流式响应不攒批。
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			if onError != nil {
				onError()
			}
			slog.Warn("proxy upstream error", "path", r.URL.Path, "err", err)
			httpx.WriteString(w, r, http.StatusBadGateway,
				"application/json", `{"detail":"Bad Gateway"}`)
		},
		ErrorLog: slog.NewLogLogger(slog.Default().Handler(), slog.LevelWarn),
	}
	return &Proxy{rp: rp, backend: backendURL}, nil
}

// ServeHTTP 实现 http.Handler。
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) { p.rp.ServeHTTP(w, r) }

// Backend 返回后端基地址（指标展示用）。
func (p *Proxy) Backend() string { return p.backend }

func stripPrefix(h http.Header, prefix string) {
	lp := strings.ToLower(prefix)
	for k := range h {
		if strings.HasPrefix(strings.ToLower(k), lp) {
			h.Del(k)
		}
	}
}
