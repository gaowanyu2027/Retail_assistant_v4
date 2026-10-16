package auth

import (
	"net/http"

	"retail-assistant/edge/internal/httpx"
)

// Decision 早拒判定结果。
type Decision int

const (
	// Allow 放行。**包含"拿不准"**：回源失败、带 URL 凭据、WS 升级一律放行，
	// 由 Python 作权威判定。
	Allow Decision = iota
	// Deny 在 edge 就回 401（与 Python 的未登录响应逐字一致）。
	Deny
)

// Gate 早拒闸门：只读请求头与 URL，结论来自 Verifier。
type Gate struct {
	v       *Verifier
	enabled bool
}

// NewGate 构造闸门。enabled=false 时 Inspect 恒返回 Allow（配置开关回退，成本为零）。
// enabled=true 而 verifier 为 nil 属于接线错误，这里按禁用处理并由 main 告警。
func NewGate(v *Verifier, enabled bool) *Gate {
	return &Gate{v: v, enabled: enabled && v != nil}
}

// Enabled 是否真正启用。
func (g *Gate) Enabled() bool { return g != nil && g.enabled }

// Inspect 判定一个即将反代的请求。reason 只用于日志/指标（不是契约）。
func (g *Gate) Inspect(r *http.Request) (Decision, string) {
	if !g.Enabled() {
		return Allow, "disabled"
	}
	if !NeedsAuth(r.URL.Path) {
		return Allow, "public"
	}
	// WS 未鉴权在 Python 侧是 accept 后 close(1008)，不是 401（S0 冻结的协议契约）。
	if IsWebSocketUpgrade(r) {
		return Allow, "websocket"
	}
	// `?ticket=` 一次性票据要"消费"、`?token=` 由 AUTH_ALLOW_QUERY_TOKEN 决定 —— 都归 Python。
	if HasQueryCredential(r) {
		return Allow, "query-credential"
	}

	creds := Credentials(r.Header)
	if len(creds) == 0 {
		// 无任何凭据：Python 也会在不碰数据库的情况下 401 —— 这是零风险的那部分。
		return Deny, "no-credential"
	}
	for _, c := range creds {
		verdict := g.v.Verify(c.Token)
		if !verdict.Known {
			// 回源没答上来：放行，交 Python 判（宁可多做一次鉴权，不可误杀）。
			return Allow, "verify-unknown"
		}
		if verdict.Valid {
			return Allow, "valid-" + c.Source
		}
		// 这枚无效，继续试下一枚（垃圾 Cookie 不该把有效 Bearer 挡掉，台账 B13）。
	}
	return Deny, "invalid-credential"
}

// ServeDenied 逐字复刻 Python 中间件的未登录响应（contracts/auth_policy.json）。
// 安全头由调用链上的 httpx.RespWriter 补齐，与直连 Python 时的响应头一致。
func ServeDenied(w http.ResponseWriter, r *http.Request) {
	httpx.WriteString(w, r, UnauthorizedStatus, UnauthorizedCT, UnauthorizedBody)
}

// ServeInternalNotFound `/internal/**` 的本地 404：Python 的内部端点路径一律不出网。
// 与 Python 侧任何未匹配路径的 404 同形（夹具 not_found）。
func ServeInternalNotFound(w http.ResponseWriter, r *http.Request) {
	httpx.WriteString(w, r, NotFoundStatus, NotFoundCT, NotFoundBody)
}
