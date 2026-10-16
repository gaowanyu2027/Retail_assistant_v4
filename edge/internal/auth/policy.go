// Package auth 在边缘做鉴权"早拒"。
//
// 目标：明显没凭据、或凭据**明确无效**的请求，在 edge 就拿到与 Python 逐字一致的
// 401，不再占用 Python（单线程事件循环 + 进程内单 SQLite 连接）的资源；有效会话
// 每 5s 才回源校验一次（见 verifier.go 的 TTL 缓存）。
//
// 三条边界：
//
//  1. **不持 auth.db 句柄**（铁律③：一表一个写者、安全关键代码不复制）——
//     "这个令牌有效吗"由 Python 的 `/internal/auth/verify` 回答，edge 只缓存结论；
//  2. **只可能提前拒绝，不可能放行**：Python 对每个请求仍自行鉴权，edge 的判定
//     最多是把 Python 也会拒的请求提前拒掉；拿不准（回源失败）时一律放行；
//  3. **不解释凭据**：票据（`?ticket=`）、URL 令牌（`?token=`）、WebSocket 升级
//     一律跳过、交给 Python —— 前两者要"消费"（一次性，且有单写者语义），
//     后者 Python 的未鉴权语义是 accept 后 close(1008)，不是 401。
//
// 策略取值全部来自 contracts/auth_policy.json（由 Python 实现实测导出），
// 由 policy_test.go 逐字比对 —— 改实现不改夹具、或反过来，都会红。
package auth

import (
	"net/http"
	"strings"
)

// ==================== 对外契约（contracts/auth_policy.json） ====================

const (
	// CookieName 会话 Cookie 名（api/security.py 的 COOKIE_NAME）。
	CookieName = "retail_sid"
	// BearerPrefix Authorization 头的前缀（比较时不区分大小写）。
	BearerPrefix = "bearer "
	// UnauthorizedStatus / UnauthorizedCT / UnauthorizedBody 未登录响应的三要素，
	// 与 Python 中间件的响应**逐字节**一致（注意 body 里 `":` 后的空格）。
	UnauthorizedStatus = 401
	UnauthorizedCT     = "application/json; charset=utf-8"
	UnauthorizedBody   = `{"detail": "未登录或登录已过期"}`

	// NotFoundBody 未知路径的 404（FastAPI 默认异常处理器的渲染结果，实测导出）。
	// `/internal/**` 的本地 404 也用这一份：两条路径上同一个探测请求响应相同。
	NotFoundStatus = 404
	NotFoundCT     = "application/json"
	NotFoundBody   = `{"detail":"Not Found"}`
)

// publicPaths 对应 api/security.py 的 _PUBLIC_PATHS（夹具 public_paths）。
//
// 刻意**不**镜像 `/docs`、`/redoc`、`/openapi.json` 那条规则：它绑着
// AUTH_PUBLIC_DOCS 开关，且都带前缀匹配的边角（如 /docs 与 /docs/），
// 在 edge 复刻一份只会引入漂移风险 —— 文档路径的匿名请求照旧反代给 Python 判。
var publicPaths = map[string]struct{}{
	"/api/auth/login":        {},
	"/api/auth/logout":       {},
	"/api/health":            {},
	"/api/health/ready":      {},
	"/api/frontend/fallback": {},
}

// NeedsAuth 与 api/security.py 的 needs_auth 同规则（白名单外 `/api/**` 一律需登录）。
func NeedsAuth(path string) bool {
	if _, ok := publicPaths[path]; ok {
		return false
	}
	return strings.HasPrefix(path, "/api/")
}

// ==================== 请求侧判定 ====================

// Credential 一枚候选中凭据。
type Credential struct {
	Source string // "bearer" / "cookie"
	Token  string
}

// Credentials 按优先级返回候选凭据：显式 Bearer 优先于隐式 Cookie。
//
// 与 api/security.py 的 token_candidates_from_request 一致 —— 那里返回**列表**是因为
// 台账 B13：浏览器里一个别的应用留下的垃圾 Cookie，不该让同请求里有效的 Bearer 失效。
// 这里逐个尝试（见 Gate.Inspect），语义相同。
func Credentials(h http.Header) []Credential {
	var out []Credential
	if auth := h.Get("Authorization"); len(auth) >= len(BearerPrefix) &&
		strings.EqualFold(auth[:len(BearerPrefix)], BearerPrefix) {
		if tok := strings.TrimSpace(auth[len(BearerPrefix):]); tok != "" {
			out = append(out, Credential{Source: "bearer", Token: tok})
		}
	}
	for _, part := range strings.Split(h.Get("Cookie"), ";") {
		name, value, ok := strings.Cut(part, "=")
		if !ok || strings.TrimSpace(name) != CookieName {
			continue
		}
		if tok := strings.TrimSpace(value); tok != "" {
			out = append(out, Credential{Source: "cookie", Token: tok})
			break // 同名 Cookie 只取第一个（与 Python 侧的 break 一致）
		}
	}
	return out
}

// HasQueryCredential 请求是否带 URL 凭据（`?ticket=` 一次性票据 / `?token=` 会话令牌）。
// 两者都由 Python 处理：票据要**消费**（用后即焚，单写者语义不能复制到 Go）。
func HasQueryCredential(r *http.Request) bool {
	q := r.URL.Query()
	return q.Get("ticket") != "" || q.Get("token") != ""
}

// IsWebSocketUpgrade 是否为 WebSocket 升级请求。
// 未鉴权的 WS 在 Python 侧是"accept 后 close(1008)"，与 HTTP 的 401 语义不同，
// 早拒必须绕开它（S0 冻结的协议契约含这条）。
func IsWebSocketUpgrade(r *http.Request) bool {
	if !strings.EqualFold(strings.TrimSpace(r.Header.Get("Upgrade")), "websocket") {
		return false
	}
	for _, v := range strings.Split(r.Header.Get("Connection"), ",") {
		if strings.EqualFold(strings.TrimSpace(v), "upgrade") {
			return true
		}
	}
	return false
}
